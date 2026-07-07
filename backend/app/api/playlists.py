"""CRUD endpoints for MusicDrop-owned playlists.

Thin router over ``app.playlists.store``. The store directory is resolved from
``settings`` (not ``app.state``) so it works under the lifespan-less ``client``
test fixture; tests drive it via the monkeypatched ``settings.beets_dir``.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.concurrency import run_in_threadpool

from app.api.albums import get_library
from app.api.plex import get_plex_store
from app.beets.library import LibraryHandle
from app.beets.playlist_match import build_match_index, match_entries
from app.beets.playlists import (
    TrackRef,
    item_exists,
    m3u_entries,
    resolve_entries,
    track_match_refs,
)
from app.config import settings
from app.models.playlist import (
    Playlist,
    PlaylistAddTracksRequest,
    PlaylistCreateRequest,
    PlaylistDetail,
    PlaylistReorderRequest,
    PlaylistResolveEntryRequest,
    PlaylistUpdateRequest,
)
from app.models.playlist_import import (
    PlaylistImportPreview,
    PlaylistImportPreviewRequest,
    PlaylistImportPreviewResponse,
    PlaylistImportRequest,
    PlaylistImportResponse,
)
from app.playlists import store
from app.playlists.m3u import delete_m3u, write_m3u
from app.playlists.m3u_parse import parse_m3u
from app.playlists.store import StoredEntry, StoredPlaylist
from app.plex import playlists_pull
from app.plex import sync as plex_sync
from app.plex.config import PlexConfig, PlexConfigStore
from app.plex.errors import PlexConnectionError, PlexNotConfigured
from app.plex.mapping import PlexTrackSpec
from app.plex.paths import translate_path

router = APIRouter(tags=["playlists"])
logger = logging.getLogger(__name__)


def get_playlists_dir() -> Path:
    """Resolve the owned-playlist store dir from settings.

    Empty ``MUSICDROP_PLAYLISTS_DIR`` -> ``<beets_dir>/playlists``.
    """
    configured = settings.playlists_dir.strip()
    if configured:
        return Path(configured)
    return Path(settings.beets_dir) / "playlists"


def _to_playlist(record: StoredPlaylist) -> Playlist:
    resolved = len(record.resolved_item_ids)
    return Playlist(
        id=record.id,
        name=record.name,
        description=record.description,
        track_count=resolved,
        pending_count=len(record.entries) - resolved,
        target_plex_users=record.target_plex_users,
        plex=record.plex,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


async def _detail_response(record: StoredPlaylist, handle: LibraryHandle) -> PlaylistDetail:
    tracks = await run_in_threadpool(resolve_entries, handle.lib, record.entries)
    return PlaylistDetail(**_to_playlist(record).model_dump(), tracks=tracks)


def _export_dir(handle: LibraryHandle) -> Path:
    configured = settings.playlists_export_dir.strip()
    if configured:
        return Path(configured)
    return Path(os.fsdecode(handle.lib.directory)) / ".playlists"


def _render_export(record: StoredPlaylist, handle: LibraryHandle, export_dir: Path) -> None:
    entries = m3u_entries(handle.lib, record.resolved_item_ids, str(export_dir))
    write_m3u(export_dir / f"{record.id}.m3u8", record.name, entries)


async def _export_playlist(record: StoredPlaylist, handle: LibraryHandle) -> None:
    """Best-effort `.m3u8` (re)write. The owned store is the source of truth, so
    a filesystem hiccup never fails the mutation."""
    try:
        await run_in_threadpool(_render_export, record, handle, _export_dir(handle))
    except Exception:
        # Best-effort: a filesystem hiccup (or any export failure) must never
        # fail the mutation — the owned store already holds the truth. Log it.
        logger.warning("Playlist .m3u8 export failed for %s", record.id, exc_info=True)


async def _remove_export(playlist_id: str, handle: LibraryHandle) -> None:
    try:
        await run_in_threadpool(delete_m3u, _export_dir(handle) / f"{playlist_id}.m3u8")
    except Exception:
        logger.warning("Playlist .m3u8 removal failed for %s", playlist_id, exc_info=True)


async def _best_effort_plex_delete(
    config: PlexConfig, rating_keys: dict[str, str | None], ctx: str, *, playlist_id: str
) -> None:
    """Remove the playlist (by recorded ratingKey, else its id marker) from the
    given Plex accounts. Best-effort: a Plex hiccup (or no Plex configured) must
    never fail the local operation."""
    if not (config.base_url and config.token) or not rating_keys:
        return
    try:
        await run_in_threadpool(
            plex_sync.delete_playlist_on_targets, config, rating_keys, playlist_id=playlist_id
        )
    except Exception:  # best-effort cleanup — log and move on, never fail the op
        logger.warning("Plex playlist cleanup failed (%s)", ctx, exc_info=True)


@router.get("/playlists", response_model=list[Playlist])
async def list_playlists_endpoint(
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
) -> list[Playlist]:
    records = await run_in_threadpool(store.list_playlists, playlists_dir)
    return [_to_playlist(record) for record in records]


@router.post("/playlists", response_model=Playlist)
async def create_playlist_endpoint(
    body: PlaylistCreateRequest,
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> Playlist:
    record = await run_in_threadpool(
        store.create_playlist,
        playlists_dir,
        name=body.name,
        description=body.description,
    )
    await _export_playlist(record, handle)
    return _to_playlist(record)


@router.get("/playlists/{playlist_id}", response_model=PlaylistDetail)
async def get_playlist_endpoint(
    playlist_id: str,
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> PlaylistDetail:
    record = await run_in_threadpool(store.get_playlist, playlists_dir, playlist_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return await _detail_response(record, handle)


@router.post("/playlists/{playlist_id}/tracks", response_model=PlaylistDetail)
async def add_tracks_endpoint(
    playlist_id: str,
    body: PlaylistAddTracksRequest,
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> PlaylistDetail:
    record = await run_in_threadpool(
        store.add_tracks,
        playlists_dir,
        playlist_id,
        track_ids=body.track_ids,
        position=body.position,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    await _export_playlist(record, handle)
    return await _detail_response(record, handle)


@router.delete("/playlists/{playlist_id}/entries/{entry_uid}", response_model=PlaylistDetail)
async def remove_entry_endpoint(
    playlist_id: str,
    entry_uid: str,
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> PlaylistDetail:
    record = await run_in_threadpool(store.get_playlist, playlists_dir, playlist_id)
    if record is None or all(e.uid != entry_uid for e in record.entries):
        raise HTTPException(status_code=404, detail="Playlist entry not found")
    record = await run_in_threadpool(store.remove_entry, playlists_dir, playlist_id, entry_uid)
    if record is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    await _export_playlist(record, handle)
    return await _detail_response(record, handle)


@router.patch("/playlists/{playlist_id}/entries/{entry_uid}", response_model=PlaylistDetail)
async def resolve_entry_endpoint(
    playlist_id: str,
    entry_uid: str,
    body: PlaylistResolveEntryRequest,
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> PlaylistDetail:
    """Point an entry at a library track — resolves a pending row (or
    re-points a resolved one) while keeping its position."""
    record = await run_in_threadpool(store.get_playlist, playlists_dir, playlist_id)
    if record is None or all(e.uid != entry_uid for e in record.entries):
        raise HTTPException(status_code=404, detail="Playlist entry not found")
    if not await run_in_threadpool(item_exists, handle.lib, body.item_id):
        raise HTTPException(status_code=422, detail=f"unknown item ids: {body.item_id}")
    record = await run_in_threadpool(
        store.resolve_entry, playlists_dir, playlist_id, entry_uid, item_id=body.item_id
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Playlist entry not found")
    await _export_playlist(record, handle)
    return await _detail_response(record, handle)


@router.put("/playlists/{playlist_id}/tracks", response_model=PlaylistDetail)
async def reorder_tracks_endpoint(
    playlist_id: str,
    body: PlaylistReorderRequest,
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> PlaylistDetail:
    record = await run_in_threadpool(store.get_playlist, playlists_dir, playlist_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    known = {e.uid for e in record.entries}
    unknown = [uid for uid in body.entry_uids if uid not in known]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown entry uids: {', '.join(unknown)}")
    record = await run_in_threadpool(
        store.set_entry_order, playlists_dir, playlist_id, uids=body.entry_uids
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    await _export_playlist(record, handle)
    return await _detail_response(record, handle)


@router.post("/playlists/{playlist_id}/sync", response_model=PlaylistDetail)
async def sync_playlist_endpoint(
    playlist_id: str,
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
    handle: Annotated[LibraryHandle, Depends(get_library)],
    plex_store: Annotated[PlexConfigStore, Depends(get_plex_store)],
) -> PlaylistDetail:
    record = await run_in_threadpool(store.get_playlist, playlists_dir, playlist_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    config = plex_store.get()
    if not (config.base_url and config.token):
        raise HTTPException(status_code=409, detail="Connect Plex first")

    try:
        specs = await run_in_threadpool(_plex_specs_for, record, handle, config)
        states = await run_in_threadpool(
            plex_sync.sync_playlist_to_targets,
            config,
            record.name,
            specs,
            record.target_plex_users,
            playlist_id=record.id,
            rating_keys={target: state.rating_key for target, state in record.plex.items()},
        )
    except PlexNotConfigured as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PlexConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except OSError as exc:
        # Reading the library files to resolve paths failed — surface a clean
        # error instead of a raw 500.
        raise HTTPException(status_code=502, detail="Couldn't read the library files.") from exc

    # Remove the playlist from any user that was previously synced but is no
    # longer a target (unticked) — best-effort, so a cleanup hiccup never fails
    # the sync. `record` still holds the PRE-sync plex state map.
    removed = sorted(set(record.plex) - {"admin"} - set(record.target_plex_users))
    removed_keys = {uid: record.plex[uid].rating_key for uid in removed}
    await _best_effort_plex_delete(
        config, removed_keys, f"de-target {playlist_id}", playlist_id=record.id
    )

    now = datetime.now(UTC).isoformat()
    states = {key: state.model_copy(update={"synced_at": now}) for key, state in states.items()}
    try:
        record = await run_in_threadpool(
            store.replace_plex_states, playlists_dir, playlist_id, states
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Failed to record sync state.") from exc
    if record is None:  # deleted mid-flight
        raise HTTPException(status_code=404, detail="Playlist not found")
    return await _detail_response(record, handle)


def _plex_specs_for(
    record: StoredPlaylist, handle: LibraryHandle, config: PlexConfig
) -> list[PlexTrackSpec]:
    beets_root = os.fsdecode(handle.lib.directory)
    refs: list[TrackRef] = track_match_refs(handle.lib, record.resolved_item_ids)
    return [
        PlexTrackSpec(
            path=translate_path(r.abs_path, beets_root, config.library_path),
            albumartist=r.albumartist,
            album=r.album,
            title=r.title,
            track=r.track,
        )
        for r in refs
    ]


@router.patch("/playlists/{playlist_id}", response_model=Playlist)
async def update_playlist_endpoint(
    playlist_id: str,
    body: PlaylistUpdateRequest,
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> Playlist:
    record = await run_in_threadpool(
        store.update_playlist,
        playlists_dir,
        playlist_id,
        name=body.name,
        description=body.description,
        target_plex_users=body.target_plex_users,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    await _export_playlist(record, handle)
    return _to_playlist(record)


@router.delete("/playlists/{playlist_id}", status_code=204)
async def delete_playlist_endpoint(
    playlist_id: str,
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
    handle: Annotated[LibraryHandle, Depends(get_library)],
    plex_store: Annotated[PlexConfigStore, Depends(get_plex_store)],
) -> Response:
    # Read the record first so we know which Plex accounts (+ ratingKeys) it was
    # synced to before the owned record is gone. `delete_playlist` stays the 404
    # authority, so a present-but-unreadable record is still removable (we just
    # skip the Plex cascade, since its targets are then unknown).
    record = await run_in_threadpool(store.get_playlist, playlists_dir, playlist_id)
    deleted = await run_in_threadpool(store.delete_playlist, playlists_dir, playlist_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Playlist not found")
    await _remove_export(playlist_id, handle)
    if record is not None:
        rating_keys = {target: state.rating_key for target, state in record.plex.items()}
        await _best_effort_plex_delete(
            plex_store.get(), rating_keys, f"delete {playlist_id}", playlist_id=record.id
        )
    return Response(status_code=204)


@router.post("/playlists/import/preview", response_model=PlaylistImportPreviewResponse)
async def import_preview_endpoint(
    body: PlaylistImportPreviewRequest,
    handle: Annotated[LibraryHandle, Depends(get_library)],
    plex_store: Annotated[PlexConfigStore, Depends(get_plex_store)],
) -> PlaylistImportPreviewResponse:
    """Parse/pull the source playlists and match every entry. Read-only:
    nothing is stored; the uploaded content never touches disk."""
    if body.files is not None:
        parsed = [parse_m3u(f.name, f.content) for f in body.files]
    else:
        try:
            parsed = await run_in_threadpool(
                playlists_pull.pull_playlist_entries,
                plex_store.get(),
                body.plex_playlists or [],
            )
        except PlexNotConfigured as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PlexConnectionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    def _match_all() -> list[PlaylistImportPreview]:
        index = build_match_index(handle.lib)  # ONE scan for the whole request
        previews: list[PlaylistImportPreview] = []
        for playlist in parsed:
            entries = match_entries(index, playlist.entries)
            previews.append(
                PlaylistImportPreview(
                    name=playlist.name,
                    entries=entries,
                    matched_count=sum(e.status == "matched" for e in entries),
                    ambiguous_count=sum(e.status == "ambiguous" for e in entries),
                    unmatched_count=sum(e.status == "unmatched" for e in entries),
                )
            )
        return previews

    return PlaylistImportPreviewResponse(playlists=await run_in_threadpool(_match_all))


def _unique_name(name: str, taken: set[str]) -> str:
    if name not in taken:
        return name
    n = 2
    while f"{name} ({n})" in taken:
        n += 1
    return f"{name} ({n})"


@router.post("/playlists/import", response_model=PlaylistImportResponse)
async def import_commit_endpoint(
    body: PlaylistImportRequest,
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> PlaylistImportResponse:
    """Create the playlists exactly as reviewed — resolved entries as tracks,
    unresolved ones as position-holding pending rows."""
    wanted_ids = sorted(
        {e.item_id for pl in body.playlists for e in pl.entries if e.item_id is not None}
    )

    def _missing() -> list[int]:
        return [item_id for item_id in wanted_ids if not item_exists(handle.lib, item_id)]

    missing = await run_in_threadpool(_missing)
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"unknown item ids: {', '.join(str(i) for i in missing)}",
        )

    existing = await run_in_threadpool(store.list_playlists, playlists_dir)
    taken = {record.name for record in existing}
    created: list[Playlist] = []
    for playlist in body.playlists:
        name = _unique_name(playlist.name.strip() or "Imported playlist", taken)
        taken.add(name)
        entries = [
            StoredEntry(uid=uuid.uuid4().hex, item_id=e.item_id, pending=e.pending)
            for e in playlist.entries
        ]
        record = await run_in_threadpool(
            store.create_playlist,
            playlists_dir,
            name=name,
            description=playlist.description,
            entries=entries,
        )
        await _export_playlist(record, handle)
        created.append(_to_playlist(record))
    return PlaylistImportResponse(created=created)
