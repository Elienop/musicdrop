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
from app.beets.store_layout import StoreLayoutError, checked_store_dirs
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
    "description": (
        "The Trash directory or the Trash origin store now sits where using it"
        " would destroy data (or no longer resolves), so no reorganize was"
        " planned or started; the message names the setting and both resolved"
        " paths."
    ),
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


def _ignore_dirs(app: object, origins_dir: Path) -> tuple[Path, ...]:
    """The app-owned dirs the orphan sweep must never trash: the resolved
    playlists export dir (defaults to <music>/.playlists), the Trash origin store,
    and the beets data dir. Dotdirs/NAS dirs are handled name-based in the
    scanner; these three cover the case where the configured path is an ordinary
    name the scanner has no reason to skip.

    Each earns its place by being audio-empty BY DEFINITION while looking exactly
    like a husk:

    * the **export dir** holds ``.m3u8`` files;
    * the **origin store** holds ``.json`` files, and sweeping it would take
      every row's exact restore into Trash in one pass;
    * the **beets data dir**, and ONLY when it sits strictly inside the music
      root. It holds ``library.db`` + ``config.yaml`` — content of its own — so
      unlike the other two it is reported DIRECTLY (measured on ``d65e635``:
      ``MUSICDROP_BEETS_DIR=<music>/musicdrop`` returned ``musicdrop``,
      identically with and without the store exclusion). It is not excluded by
      name either: only a dot-name is, and ``<music>/.musicdrop`` returned ``[]``
      where ``<music>/musicdrop`` did not, which is the whole difference between
      the two spellings. It is listed here so the containment rule can allow a
      beets dir inside the library (``app.beets.store_layout``) without the sweep
      then trashing it.

      The "strictly inside" condition is load-bearing, not tidiness. An exclude
      root that CONTAINS the music root excludes every candidate — ``excluded()``
      is a prefix test — so the sweep would return ``[]`` for the whole library
      and report nothing at all. That is not hypothetical: the dev/test layout
      has ``beets_dir`` as the PARENT of ``directory`` (``<beets>/../music`` is
      the starter config's own default shape), which is exactly the case an
      unconditional entry silences.

    The Trash dir itself is excluded inside ``find_orphan_folders``, which takes
    it as its own argument.

    Excluding a subtree is not on its own enough, because the mover takes a
    reported folder's WHOLE subtree: a sweep that reports an ANCESTOR of an
    excluded dir hands the excluded dir over anyway. That hole is closed at the
    finder now (``orphans._drop_excluded_ancestors``), which drops every
    candidate above any excluded root, so the tuple this function returns is
    protected up as well as down.

    Both consumers read this same tuple — the dry-run preview
    (``beets.reorganize._orphan_preview``) and the runner's ``_sweep_orphans`` —
    so preview and outcome cannot disagree about what is spared."""
    handle: LibraryHandle = app.state.beets_library  # type: ignore[attr-defined]  # app duck-typed (object)
    configured = _settings(app).playlists_export_dir.strip()  # type: ignore[arg-type]  # app duck-typed (object)
    music_dir = Path(os.fsdecode(handle.lib.directory))
    export_dir = Path(configured) if configured else music_dir / ".playlists"
    dirs = [export_dir, origins_dir]
    beets_dir = handle.beets_dir.resolve()
    music_root = music_dir.resolve()
    if beets_dir != music_root and beets_dir.is_relative_to(music_root):
        dirs.append(handle.beets_dir)
    return tuple(dirs)


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
        ignore_dirs=_ignore_dirs(request.app, origins_dir),
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
        ignore_dirs=_ignore_dirs(request.app, origins_dir),
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
    try:
        reg.start(scope=scope, artist=artist, album_id=None, scope_label=label)
    except RuntimeError:
        raise HTTPException(status.HTTP_409_CONFLICT, "A reorganize is already running") from None
    app = request.app
    handle = app.state.beets_library
    trash_dir, origins_dir = _store(app)
    start_backfill(
        reg,
        handle,
        scope=scope,
        artist=artist,
        album_id=None,
        trash_dir=trash_dir,
        trash_origins_dir=origins_dir,
        ignore_dirs=_ignore_dirs(app, origins_dir),
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
    try:
        reg.start(scope="album", artist=None, album_id=album_id, scope_label=label)
    except RuntimeError:
        raise HTTPException(status.HTTP_409_CONFLICT, "A reorganize is already running") from None
    app = request.app
    trash_dir, origins_dir = _store(app)
    start_backfill(
        reg,
        handle,
        scope="album",
        artist=None,
        album_id=album_id,
        trash_dir=trash_dir,
        trash_origins_dir=origins_dir,
        ignore_dirs=_ignore_dirs(app, origins_dir),
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
