"""Album/track tag-edit adapter — the beets write boundary.

All beets interaction for editing existing items lives here (CLAUDE.md rule 3).
The pure helpers mirror beets' ``modify`` semantics (see memory
``beets-edit-contract``): album-level fields fan to every track (``inherit``),
``item.try_write()`` writes tags in place via mutagen, ``item.move()`` relocates
files only when move is enabled and the path-format destination differs. Write
failures are captured per item (beets swallows them); move failures are captured
per item too (beets would roll back the whole batch). Type coercion uses beets'
own ``set_parse``; the request models have already rejected bad types up front.

A tag edit RENAMES files, so it can manufacture a filename collision the user
never asked for — and beets answers a taken destination by diverting the file to
a ``.N`` sibling in silence. The move phase therefore refuses a track whose
destination would divert (``reorganize.collisions_by_dest``, reused verbatim) and
reports that on the track's own row, leaving its tag write standing; and every
move that does happen carries the track's ``.lrc``/``.txt`` lyric sidecars, which
beets itself knows nothing about.

The PREVIEW runs that same pre-flight over the same input, so it splits its move
rows into the renames that will happen and the ones the apply will refuse, with
the same reason. One predicate, two surfaces — they cannot drift apart, and the
user does not learn about a refusal only after pressing Apply.

Off-main-thread safety: every op binds ``lib.music_dir_context()`` because beets
stores DB paths relative to the library dir and converts them in both directions
via a ``ContextVar`` that a FastAPI threadpool thread does not inherit (memory
``beets-read-write-concurrency-gap``).
"""

from __future__ import annotations

import os
from typing import Any

from beets.library import Library
from beets.util import MoveOperation, syspath
from fastapi import Request

from app.beets.library import _album_genre, _genre_values, _require_id

# An edit that renames a file performs the SAME move reorganize does, under a
# different trigger — so it reuses reorganize's divert prediction and its sidecar
# carry rather than growing second copies that would drift apart.
from app.beets.reorganize import carry_sidecars, collisions_by_dest
from app.models.edit import (
    AlbumDiffSide,
    AlbumEditPreview,
    AlbumEditRequest,
    AlbumEditResult,
    EditTrackChange,
    ItemWriteResult,
    TrackMoveRefusal,
    TrackPathChange,
)

# Request field name -> beets field name. ``genre`` maps to beets 2.13's
# multi-valued ``genres``: the single-valued ``genre`` field was dropped, so
# edits written under that name landed in a flex key nothing reads.
_ALBUM_FIELD_MAP: dict[str, str] = {
    "album_artist": "albumartist",
    "title": "album",
    "year": "year",
    "genre": "genres",
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
    """Beets-named album fields to set (only the non-None request keys).

    ``genres`` is list-typed in beets 2.13, so the submitted display string
    ("Rock; Pop") is split HERE — by the same reader the whole app's genre reads
    go through, which is what makes read -> edit -> save -> read give back what
    the user typed.
    """
    out: dict[str, Any] = {}
    if request.album is not None:
        for ours, beets_name in _ALBUM_FIELD_MAP.items():
            value = getattr(request.album, ours)
            if value is None:
                continue
            out[beets_name] = _genre_values(value) if beets_name == "genres" else value
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


def _set_field(obj: Any, field: str, value: Any) -> None:
    """Set one beets field on an album or item from an edit value.

    ``set_parse`` is beets' own string -> field-type coercion, but it takes a
    STRING — and beets 2.13's ``genres`` holds a ``list``. A list is therefore
    assigned directly, letting the field's ``normalize`` take it as-is;
    ``str(["Rock", "Pop"])`` would have stored the Python repr as one genre.
    """
    if isinstance(value, list):
        obj[field] = value
    else:
        obj.set_parse(field, str(value))


def _apply_in_memory(
    album: Any,
    items: list[Any],
    album_edits: dict[str, Any],
    track_edits: dict[int, dict[str, Any]],
) -> None:
    """Set fields on the in-memory album + items (no store/write/move).

    Album fields are fanned to every item (mirrors beets ``inherit``: the four
    editable album fields are all item keys). Per-track fields then override on
    the specific items.
    """
    by_id = {int(it.id): it for it in items}
    for field, value in album_edits.items():
        _set_field(album, field, value)
        for item in items:
            _set_field(item, field, value)
    for item_id, fields in track_edits.items():
        item = by_id[item_id]
        for field, value in fields.items():
            _set_field(item, field, value)


def _album_side(album: Any, items: list[Any]) -> AlbumDiffSide:
    """One side of the album-header diff, read exactly as the app reads albums.

    Genre goes through ``_album_genre`` — the single resolver every other
    surface uses (Browse rows, album detail, duplicates), fallback to the first
    track included — so the panel can never show an empty Genre for an album
    whose detail page shows one. Consequence worth knowing: an album whose genre
    comes from its tracks and is saved back UNCHANGED materialises that genre at
    album level while ``changed_fields`` reports no change. That is the honest
    answer for the user, who is looking at a value that did not move.
    """

    def _s(value: Any) -> str | None:
        text = str(value).strip() if value is not None else ""
        return text or None

    year = int(album.year) if album.year else None
    return AlbumDiffSide(
        album_artist=_s(album.albumartist),
        title=_s(album.album),
        year=year,
        genre=_album_genre(album, items),
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
        _set_field(shadow, field, value)
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
        by_id = {_require_id(it.id): it for it in items}

        album_edits = _album_edits(request)
        track_edits = _track_edits(request)
        _validate_track_ids(request, by_id)

        before_album = _album_side(album, items)
        before_titles = {_require_id(it.id): str(it.title) for it in items}
        before_tracknums = {_require_id(it.id): int(it.track or 0) for it in items}
        before_artists = {_require_id(it.id): str(it.artist) for it in items}
        before_paths = {_require_id(it.id): _abs(it) for it in items}

        _apply_in_memory(album, items, album_edits, track_edits)

        after_album = _album_side(album, items)
        changed_fields = [
            name
            for name in ("album_artist", "title", "year", "genre")
            if getattr(before_album, name) != getattr(after_album, name)
        ]

        track_rows: list[EditTrackChange] = []
        for item in items:
            iid = _require_id(item.id)
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
        move_refusals: list[TrackMoveRefusal] = []
        if move_enabled:
            # ``item.destination()`` resolves album-level path fields (e.g.
            # ``$albumartist``) from ``item._cached_album``, which beets reloads
            # from the DB on access — discarding our in-memory album edits. Bind
            # a clean, revision-aligned shadow album carrying the edited fields so
            # the move plan reflects album-header changes (see beets 2.11
            # ``Item._cached_album`` / ``Model.load`` early-exit semantics).
            # The apply gets away without this because it has already STORED the
            # edits before its move phase runs; the preview stores nothing.
            shadow = _shadow_album(lib, album_id, album_edits)
            if shadow is not None:
                for item in items:
                    item._cached_album = shadow
            # Destinations stay BYTES here — the shared collision predicate keys on
            # them — and are computed once per item for both the pre-flight and the
            # rows below. Filtered to INSIDE-library tracks up front: apply's move
            # phase skips outside files entirely, so a plan row for one would
            # promise a move that never happens.
            dests = [
                (it, bytes(it.destination(basedir=lib.directory)))
                for it in items
                if _inside_library(lib, it)
            ]
            # The same pre-flight the apply runs, over the same input: every
            # inside-library track, including the ones whose path does not change,
            # because a track sitting on its name is exactly what makes a mate's
            # rename divert. Read-only — it stats paths and queries, nothing else.
            refusals = _move_refusals(lib, dests)
            for item, dest in dests:
                iid = _require_id(item.id)
                new_path = os.fsdecode(dest)
                old_path = before_paths[iid]
                # Ordered as the apply orders it: a track already sitting on its
                # destination is not moving, so it is never refused either — even
                # when it is the mate whose name a refused rename collides with.
                if new_path == old_path:
                    continue
                detail = refusals.get(iid)
                if detail is None:
                    move_plan.append(
                        TrackPathChange(
                            item_id=iid,
                            track=int(item.track or 0),
                            old_path=old_path,
                            new_path=new_path,
                        )
                    )
                else:
                    move_refusals.append(
                        TrackMoveRefusal(
                            item_id=iid,
                            track=int(item.track or 0),
                            old_path=old_path,
                            new_path=new_path,
                            detail=detail,
                        )
                    )

        return AlbumEditPreview(
            changed_fields=changed_fields,
            album_before=before_album,
            album_after=after_album,
            tracks=track_rows,
            move_enabled=move_enabled,
            move_plan=move_plan,
            move_refusals=move_refusals,
        )


def _inside_library(lib: Library, item: Any) -> bool:
    """True iff the item's file lives under the library dir.

    Mirrors beets ``Item.try_sync``'s guard: a file outside the library is never
    relocated, so it takes no part in the move phase at all — not even in the
    collision pre-flight, whose whole subject is names inside the library.
    """
    current = os.path.abspath(os.fsdecode(item.path))
    libdir = os.path.abspath(os.fsdecode(lib.directory))
    return os.path.commonpath([current, libdir]) == libdir


def _move_refusals(lib: Library, dests: list[tuple[Any, bytes]]) -> dict[int, str]:
    """item id -> why its move must be refused, for every track whose destination
    would make beets divert the file to a ``.N`` sibling.

    The prediction itself is ``reorganize.collisions_by_dest`` — the ONE
    implementation of it (two tracks computing one name; a name already held on
    disk by anything but a path this same batch vacates). All this adds is the
    per-track attribution the edit API reports through, since reorganize refuses
    whole units while an edit answers for each track on its own row.

    Refusing one track can un-exempt another: a destination held by a batch-mate
    is allowed only because that mate relocates and vacates it, and a refused mate
    no longer does. So the prediction is re-run over the survivors until it comes
    back clean. It terminates because every key it returns IS one of the passed
    destinations, so each pass refuses at least one track.
    """
    refusals: dict[int, str] = {}
    allowed = list(dests)
    while allowed:
        collisions = collisions_by_dest(lib, allowed)
        if not collisions:
            break
        survivors: list[tuple[Any, bytes]] = []
        for item, dest in allowed:
            collision = collisions.get(os.path.normpath(dest))
            if collision is None:
                survivors.append((item, dest))
            else:
                refusals[_require_id(item.id)] = collision.detail
        allowed = survivors
    return refusals


def _move_item(lib: Library, item: Any, dest: bytes) -> str | None:
    """Relocate one item's file and carry its lyric sidecars along.

    Returns a problem string when the file did NOT land on ``dest`` (beets
    diverted it, i.e. the name was taken between the pre-flight and here), else
    None. Real I/O failures raise; the caller reports them per track.

    ``store=False`` because the caller stores once per item after write+move;
    ``with_album=False`` so the album art is NOT moved per item — that discards
    the transient album's updated ``artpath``, stranding the cover. The caller
    relocates the art once after the move phase (mirrors beets' ``update_items``).
    """
    old_path = bytes(item.path)
    if not os.path.exists(syspath(old_path)):
        # beets 2.12+'s item.move() logs "file not found, skipping" and returns
        # WITHOUT raising when the source is gone (2.11 raised). Surface it as the
        # move failure it is, so the caller reports it (a real I/O error during
        # the move below — permission, disk — still raises as before).
        raise FileNotFoundError(f"source file is missing: {os.fsdecode(old_path)}")
    item.move(basedir=lib.directory, store=False, with_album=False)
    landed = bytes(item.path)
    # Keyed off the ACTUAL landing, never the computed destination: a diverted
    # file must take its own lyrics with it.
    carry_sidecars(lib, old_path, landed)
    if landed != dest:
        return (
            "the computed file name was already taken on disk, so the file landed at "
            f"{os.path.basename(os.fsdecode(landed))!r}"
            "; check this album's folder for what is holding that name"
        )
    return None


def _move_items(lib: Library, items: list[Any]) -> tuple[set[int], dict[int, str]]:
    """Relocate every track whose destination differs. Returns the ids that moved
    and, per track, what went wrong — the caller turns those into per-track errors.

    The pre-flight runs over the WHOLE batch before the first file moves, because
    both of its arms need the pre-move picture: a track that is already sitting on
    its destination still holds that name (and would make a second track's rename
    divert), and a track that is relocating vacates the one it holds.

    Two passes. A batch-mate only frees the name it holds once it has itself
    moved, so the first mover of a swap — two tracks renamed into each other's
    current names — necessarily meets an occupied destination and beets diverts it
    to a ``.N`` sibling. After one full pass every mover has left its original
    name, so a single retry settles the cycle; a track still off its destination
    after that is held by something that will not vacate and is reported.

    A mate whose move RAISED never vacated its name, so the retry pass drops it
    from the pre-flight entirely (``stuck``): its file then reads as a plain
    on-disk occupant and the tracks that were counting on it are refused instead
    of re-diverted — without this, every apply renamed the diverted file AGAIN
    (``.1`` -> ``.2``) for as long as the mate kept failing.
    """
    moved: set[int] = set()
    problems: dict[int, str] = {}
    stuck: set[int] = set()  # moves that raised; their files never vacated
    inside = [it for it in items if _inside_library(lib, it)]
    queue = inside
    for _attempt in (1, 2):
        active = [it for it in inside if _require_id(it.id) not in stuck]
        dests = [(it, bytes(it.destination(basedir=lib.directory))) for it in active]
        refusals = _move_refusals(lib, dests)
        wanted = {_require_id(it.id): dest for it, dest in dests}
        diverted: list[Any] = []
        for item in queue:
            iid = _require_id(item.id)
            dest = wanted[iid]
            if bytes(item.path) == dest:
                continue  # already where it belongs; nothing to move, nothing to refuse
            if iid in refusals:
                # A file that already moved this batch was diverted doing so; its
                # "landed at" problem is the truthful row — refusing the retry
                # must not overwrite it with a contradictory "move refused".
                if iid not in moved:
                    problems[iid] = f"move refused: {refusals[iid]}"
                continue
            try:
                problem = _move_item(lib, item, dest)
            except Exception as exc:  # report, do not abort the batch
                problems[iid] = f"move failed: {exc}"
                stuck.add(iid)
                continue
            moved.add(iid)
            if problem is None:
                problems.pop(iid, None)  # the retry settled the first pass's divert
            else:
                problems[iid] = problem
                diverted.append(item)
        queue = diverted
        if not queue:
            break
    return moved, problems


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
    failure neither silently rolls back the album nor hides which file failed —
    including the track whose tags were written while its move was refused
    (``_move_items``), which is a partial success and says so on its own row.
    """
    from app.beets.library import get_album_detail

    with lib.music_dir_context():
        album = lib.get_album(album_id)
        if album is None:
            raise AlbumNotFoundError(f"album {album_id} not found")
        items = sorted(album.items(), key=lambda it: (int(it.disc or 0), int(it.track or 0)))
        by_id = {_require_id(it.id): it for it in items}

        album_edits = _album_edits(request)
        track_edits = _track_edits(request)
        _validate_track_ids(request, by_id)

        write_failures = 0
        written: set[int] = set()
        moved: set[int] = set()
        move_problems: dict[int, str] = {}
        # Collect every failure per item: write and move are independent, so a
        # track can fail both. A single error slot would let the move error
        # clobber the write error.
        errors: dict[int, list[str]] = {_require_id(it.id): [] for it in items}
        with lib.transaction():
            _apply_in_memory(album, items, album_edits, track_edits)
            album.store(inherit=False)  # we fanned album fields to items manually
            if write:
                for item in items:
                    iid = _require_id(item.id)
                    if bool(item.try_write()):
                        written.add(iid)
                    else:
                        write_failures += 1
                        errors[iid].append("tag write failed")
            if move:
                # Moves are their own phase, after every tag write: the collision
                # pre-flight has to see the whole batch's destinations — and the
                # names it is about to vacate — before the first file relocates.
                moved, move_problems = _move_items(lib, items)
                for iid, problem in move_problems.items():
                    errors[iid].append(problem)
            for item in items:
                item.store()
            # Relocate the album art ONCE, after the items have moved+stored (so
            # art_destination reads their new dir), then persist the new artpath.
            # Per-item with_album=True would have moved the art but dropped the
            # artpath update, stranding the cover at the pruned old folder.
            if moved:
                album.move_art(MoveOperation.MOVE)
                album.store(inherit=False)

        results: list[ItemWriteResult] = []
        for item in items:
            iid = _require_id(item.id)
            results.append(
                ItemWriteResult(
                    item_id=iid,
                    track=int(item.track or 0),
                    title=str(item.title),
                    written=iid in written,
                    moved=iid in moved,
                    error="; ".join(errors[iid]) or None,
                )
            )

        detail = get_album_detail(lib, album_id)
        assert detail is not None  # the album still exists; we just edited it
        return AlbumEditResult(
            album=detail,
            items=results,
            write_failures=write_failures,
            # A refused move is a move failure: the file did not go where the
            # edited tags say it belongs, and the user has to act on it.
            move_failures=len(move_problems),
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
