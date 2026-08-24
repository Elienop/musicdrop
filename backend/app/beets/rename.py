"""Artist-rename adapter: the album edit fanned across every album of an artist.

One new orchestration over machinery that already ships: per album it calls
``preview_album_edit`` / ``apply_album_edit`` (no second mover, no second
tag-writer, no second collision predicate), selected by the exact
``albumartist`` equality the artist page's album list uses. Only
``album_artist`` is edited; per-track ``artist`` tags are never touched
(owner decision — they must keep matching source metadata for lyrics lookups).

Batch shape mirrors duplicates resolve-all: the op takes the swap lock ONCE,
each album applies in its own transaction, a drifted or failing album is
recorded and never aborts the rest.
"""

from __future__ import annotations

from dataclasses import dataclass

from beets.library import Library
from fastapi import Request

from app.beets.edit import AlbumNotFoundError, apply_album_edit, preview_album_edit
from app.beets.library import _coerce_str, _require_id
from app.models.edit import AlbumEditRequest, AlbumFieldEdits
from app.models.rename import (
    ArtistRenameAlbumPreview,
    ArtistRenameAlbumResult,
    ArtistRenameMergeInfo,
    ArtistRenamePreview,
    ArtistRenameRequest,
)


class ArtistNotFoundError(Exception):
    """The named artist has no albums. Maps to 404."""


def _artist_albums(lib: Library, name: str) -> list[tuple[int, str]]:
    """(album_id, title) for every album whose albumartist is EXACTLY ``name``.

    Raw, case-sensitive equality — the same comparison ``list_albums``'
    ``?artist=`` filter applies — so the fan-out set is precisely the album
    list the artist page shows. (``delete_artist`` strips both sides; the
    rename must not, or the preview could cover albums the page does not.)
    """
    return [
        (_require_id(a.id), _coerce_str(a.album))
        for a in lib.albums()
        if _coerce_str(a.albumartist) == name
    ]


def _edit_request(new_name: str) -> AlbumEditRequest:
    return AlbumEditRequest(album=AlbumFieldEdits(album_artist=new_name))


def preview_artist_rename(
    lib: Library, *, request: ArtistRenameRequest, move_enabled: bool
) -> ArtistRenamePreview:
    """Fan the single-album preview across the artist. Persists nothing."""
    with lib.music_dir_context():
        targets = _artist_albums(lib, request.name)
        if not targets:
            raise ArtistNotFoundError(f"artist {request.name!r} not found")
        existing = len(_artist_albums(lib, request.new_name))
        edit = _edit_request(request.new_name)
        rows: list[ArtistRenameAlbumPreview] = []
        for album_id, title in targets:
            try:
                p = preview_album_edit(
                    lib, album_id=album_id, request=edit, move_enabled=move_enabled
                )
            except AlbumNotFoundError:
                # Vanished since the snapshot (concurrent delete — the preview
                # holds no lock): no longer part of this artist, drop it rather
                # than abort the whole preview.
                continue
            rows.append(
                ArtistRenameAlbumPreview(
                    album_id=album_id,
                    title=title,
                    move_count=len(p.move_plan),
                    refusals=p.move_refusals,
                )
            )
        return ArtistRenamePreview(
            name=request.name,
            new_name=request.new_name,
            move_enabled=move_enabled,
            albums=rows,
            merge=(ArtistRenameMergeInfo(existing_album_count=existing) if existing else None),
        )


@dataclass(frozen=True)
class ArtistRenameApplyOutcome:
    """What the adapter did. The API layer composes the wire result on top:
    portrait re-key, playlist re-export and the art-job kick are not beets
    concerns and stay out of this module."""

    albums: list[ArtistRenameAlbumResult]
    old_name_remaining_albums: int
    moved_item_ids: list[int]


def apply_artist_rename(
    lib: Library, *, request: ArtistRenameRequest, write: bool, move: bool
) -> ArtistRenameApplyOutcome:
    """Apply the rename album by album. One transaction per album (inside
    ``apply_album_edit``); a drifted album is skipped and recorded; one album's
    failure never aborts the rest (the resolve-all batch discipline)."""
    with lib.music_dir_context():
        # Snapshot ids BEFORE mutating: renaming changes the very field the
        # selection matches on (the delete_artist lesson).
        targets = _artist_albums(lib, request.name)
        if not targets:
            raise ArtistNotFoundError(f"artist {request.name!r} not found")
        edit = _edit_request(request.new_name)
        results: list[ArtistRenameAlbumResult] = []
        moved_item_ids: list[int] = []
        for album_id, title in targets:
            album = lib.get_album(album_id)
            if album is None or _coerce_str(album.albumartist) != request.name:
                results.append(
                    ArtistRenameAlbumResult(
                        album_id=album_id,
                        title=title,
                        outcome="skipped_drifted",
                        error="album no longer belongs to this artist",
                    )
                )
                continue
            try:
                res = apply_album_edit(lib, album_id=album_id, request=edit, write=write, move=move)
            except AlbumNotFoundError:
                # Vanished between the drift check and the re-fetch — same
                # verdict as the preview's drop, not a "failed".
                results.append(
                    ArtistRenameAlbumResult(
                        album_id=album_id,
                        title=title,
                        outcome="skipped_drifted",
                        error="album no longer exists",
                    )
                )
                continue
            except Exception as exc:  # isolate: report this album, continue the batch
                results.append(
                    ArtistRenameAlbumResult(
                        album_id=album_id, title=title, outcome="failed", error=str(exc)
                    )
                )
                continue
            moved_item_ids.extend(r.item_id for r in res.items if r.moved)
            results.append(
                ArtistRenameAlbumResult(
                    album_id=album_id,
                    title=title,
                    outcome="renamed",
                    write_failures=res.write_failures,
                    move_failures=res.move_failures,
                )
            )
        remaining = len(_artist_albums(lib, request.name))
        return ArtistRenameApplyOutcome(
            albums=results,
            old_name_remaining_albums=remaining,
            moved_item_ids=moved_item_ids,
        )


async def preview_artist_rename_op(
    request_obj: Request, payload: ArtistRenameRequest
) -> ArtistRenamePreview:
    """Read-only preview: no lock, no gate — mirrors ``preview_album_edit_op``."""
    from beets.ui import should_move
    from fastapi import HTTPException
    from fastapi.concurrency import run_in_threadpool

    handle = request_obj.app.state.beets_library
    move_enabled = bool(should_move(None))
    try:
        return await run_in_threadpool(
            preview_artist_rename, handle.lib, request=payload, move_enabled=move_enabled
        )
    except ArtistNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def apply_artist_rename_op(
    request_obj: Request, payload: ArtistRenameRequest
) -> ArtistRenameApplyOutcome:
    """Gate (409) + the shared swap lock held ONCE for the whole batch +
    threadpool — the duplicates ``resolve_all_op`` shape."""
    from beets.ui import should_move, should_write
    from fastapi import HTTPException
    from fastapi.concurrency import run_in_threadpool

    from app.beets.config_editor import _swap_lock
    from app.library_busy import library_job_active

    app = request_obj.app
    if library_job_active():
        raise HTTPException(
            status_code=409,
            detail="A library operation is in progress; rename available when it finishes",
        )
    async with _swap_lock(app):
        handle = app.state.beets_library
        write = bool(should_write(None))
        move = bool(should_move(None))
        try:
            return await run_in_threadpool(
                apply_artist_rename, handle.lib, request=payload, write=write, move=move
            )
        except ArtistNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:  # structured 500 like the album edit
            raise HTTPException(
                status_code=500,
                detail={
                    "message": f"Rename failed: {exc}",
                    "recovery": (
                        "Albums already renamed keep the new name; reload and retry for the rest."
                    ),
                },
            ) from exc
