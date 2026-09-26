"""The folder browser: one server folder's direct child folders.

``GET /api/folders`` backs Add from folder's Browse folders dialog: a file
browser over the server's folders, as Sonarr's is (``decisions`` #54). Folders
only, one level, no walk. It reads no ledger: a slskd folder "Not imported yet"
hides is still listed here, which is the way back to it (#77).

What it asks beets is in ``app.beets.import_walk`` (which folders an import
skips, and in what order the walk sorts them); what it asks the import refusal
is in ``app.beets.store_layout`` (the refusal line and the badges, off the rows
the start refuses from).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from functools import partial
from typing import Annotated, Final, TypeVar

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from pydantic import AfterValidator

from app.api.import_ import AMBIGUOUS_FOLDERS_DETAIL
from app.beets.import_walk import (
    WalkRules,
    skipped_by_the_walk,
    walk_order,
    walk_rules,
    walked_path,
)
from app.beets.store_layout import SourceRows, folder_badge, source_refusal
from app.import_jobs.registry import ImportJobRegistry, get_registry
from app.import_jobs.runner import (
    SourcePathMissingError,
    refuse_unless_absent,
    unreadable_source_error,
)
from app.models.errors import ErrorDetail, validation_or_detail_422
from app.models.folders import FolderEntry, FolderListing
from app.models.import_api import without_a_nul
from app.wire import AmbiguousDisplayName, display_path, resolve_posted_path

router = APIRouter(tags=["folders"])

_T = TypeVar("_T")

#: Where the browser opens with no ``path``: the image's media mount, else ``/``.
DEFAULT_FOLDER: Final = "/media"

#: How many folders one listing names. Not measured: a screenful that stays fast.
#: ``total`` carries the full count, and a typed path opens any folder.
MAX_FOLDERS: Final = 500

#: How many listings may occupy anyio's worker pool at once.
#:
#: A listing is one ``scandir`` plus a ``stat`` per symlinked entry, and on a
#: hung mount none of it returns. Its OWN cap, in ``inbox_read``'s shape
#: (``app/api/acquisition.py``): admission first, then ``run_in_threadpool`` on
#: anyio's default limiter, which does not abandon a cancelled caller's thread,
#: so the cap really bounds threads. 2 because one person clicks through one
#: dialog; a second is a double click or a second tab. No deadline: a hung mount
#: hangs that request and parks the ones behind it, never the rest of the app.
#: Settings → Sources shares it (:func:`folder_read`), and its list and add
#: ``stat`` SAVED folders, so one hung saved folder can stall Browse and every
#: other reader of this cap. Taken so the cap keeps every other thread of
#: anyio's pool for the rest of the app.
_FOLDER_LIST_SLOTS: Final = anyio.CapacityLimiter(2)


async def folder_read(read: Callable[[], _T]) -> _T:
    """Run one blocking read of operator-chosen folders under the cap above."""
    async with _FOLDER_LIST_SLOTS:
        return await run_in_threadpool(read)


def _start_folder(path: str | None) -> bytes:
    """The folder to list first: where a start on ``path`` would walk, or the default.

    Stripped as ``POST /api/import`` strips its ``path``, so the folder listed is
    the one Use this folder starts.
    """
    typed = (path or "").strip()
    if not typed:
        return os.fsencode(DEFAULT_FOLDER if os.path.isdir(DEFAULT_FOLDER) else "/")
    return walked_path(resolve_posted_path(typed))


def _is_folder(entry: os.DirEntry[bytes]) -> bool:
    """beets' walk's answer for one entry: ``os.path.isdir``, False on any ``OSError``.

    ``DirEntry.is_dir()`` answers from the directory entry's own type, so only
    a symlink costs a ``stat``; a link to a folder counts as one. Unlike
    ``isdir`` it RAISES for a link that loops, runs through a file, points into
    a locked folder or names too long a target, and that error would fail or
    redirect the whole listing. beets calls such an entry a file.
    """
    try:
        return entry.is_dir()
    except OSError:
        return False


def _child_folders(folder: bytes, rules: WalkRules) -> list[bytes]:
    """``folder``'s direct child folders an import would walk, in the walk's order.

    The name test runs first and needs no syscall; then ``_is_folder``.
    """
    with os.scandir(folder) as entries:
        names = [
            entry.name
            for entry in entries
            if not skipped_by_the_walk(folder, entry.name, rules) and _is_folder(entry)
        ]
    names.sort(key=lambda name: (walk_order(name), name))
    return names


def _nearest_listing(folder: bytes, rules: WalkRules) -> tuple[bytes, list[bytes]]:
    """List ``folder``, or its nearest ancestor that IS a folder.

    So a Recent folder that was moved away still opens nearby. Absent is
    whatever the import's own absent set says (a missing name, a file where a
    folder was asked, a name too long); anything else is the shared unreadable
    refusal, 422.
    """
    current = folder
    while True:
        try:
            return current, _child_folders(current, rules)
        except OSError as exc:
            refuse_unless_absent(exc)
            parent = os.path.dirname(current)
            if parent == current:
                raise unreadable_source_error(exc) from None
            current = parent


def _entry(
    folder: bytes, name: bytes, rows: SourceRows | None, spellings: list[str]
) -> FolderEntry:
    """One row, badged by path alone (``store_layout.folder_badge``)."""
    child = os.fsdecode(name)
    badge = None
    if rows is not None:
        badge = folder_badge([os.path.join(spelling, child) for spelling in spellings], rows)
    return FolderEntry(
        name=display_path(name), path=display_path(os.path.join(folder, name)), badge=badge
    )


def _read_listing(path: str | None, reg: ImportJobRegistry) -> FolderListing:
    """Every blocking step of one listing, on one worker thread."""
    folder, names = _nearest_listing(_start_folder(path), walk_rules())
    rows = reg.source_rows()
    spelled = os.fsdecode(folder)
    # The listed folder as spelled and where it resolves, so an entry reached
    # through a link to the library's parent still names it. One resolve per
    # listing, never one per entry.
    spellings = list(dict.fromkeys([spelled, os.path.realpath(spelled)]))
    parent = os.path.dirname(folder)
    return FolderListing(
        path=display_path(folder),
        parent=None if parent == folder else display_path(parent),
        folders=[_entry(folder, name, rows, spellings) for name in names[:MAX_FOLDERS]],
        total=len(names),
        refusal=None if rows is None else source_refusal([spelled], rows),
    )


@router.get(
    "/folders",
    responses={
        409: {
            "model": ErrorDetail,
            "description": "Two folders display under the same name.",
        },
        422: validation_or_detail_422(
            "The folder cannot be read, or the request failed validation."
        ),
    },
)
async def list_folders(
    reg: Annotated[ImportJobRegistry, Depends(get_registry)],
    path: Annotated[
        Annotated[str, AfterValidator(without_a_nul)] | None, Query(max_length=4096)
    ] = None,
) -> FolderListing:
    """The folders directly inside ``path``, for Add from folder's browser.

    No ``path`` opens ``/media`` when it is a folder, else ``/``. A ``path`` that
    is not a folder lists its nearest existing parent. ``path`` is the display
    form a listing handed out, mapped back the way ``POST /api/import`` maps it.
    """
    try:
        return await folder_read(partial(_read_listing, path, reg))
    except AmbiguousDisplayName:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=AMBIGUOUS_FOLDERS_DETAIL
        ) from None
    except SourcePathMissingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
