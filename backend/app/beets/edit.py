"""Album/track tag-edit adapter — the beets write boundary.

All beets interaction for editing existing items lives here (CLAUDE.md rule 3).
The pure helpers mirror beets' ``modify`` semantics (see memory
``beets-edit-contract``): album-level fields fan to every track (``inherit``),
``item.try_write()`` writes tags in place via mutagen, ``item.move()`` relocates
files only when move is enabled and the path-format destination differs. Write
failures are captured per item (beets swallows them); move failures are captured
per item too (beets would roll back the whole batch). Type coercion uses beets'
own ``set_parse``; the request models have already rejected bad types up front.

Off-main-thread safety: every op binds ``lib.music_dir_context()`` because beets
2.11 stores DB paths relative to the library dir and re-expands them via a
``ContextVar`` that a FastAPI threadpool thread does not inherit (memory
``beets-read-write-concurrency-gap``).
"""

from __future__ import annotations

import os
from typing import Any

from beets.library import Library
from fastapi import Request

from app.models.edit import (
    AlbumDiffSide,
    AlbumEditPreview,
    AlbumEditRequest,
    AlbumEditResult,
    EditTrackChange,
    ItemWriteResult,
    TrackPathChange,
)

# Request field name -> beets field name.
_ALBUM_FIELD_MAP: dict[str, str] = {
    "album_artist": "albumartist",
    "title": "album",
    "year": "year",
    "genre": "genre",
}
_TRACK_FIELD_MAP: dict[str, str] = {
    "title": "title",
    "track": "track",
    "artist": "artist",
}


class AlbumNotFoundError(Exception):
    """A referenced album id is not in the library. Maps to 404."""


class ForeignTrackError(Exception):
    """A track id in the request does not belong to the album. Maps to 422."""


def _album_edits(request: AlbumEditRequest) -> dict[str, Any]:
    """Beets-named album fields to set (only the non-None request keys)."""
    out: dict[str, Any] = {}
    if request.album is not None:
        for ours, beets_name in _ALBUM_FIELD_MAP.items():
            value = getattr(request.album, ours)
            if value is not None:
                out[beets_name] = value
    return out


def _track_edits(request: AlbumEditRequest) -> dict[int, dict[str, Any]]:
    """Map item_id -> {beets_field: value} for the non-None per-track keys."""
    out: dict[int, dict[str, Any]] = {}
    for edit in request.tracks:
        fields: dict[str, Any] = {}
        for ours, beets_name in _TRACK_FIELD_MAP.items():
            value = getattr(edit, ours)
            if value is not None:
                fields[beets_name] = value
        if fields:
            out[edit.item_id] = fields
    return out


def _validate_track_ids(request: AlbumEditRequest, by_id: dict[int, Any]) -> None:
    """Raise ForeignTrackError if any requested item_id is not in the album.

    Covers every ``request.tracks[*].item_id`` — including no-op edits whose
    fields are all None — so a request referencing a nonexistent track is
    rejected (422) rather than silently ignored by ``_track_edits``.
    """
    for edit in request.tracks:
        if edit.item_id not in by_id:
            raise ForeignTrackError(f"track {edit.item_id} is not in album")


def _abs(item: Any) -> str:
    return os.fsdecode(item.path)


def _apply_in_memory(
    album: Any,
    items: list[Any],
    album_edits: dict[str, Any],
    track_edits: dict[int, dict[str, Any]],
) -> None:
    """Set fields on the in-memory album + items (no store/write/move).

    Album fields are fanned to every item (mirrors beets ``inherit``: the four
    editable album fields are all item keys). Per-track fields then override on
    the specific items. Uses beets' own ``set_parse`` for type coercion.
    """
    by_id = {int(it.id): it for it in items}
    for field, value in album_edits.items():
        album.set_parse(field, str(value))
        for item in items:
            item.set_parse(field, str(value))
    for item_id, fields in track_edits.items():
        item = by_id[item_id]
        for field, value in fields.items():
            item.set_parse(field, str(value))


def _album_side(album: Any) -> AlbumDiffSide:
    def _s(value: Any) -> str | None:
        text = str(value).strip() if value is not None else ""
        return text or None

    year = int(album.year) if album.year else None
    return AlbumDiffSide(
        album_artist=_s(album.albumartist),
        title=_s(album.album),
        year=year,
        genre=_s(album.get("genre")),
    )


def _shadow_album(lib: Library, album_id: int, album_edits: dict[str, Any]) -> Any:
    """A detached album copy carrying the edited fields, safe to read for paths.

    Returns ``None`` when there are no album-level edits (nothing to shadow). The
    copy is left clean and revision-aligned so beets' ``Item._cached_album``
    access short-circuits ``Model.load`` instead of reloading it from the DB and
    discarding the in-memory edits.
    """
    if not album_edits:
        return None
    shadow = lib.get_album(album_id)
    if shadow is None:
        return None
    for field, value in album_edits.items():
        shadow.set_parse(field, str(value))
    shadow.clear_dirty()
    shadow._revision = lib.revision
    return shadow


def preview_album_edit(
    lib: Library,
    *,
    album_id: int,
    request: AlbumEditRequest,
    move_enabled: bool,
) -> AlbumEditPreview:
    """Compute the field diff + (optional) move plan. Persists nothing.

    Applies the edits to in-memory copies only, so the mutations evaporate when
    this returns. The move plan calls ``item.destination()`` after the temp
    apply, so album- and track-field edits that change the path template are
    reflected.
    """
    with lib.music_dir_context():
        album = lib.get_album(album_id)
        if album is None:
            raise AlbumNotFoundError(f"album {album_id} not found")
        items = sorted(album.items(), key=lambda it: (int(it.disc or 0), int(it.track or 0)))
        by_id = {int(it.id): it for it in items}

        album_edits = _album_edits(request)
        track_edits = _track_edits(request)
        _validate_track_ids(request, by_id)

        before_album = _album_side(album)
        before_titles = {int(it.id): str(it.title) for it in items}
        before_tracknums = {int(it.id): int(it.track or 0) for it in items}
        before_artists = {int(it.id): str(it.artist) for it in items}
        before_paths = {int(it.id): _abs(it) for it in items}

        _apply_in_memory(album, items, album_edits, track_edits)

        after_album = _album_side(album)
        changed_fields = [
            name
            for name in ("album_artist", "title", "year", "genre")
            if getattr(before_album, name) != getattr(after_album, name)
        ]

        track_rows: list[EditTrackChange] = []
        for item in items:
            iid = int(item.id)
            t_before, t_after = before_titles[iid], str(item.title)
            n_before, n_after = before_tracknums[iid], int(item.track or 0)
            a_before, a_after = before_artists[iid], str(item.artist)
            if (t_before, n_before, a_before) != (t_after, n_after, a_after):
                track_rows.append(
                    EditTrackChange(
                        item_id=iid,
                        title_before=t_before,
                        title_after=t_after,
                        track_before=n_before,
                        track_after=n_after,
                        artist_before=a_before,
                        artist_after=a_after,
                    )
                )

        move_plan: list[TrackPathChange] = []
        if move_enabled:
            # ``item.destination()`` resolves album-level path fields (e.g.
            # ``$albumartist``) from ``item._cached_album``, which beets reloads
            # from the DB on access — discarding our in-memory album edits. Bind
            # a clean, revision-aligned shadow album carrying the edited fields so
            # the move plan reflects album-header changes (see beets 2.11
            # ``Item._cached_album`` / ``Model.load`` early-exit semantics).
            shadow = _shadow_album(lib, album_id, album_edits)
            for item in items:
                iid = int(item.id)
                if shadow is not None:
                    item._cached_album = shadow
                new_path = os.fsdecode(item.destination(basedir=lib.directory))
                old_path = before_paths[iid]
                if new_path != old_path:
                    move_plan.append(
                        TrackPathChange(
                            item_id=iid,
                            track=int(item.track or 0),
                            old_path=old_path,
                            new_path=new_path,
                        )
                    )

        return AlbumEditPreview(
            changed_fields=changed_fields,
            album_before=before_album,
            album_after=after_album,
            tracks=track_rows,
            move_enabled=move_enabled,
            move_plan=move_plan,
        )


def _maybe_move(lib: Library, item: Any) -> bool:
    """Relocate the file iff inside the library dir and its destination differs.

    Returns True when a move happened. Mirrors beets ``Item.try_sync``'s guard
    (only move files under the library directory). ``store=False`` because the
    caller stores once per item after write+move.
    """
    current = os.fsdecode(item.path)
    libdir = os.fsdecode(lib.directory)
    if os.path.commonpath([os.path.abspath(current), os.path.abspath(libdir)]) != os.path.abspath(
        libdir
    ):
        return False
    destination = os.fsdecode(item.destination(basedir=lib.directory))
    if destination == current:
        return False
    if not os.path.exists(current):
        # beets 2.12's item.move() logs "file not found, skipping" and returns
        # WITHOUT raising when the source is gone (2.11 raised). Surface it as the
        # move failure it is, so the caller reports it (a real I/O error during
        # the move below — permission, disk — still raises as before).
        raise FileNotFoundError(f"source file is missing: {current}")
    item.move(basedir=lib.directory, store=False)
    return True


def apply_album_edit(
    lib: Library,
    *,
    album_id: int,
    request: AlbumEditRequest,
    write: bool,
    move: bool,
) -> AlbumEditResult:
    """Apply album + per-track edits: write tags, optionally move, store.

    Album fields fan to every track (beets ``inherit``); per-track fields
    override. Each track's write and move are isolated and reported, so one
    failure neither silently rolls back the album nor hides which file failed.
    """
    from app.beets.library import get_album_detail

    with lib.music_dir_context():
        album = lib.get_album(album_id)
        if album is None:
            raise AlbumNotFoundError(f"album {album_id} not found")
        items = sorted(album.items(), key=lambda it: (int(it.disc or 0), int(it.track or 0)))
        by_id = {int(it.id): it for it in items}

        album_edits = _album_edits(request)
        track_edits = _track_edits(request)
        _validate_track_ids(request, by_id)

        results: list[ItemWriteResult] = []
        write_failures = 0
        move_failures = 0
        with lib.transaction():
            _apply_in_memory(album, items, album_edits, track_edits)
            album.store(inherit=False)  # we fanned album fields to items manually
            for item in items:
                written = False
                moved = False
                # Collect every failure for this item: write and move are
                # independent, so a track can fail both. A single error slot
                # would let the move error clobber the write error.
                errors: list[str] = []
                if write:
                    written = bool(item.try_write())
                    if not written:
                        write_failures += 1
                        errors.append("tag write failed")
                if move:
                    try:
                        moved = _maybe_move(lib, item)
                    except Exception as exc:  # report, do not abort the batch
                        move_failures += 1
                        errors.append(f"move failed: {exc}")
                item.store()
                results.append(
                    ItemWriteResult(
                        item_id=int(item.id),
                        track=int(item.track or 0),
                        title=str(item.title),
                        written=written,
                        moved=moved,
                        error="; ".join(errors) if errors else None,
                    )
                )

        detail = get_album_detail(lib, album_id)
        assert detail is not None  # the album still exists; we just edited it
        return AlbumEditResult(
            album=detail,
            items=results,
            write_failures=write_failures,
            move_failures=move_failures,
        )


async def preview_album_edit_op(
    request_obj: Request, album_id: int, payload: AlbumEditRequest
) -> AlbumEditPreview:
    """Read-only preview: no lock, no import gate. Resolves move from config."""
    from beets.ui import should_move
    from fastapi import HTTPException
    from fastapi.concurrency import run_in_threadpool

    handle = request_obj.app.state.beets_library
    move_enabled = bool(should_move(None))
    try:
        return await run_in_threadpool(
            preview_album_edit,
            handle.lib,
            album_id=album_id,
            request=payload,
            move_enabled=move_enabled,
        )
    except AlbumNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ForeignTrackError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def apply_album_edit_op(
    request_obj: Request, album_id: int, payload: AlbumEditRequest
) -> AlbumEditResult:
    """Apply: import-gate (409) + shared swap-lock + threadpool, like duplicates."""
    from beets.ui import should_move, should_write
    from fastapi import HTTPException
    from fastapi.concurrency import run_in_threadpool

    from app.beets.config_editor import _swap_lock
    from app.library_busy import library_job_active

    app = request_obj.app
    if library_job_active():
        raise HTTPException(
            status_code=409,
            detail="A library operation is in progress; edit available when it finishes",
        )
    async with _swap_lock(app):
        handle = app.state.beets_library
        write = bool(should_write(None))
        move = bool(should_move(None))
        try:
            return await run_in_threadpool(
                apply_album_edit,
                handle.lib,
                album_id=album_id,
                request=payload,
                write=write,
                move=move,
            )
        except AlbumNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ForeignTrackError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # surface a structured 500 like config Apply
            raise HTTPException(
                status_code=500,
                detail={
                    "message": f"Edit failed: {exc}",
                    "recovery": "Your library is unchanged for failed tracks; reload and retry.",
                },
            ) from exc
