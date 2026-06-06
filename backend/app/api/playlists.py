"""CRUD endpoints for MusicDrop-owned playlists.

Thin router over ``app.playlists.store``. The store directory is resolved from
``settings`` (not ``app.state``) so it works under the lifespan-less ``client``
test fixture; tests drive it via the monkeypatched ``settings.beets_dir``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.concurrency import run_in_threadpool

from app.api.albums import get_library
from app.beets.library import LibraryHandle
from app.beets.playlists import m3u_entries, resolve_tracks
from app.config import settings
from app.models.playlist import (
    Playlist,
    PlaylistAddTracksRequest,
    PlaylistCreateRequest,
    PlaylistDetail,
    PlaylistReorderRequest,
    PlaylistUpdateRequest,
)
from app.playlists import store
from app.playlists.m3u import delete_m3u, write_m3u
from app.playlists.store import StoredPlaylist

router = APIRouter(tags=["playlists"])


def get_playlists_dir() -> Path:
    """Resolve the owned-playlist store dir from settings.

    Empty ``MUSICDROP_PLAYLISTS_DIR`` -> ``<beets_dir>/playlists``.
    """
    configured = settings.playlists_dir.strip()
    if configured:
        return Path(configured)
    return Path(settings.beets_dir) / "playlists"


def _to_playlist(record: StoredPlaylist) -> Playlist:
    return Playlist(
        id=record.id,
        name=record.name,
        description=record.description,
        track_count=len(record.track_ids),
        target_plex_users=record.target_plex_users,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


async def _detail_response(record: StoredPlaylist, handle: LibraryHandle) -> PlaylistDetail:
    tracks = await run_in_threadpool(resolve_tracks, handle.lib, record.track_ids)
    return PlaylistDetail(**_to_playlist(record).model_dump(), tracks=tracks)


def _export_dir(handle: LibraryHandle) -> Path:
    configured = settings.playlists_export_dir.strip()
    if configured:
        return Path(configured)
    return Path(os.fsdecode(handle.lib.directory)) / ".playlists"


def _render_export(record: StoredPlaylist, handle: LibraryHandle, export_dir: Path) -> None:
    entries = m3u_entries(handle.lib, record.track_ids, str(export_dir))
    write_m3u(export_dir / f"{record.id}.m3u8", record.name, entries)


async def _export_playlist(record: StoredPlaylist, handle: LibraryHandle) -> None:
    """Best-effort `.m3u8` (re)write. The owned store is the source of truth, so
    a filesystem hiccup never fails the mutation."""
    try:
        await run_in_threadpool(_render_export, record, handle, _export_dir(handle))
    except OSError:
        pass


async def _remove_export(playlist_id: str, handle: LibraryHandle) -> None:
    try:
        await run_in_threadpool(delete_m3u, _export_dir(handle) / f"{playlist_id}.m3u8")
    except OSError:
        pass


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


@router.delete("/playlists/{playlist_id}/tracks/{item_id}", response_model=PlaylistDetail)
async def remove_track_endpoint(
    playlist_id: str,
    item_id: int,
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> PlaylistDetail:
    record = await run_in_threadpool(store.remove_track, playlists_dir, playlist_id, item_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    await _export_playlist(record, handle)
    return await _detail_response(record, handle)


@router.put("/playlists/{playlist_id}/tracks", response_model=PlaylistDetail)
async def reorder_tracks_endpoint(
    playlist_id: str,
    body: PlaylistReorderRequest,
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> PlaylistDetail:
    record = await run_in_threadpool(
        store.set_track_order, playlists_dir, playlist_id, track_ids=body.track_ids
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    await _export_playlist(record, handle)
    return await _detail_response(record, handle)


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
) -> Response:
    deleted = await run_in_threadpool(store.delete_playlist, playlists_dir, playlist_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Playlist not found")
    await _remove_export(playlist_id, handle)
    return Response(status_code=204)
