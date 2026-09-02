# backend/app/api/reorganize.py
"""Reorganize Library endpoints: dry-run preview + the single-slot move job.

Scope is carried by route + ``?artist=`` query (library = no query; artist =
?artist=NAME; album = nested under /albums/{id}). One global registry serves all
three — only one reorganize at a time — and it is mutually exclusive with every
other library write (see _gate_busy + the gate sites in edit/cover/config/
duplicates/import/lyrics/artists)."""

import os
from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool

from app.api.albums import get_library
from app.beets.config_editor import _settings
from app.beets.library import LibraryHandle, album_exists
from app.beets.reorganize import album_scope_label, plan_reorganize
from app.beets.trash import resolve_trash_dir, resolve_trash_origins_dir
from app.events.emit import emit_library_changed
from app.library_busy import raise_if_library_busy
from app.models.errors import ErrorDetail
from app.models.reorganize import ReorganizeBackfillStatus, ReorganizePlan, ReorganizeScope
from app.playlists.store import get_playlists_dir
from app.reorganize_jobs.registry import (
    ReorganizeRegistry,
    get_reorganize_backfill,
)
from app.reorganize_jobs.runner import start_backfill

router = APIRouter(tags=["reorganize"])

_BUSY = "A library operation is in progress; reorganize available when it finishes"

#: Every status in this file is spelled ``status.HTTP_*``, which is why
#: SonarQube python:S8415 (integer literals only) never flagged the router.
#: Each entry names a model so the ``{detail: str}`` body keeps a generated
#: type - see app/models/errors.py.
_REORGANIZE_BUSY_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": (
        "A reorganize is already running, or another library job or a beets swap holds the library."
    ),
}
_ALBUM_NOT_FOUND_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": "No album has that id.",
}


def _gate_busy(app: object) -> None:
    # Excludes reorganize's own slot — this is reorganize's start-gate; the
    # single-slot check stays at ``reg.start`` (RuntimeError -> 409).
    raise_if_library_busy(app, exclude=("reorganize",), message=_BUSY)


def _trash_dir(app: object) -> Path:
    """The configured Trash dir for the orphan sweep (resolved like the trash API)."""
    handle: LibraryHandle = app.state.beets_library  # type: ignore[attr-defined]  # app duck-typed (object)
    return resolve_trash_dir(_settings(app), handle)  # type: ignore[arg-type]  # app duck-typed (object)


def _origins_dir(app: object) -> Path:
    """The Trash origin store, resolved like the trash API. Its sibling."""
    handle: LibraryHandle = app.state.beets_library  # type: ignore[attr-defined]  # app duck-typed (object)
    return resolve_trash_origins_dir(_settings(app), handle)  # type: ignore[arg-type]  # app duck-typed (object)


def _ignore_dirs(app: object) -> tuple[Path, ...]:
    """Dirs under the music root the orphan sweep must never trash — the resolved
    playlists export dir (defaults to <music>/.playlists) and the Trash origin store.
    Dotdirs/NAS dirs are handled name-based in the scanner; this covers a configured
    non-dotfile export dir.

    The origin store earns its place the moment it stops being a sidecar: it is a
    non-dotfile directory holding only ``.json`` files, so it is audio-empty BY
    DEFINITION, and a ``trash_origins_dir`` configured under the music root reads
    as a husk — the sweep would relocate the whole store into Trash, taking every
    row's exact restore with it in one pass. The Trash dir itself is already
    excluded by ``find_orphan_folders``; this one is new because it is no longer
    inside it.

    That covers a sweep reaching the store DIRECTLY, and only that. Excluding a
    directory does not protect it from a sweep that takes its PARENT:
    ``find_orphan_folders`` reports the TOP-MOST audio-empty dir, and an excluded
    subtree is not counted as audio for the dir above it. Measured with
    ``beets_dir`` itself under the music root — the sweep returns ``beets_dir``,
    the same list with and without this exclusion. What happens NEXT depends on
    where Trash sits, and only one of the two loses anything:

    * **Default layout** (``trash_dir`` unset = ``<beets_dir>/trash``): the move
      is a directory into its own subtree, so ``shutil.move`` refuses it —
      ``Cannot move a directory '<music>/<beets>' into itself
      '<music>/<beets>/trash/<beets>'``. ``shutil.Error`` IS an ``OSError``, so
      ``reorganize_jobs.runner``'s per-folder ``except OSError: continue``
      swallows it on every sweep. ``library.db``, ``config.yaml`` and the store
      all survive; the visible residue is an empty ``<beets_dir>/trash`` the
      mover created before failing, and a report that counts no orphan.
    * **``MUSICDROP_TRASH_DIR`` pointing OUTSIDE ``beets_dir``**: the move
      succeeds and all three land under Trash. ``beets_dir`` is then recreated
      as an empty shell holding one origin record — the one written for the
      folder that just left.

    The exclusion is also not useless above the store: a parent whose ONLY child
    is the excluded dir, and which holds no file of its own, reads as EMPTY
    (never recorded, so it contributes no ``has_file``) and empty dirs are
    skipped. Give that parent one file of its own and it is reported again. So
    the hole is the ancestor that has other content, not every ancestor.

    That is the shape the ``trash_dir`` exclusion has always had rather than
    anything the sibling store introduced, and the shipped image does not reach
    it (``/data`` and ``/music`` are separate mounts); it needs a beets dir
    deliberately placed inside the music library. Not fixed here: sparing every
    ancestor of an ignored dir would only push the report one level up whenever
    the store is nested deeper, so it is a change to what the finder reports and
    not a guard to bolt on."""
    handle: LibraryHandle = app.state.beets_library  # type: ignore[attr-defined]  # app duck-typed (object)
    configured = _settings(app).playlists_export_dir.strip()  # type: ignore[arg-type]  # app duck-typed (object)
    export_dir = (
        Path(configured) if configured else Path(os.fsdecode(handle.lib.directory)) / ".playlists"
    )
    return (export_dir, _origins_dir(app))


@router.get("/reorganize/preview")
async def preview_reorganize(
    request: Request,
    handle: Annotated[LibraryHandle, Depends(get_library)],
    artist: Annotated[str | None, Query(min_length=1)] = None,
) -> ReorganizePlan:
    """Dry run: what would move under the current path config. Read-only."""
    scope: ReorganizeScope = "artist" if artist is not None else "library"
    return await run_in_threadpool(
        plan_reorganize,
        handle.lib,
        scope=scope,
        artist=artist,
        album_id=None,
        trash_dir=_trash_dir(request.app),
        ignore_dirs=_ignore_dirs(request.app),
    )


@router.get(
    "/albums/{album_id}/reorganize/preview",
    responses={404: _ALBUM_NOT_FOUND_RESPONSE},
)
async def preview_album_reorganize(
    album_id: int,
    request: Request,
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> ReorganizePlan:
    exists = await run_in_threadpool(album_exists, handle, album_id)
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Album not found")
    return await run_in_threadpool(
        plan_reorganize,
        handle.lib,
        scope="album",
        artist=None,
        album_id=album_id,
        trash_dir=_trash_dir(request.app),
        ignore_dirs=_ignore_dirs(request.app),
    )


@router.post("/reorganize", responses={409: _REORGANIZE_BUSY_RESPONSE})
async def start_reorganize(
    request: Request,
    reg: Annotated[ReorganizeRegistry, Depends(get_reorganize_backfill)],
    # Depends (not a direct call) so app.dependency_overrides reaches this route
    # too — a bare get_playlists_dir() here would hand the WORKER the real
    # settings-derived store while every test override silently misses it.
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
    artist: Annotated[str | None, Query(min_length=1)] = None,
) -> ReorganizeBackfillStatus:
    _gate_busy(request.app)
    scope: ReorganizeScope = "artist" if artist is not None else "library"
    label = artist if artist is not None else "library"
    try:
        reg.start(scope=scope, artist=artist, album_id=None, scope_label=label)
    except RuntimeError:
        raise HTTPException(status.HTTP_409_CONFLICT, "A reorganize is already running") from None
    app = request.app
    handle = app.state.beets_library
    start_backfill(
        reg,
        handle,
        scope=scope,
        artist=artist,
        album_id=None,
        trash_dir=_trash_dir(app),
        trash_origins_dir=_origins_dir(app),
        ignore_dirs=_ignore_dirs(app),
        playlists_dir=playlists_dir,
        on_complete=lambda: emit_library_changed(app),
    )
    return reg.state()


@router.post(
    "/albums/{album_id}/reorganize",
    responses={404: _ALBUM_NOT_FOUND_RESPONSE, 409: _REORGANIZE_BUSY_RESPONSE},
)
async def start_album_reorganize(
    album_id: int,
    request: Request,
    reg: Annotated[ReorganizeRegistry, Depends(get_reorganize_backfill)],
    # Same Depends-not-direct-call rationale as start_reorganize above.
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
) -> ReorganizeBackfillStatus:
    handle = request.app.state.beets_library
    label = await run_in_threadpool(album_scope_label, handle, album_id)
    if label is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Album not found")
    _gate_busy(request.app)
    try:
        reg.start(scope="album", artist=None, album_id=album_id, scope_label=label)
    except RuntimeError:
        raise HTTPException(status.HTTP_409_CONFLICT, "A reorganize is already running") from None
    app = request.app
    start_backfill(
        reg,
        handle,
        scope="album",
        artist=None,
        album_id=album_id,
        trash_dir=_trash_dir(app),
        trash_origins_dir=_origins_dir(app),
        ignore_dirs=_ignore_dirs(app),
        playlists_dir=playlists_dir,
        on_complete=lambda: emit_library_changed(app),
    )
    return reg.state()


@router.get("/reorganize/status")
async def reorganize_status(
    reg: Annotated[ReorganizeRegistry, Depends(get_reorganize_backfill)],
) -> ReorganizeBackfillStatus:
    return reg.state()


@router.post("/reorganize/stop")
async def stop_reorganize(
    reg: Annotated[ReorganizeRegistry, Depends(get_reorganize_backfill)],
) -> ReorganizeBackfillStatus:
    reg.request_stop()
    return reg.state()


@router.post(
    "/reorganize/dismiss",
    responses={
        # NOT the shared busy entry: this route has no ``_gate_busy``, so its
        # only 409 is the registry refusing to clear a slot that is still
        # running.
        409: {
            "model": ErrorDetail,
            "description": "A reorganize is still running; stop it before dismissing its result.",
        },
    },
)
async def dismiss_reorganize(
    reg: Annotated[ReorganizeRegistry, Depends(get_reorganize_backfill)],
) -> ReorganizeBackfillStatus:
    """Clear a FINISHED job's result (its failure rows) from the slot.

    NO ``_gate_busy`` on purpose: this touches the in-memory registry only —
    never the library, never beets — so an import or another sweep running
    elsewhere has no reason to hold a stale error message on screen.

    Idempotent: dismissing an already-empty slot returns the idle status rather
    than 404. The caller is asking for "nothing displayed", and that is exactly
    what it gets; a 404 would make the UI special-case a state indistinguishable
    from success (a double click, a retry, or a concurrent tab that dismissed
    first). Returns the post-dismiss status so the caller can seed its cache
    without a follow-up GET.
    """
    try:
        reg.dismiss()
    except RuntimeError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A reorganize is running; stop it before dismissing"
        ) from None
    return reg.state()
