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
from app.beets.protected import protected_entries
from app.beets.reorganize import album_scope_label, plan_reorganize
from app.beets.store_layout import (
    StoreLayoutError,
    checked_store_dirs,
    lib_music_and_library,
)
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
#: Both the preview and the start route resolve the Trash / origin-store pair
#: per request and run the containment check on what it resolved to, so both can
#: refuse before the sweep is planned or spawned.
_LAYOUT_REFUSED_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": "A store-layout refusal; nothing was planned or started.",
}


def _gate_busy(app: object) -> None:
    # Excludes reorganize's own slot — this is reorganize's start-gate; the
    # single-slot check stays at ``reg.start`` (RuntimeError -> 409).
    raise_if_library_busy(app, exclude=("reorganize",), message=_BUSY)


def _store(app: object) -> tuple[Path, Path]:
    """The CHECKED Trash / origin-store pair for the orphan sweep, or a 503.

    Resolved exactly as the trash API resolves them, and checked at the same
    moment: ``resolve()`` follows whatever the configured path points at NOW, so
    a symlink dropped at the Trash path after startup would send every husk this
    run moves wherever it points. The runner re-asks on its own thread when the
    sweep actually begins (``reorganize_jobs.runner._sweep_orphans``); this one
    is what stops the preview from describing a run that would not be allowed.

    503 with the refusal's own sentence, the tier the delete paths use for the
    same class of fault. Raised inline so the status stays a literal
    ``tests/test_route_status_declarations.py`` can see.
    """
    handle: LibraryHandle = app.state.beets_library  # type: ignore[attr-defined]  # app duck-typed (object)
    try:
        return checked_store_dirs(_settings(app), handle)  # type: ignore[arg-type]  # app duck-typed (object)
    except StoreLayoutError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _ignore_dirs(app: object, trash_dir: Path, origins_dir: Path) -> tuple[Path, ...]:
    """Every directory the app owns, so the sweep spares what the movers refuse.

    One list with ``protected_trees`` (``beets.protected.protected_entries``),
    not a second hand-written tuple. They were two, and the shorter one was the
    sweep's: with ``MUSICDROP_INBOX_DIR=<M>/Downloads/inbox`` — a layout the
    rule allows — the walk went into the live download inbox, the preview
    offered a still-arriving album's cover-art folder, and the run moved it to
    Trash. Measured in the review round and pinned by
    ``tests/test_orphans.py::test_the_ignore_list_names_every_app_store_the_guard_protects``.

    Two entries are dropped: the music root (the walk root, which
    ``orphans._exclude_ids`` would drop with a WARNING on every sweep) and the
    Trash (``find_orphan_folders``' own argument). A path outside the music tree
    costs one ``stat`` and matches nothing, so the rest are passed as they come.

    The mover's guard still decides: it compares IDENTITY, so a bind-mounted alias
    no spelling here can name is refused at the move, and the run reports that
    folder as skipped.
    """
    handle: LibraryHandle = app.state.beets_library  # type: ignore[attr-defined]  # app duck-typed (object)
    music, library_path = lib_music_and_library(handle.lib)
    entries = protected_entries(
        settings=_settings(app),  # type: ignore[arg-type]  # app duck-typed (object)
        music_dir=music,
        beets_dir=handle.beets_dir,
        trash_dir=trash_dir,
        origins_dir=origins_dir,
        library_path=library_path,
    )
    skip = {music, trash_dir}
    # Deduped in order, on the NORMALISED spelling: on the default layout
    # ``library:`` resolves to ``<B>/library.db``, so two entries are the same
    # directory and the finder would climb from one root twice. ``Path``
    # equality does not fold ``<M>/..`` into ``<M>``'s parent, and measured, the
    # sweep then logged its one at-or-above WARNING twice for one directory.
    seen: dict[str, Path] = {}
    for path, _name, _setting in entries:
        if path not in skip:
            seen.setdefault(os.path.normpath(str(path)), path)
    return tuple(seen.values())


@router.get("/reorganize/preview", responses={503: _LAYOUT_REFUSED_RESPONSE})
async def preview_reorganize(
    request: Request,
    handle: Annotated[LibraryHandle, Depends(get_library)],
    artist: Annotated[str | None, Query(min_length=1)] = None,
) -> ReorganizePlan:
    """Dry run: what would move under the current path config. Read-only."""
    scope: ReorganizeScope = "artist" if artist is not None else "library"
    trash_dir, origins_dir = _store(request.app)
    return await run_in_threadpool(
        plan_reorganize,
        handle.lib,
        scope=scope,
        artist=artist,
        album_id=None,
        trash_dir=trash_dir,
        ignore_dirs=_ignore_dirs(request.app, trash_dir, origins_dir),
    )


@router.get(
    "/albums/{album_id}/reorganize/preview",
    responses={404: _ALBUM_NOT_FOUND_RESPONSE, 503: _LAYOUT_REFUSED_RESPONSE},
)
async def preview_album_reorganize(
    album_id: int,
    request: Request,
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> ReorganizePlan:
    exists = await run_in_threadpool(album_exists, handle, album_id)
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Album not found")
    trash_dir, origins_dir = _store(request.app)
    return await run_in_threadpool(
        plan_reorganize,
        handle.lib,
        scope="album",
        artist=None,
        album_id=album_id,
        trash_dir=trash_dir,
        ignore_dirs=_ignore_dirs(request.app, trash_dir, origins_dir),
    )


@router.post(
    "/reorganize",
    responses={409: _REORGANIZE_BUSY_RESPONSE, 503: _LAYOUT_REFUSED_RESPONSE},
)
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
    app = request.app
    handle = app.state.beets_library
    # Everything that can refuse this start runs BEFORE the slot is claimed.
    # ``reg.start`` used to come first, and the 503 below then left the registry
    # holding a job at phase=running with no worker to finish it — measured:
    # every library-mutating write answered 409 until the process restarted,
    # ``stop`` was a no-op, ``dismiss`` refuses a running job, and repairing the
    # layout did not clear it. ``_store`` and ``_ignore_dirs`` need nothing from
    # the registry, so the order costs nothing.
    trash_dir, origins_dir = _store(app)
    ignore_dirs = _ignore_dirs(app, trash_dir, origins_dir)
    try:
        reg.start(scope=scope, artist=artist, album_id=None, scope_label=label)
    except RuntimeError:
        raise HTTPException(status.HTTP_409_CONFLICT, "A reorganize is already running") from None
    start_backfill(
        reg,
        handle,
        scope=scope,
        artist=artist,
        album_id=None,
        trash_dir=trash_dir,
        trash_origins_dir=origins_dir,
        ignore_dirs=ignore_dirs,
        playlists_dir=playlists_dir,
        on_complete=lambda: emit_library_changed(app),
    )
    return reg.state()


@router.post(
    "/albums/{album_id}/reorganize",
    responses={
        404: _ALBUM_NOT_FOUND_RESPONSE,
        409: _REORGANIZE_BUSY_RESPONSE,
        503: _LAYOUT_REFUSED_RESPONSE,
    },
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
    app = request.app
    # Same order, same reason as ``start_reorganize``: nothing claims the single
    # slot until every refusal has had its turn.
    trash_dir, origins_dir = _store(app)
    ignore_dirs = _ignore_dirs(app, trash_dir, origins_dir)
    try:
        reg.start(scope="album", artist=None, album_id=album_id, scope_label=label)
    except RuntimeError:
        raise HTTPException(status.HTTP_409_CONFLICT, "A reorganize is already running") from None
    start_backfill(
        reg,
        handle,
        scope="album",
        artist=None,
        album_id=album_id,
        trash_dir=trash_dir,
        trash_origins_dir=origins_dir,
        ignore_dirs=ignore_dirs,
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
    """Clear a FINISHED job's result (its failure rows) from the slot. Idempotent."""
    # NO ``_gate_busy`` on purpose: this touches the in-memory registry only —
    # never the library, never beets — so an import or another sweep elsewhere
    # has no reason to hold a stale error message on screen.
    #
    # Dismissing an already-empty slot returns the idle status rather than 404: a
    # 404 would make the UI special-case a state indistinguishable from success
    # (a double click, a retry, a concurrent tab). The post-dismiss status comes
    # back so the caller can seed its cache without a follow-up GET.
    try:
        reg.dismiss()
    except RuntimeError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A reorganize is running; stop it before dismissing"
        ) from None
    return reg.state()
