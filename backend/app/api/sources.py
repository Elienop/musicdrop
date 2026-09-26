"""Settings → Sources: the Folder sources Add from folder offers as buttons.

A Folder source is a name and a folder (``app.sources.store``). slskd is never
listed here (``decisions`` #77: slskd's message is the one way in); its folder
shows on its own card through ``GET /api/slskd/settings``.

The folder is stored as the client sent it, in display form, and resolved on
use the way ``POST /api/import`` resolves its ``path``. Adding asks what a start
would: the store layout is not refused (else the start's 503), the folder is
there and is a folder, and the import refusal lets it through, with the same
sentences. Removing touches no files.
"""

from __future__ import annotations

import os
from functools import partial
from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.api.folders import folder_read
from app.api.import_ import AMBIGUOUS_FOLDERS_DETAIL
from app.beets.store_layout import source_refusal
from app.config import settings
from app.import_jobs.registry import ImportJobRegistry, LibraryRefusedError, get_registry
from app.import_jobs.runner import (
    SOURCE_MISSING,
    ImportSourceRefusedError,
    SourcePathMissingError,
    missing_source_error,
)
from app.models.errors import ErrorDetail, validation_or_detail_422
from app.models.sources import FolderSourceCreate, SourceList, SourceSummary
from app.sources.store import FolderSource, SourcesStore, sources_path
from app.wire import AmbiguousDisplayName, resolve_posted_path

router = APIRouter(tags=["sources"])

SOURCE_NOT_FOUND: Final = "Source not found."


def get_sources_store() -> SourcesStore:
    """Resolved from ``settings`` per request, like the slskd store, so it works
    under the lifespan-less test client."""
    return SourcesStore(sources_path(Path(settings.beets_dir)))


def _is_folder(folder: str) -> bool:
    """Whether a stored display-form folder is a folder right now.

    Two folders showing under one name are there; a start on it answers 409.
    """
    try:
        return os.path.isdir(resolve_posted_path(folder))
    except AmbiguousDisplayName:
        return True


def _summary(source: FolderSource) -> SourceSummary:
    return SourceSummary(
        id=source.id, name=source.name, folder=source.folder, exists=_is_folder(source.folder)
    )


def _read_sources(store: SourcesStore) -> SourceList:
    return SourceList(sources=[_summary(source) for source in store.folders()])


def _add(store: SourcesStore, reg: ImportJobRegistry, name: str, folder: str) -> SourceSummary:
    """Check ``folder`` as a start would, then store it as sent. Every blocking step.

    A refused layout answers first, as it does for a start: it leaves no rows, so
    nothing else here could say the folder is slskd's or the beets dir.
    """
    reg.raise_if_refused()
    path = resolve_posted_path(folder)
    refusal = missing_source_error([path])
    if refusal is not None:
        raise refusal
    if not os.path.isdir(path):
        raise SourcePathMissingError(SOURCE_MISSING)
    rows = reg.source_rows()
    refused = None if rows is None else source_refusal([path], rows)
    if refused is not None:
        raise ImportSourceRefusedError(refused)
    added = store.add(name, folder)
    return SourceSummary(id=added.id, name=added.name, folder=added.folder, exists=True)


@router.get("/sources")
async def list_sources(
    store: Annotated[SourcesStore, Depends(get_sources_store)],
) -> SourceList:
    """The Folder sources, in the order added. Never slskd."""
    return await folder_read(partial(_read_sources, store))


@router.post(
    "/sources/folders",
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {
            "model": ErrorDetail,
            "description": "Two folders display under the same name.",
        },
        422: validation_or_detail_422(
            "The folder does not exist, is not a folder or cannot be read, or it is or"
            " holds the library or MusicDrop's own data, or it is or holds slskd's whole"
            " folder, or the request failed validation."
        ),
        503: {
            "model": ErrorDetail,
            "description": "The store layout is refused, so no import could start.",
        },
    },
)
async def add_folder_source(
    body: FolderSourceCreate,
    store: Annotated[SourcesStore, Depends(get_sources_store)],
    reg: Annotated[ImportJobRegistry, Depends(get_registry)],
) -> SourceSummary:
    """Add a Folder source when a start on its folder would not be refused."""
    try:
        return await folder_read(partial(_add, store, reg, body.name, body.folder))
    except AmbiguousDisplayName:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=AMBIGUOUS_FOLDERS_DETAIL
        ) from None
    except (SourcePathMissingError, ImportSourceRefusedError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except LibraryRefusedError as exc:
        # The start's answer and sentence (``app/api/import_.py``).
        raise HTTPException(status_code=503, detail=str(exc)) from None


@router.delete(
    "/sources/folders/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"model": ErrorDetail, "description": "No Folder source has that id."}},
)
async def remove_folder_source(
    source_id: str,
    store: Annotated[SourcesStore, Depends(get_sources_store)],
) -> Response:
    """Remove a Folder source. The folder on disk is not touched.

    Under the same cap as list and add, since it reads the same file.
    """
    if not await folder_read(partial(store.remove, source_id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=SOURCE_NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
