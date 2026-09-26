"""Pydantic contract for Settings → Sources (``/api/sources``).

Folder sources only: slskd is not a source row (``decisions`` #77), and its own
settings stay on ``/api/slskd/settings``.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, StringConstraints

from app.models.import_api import without_a_nul


class SourceSummary(BaseModel):
    """One Folder source. ``folder`` is the path as it was added (display form);
    ``exists`` is whether it is a folder right now."""

    id: str
    name: str
    folder: str
    exists: bool


class SourceList(BaseModel):
    """Every Folder source, in the order added."""

    sources: list[SourceSummary]


class FolderSourceCreate(BaseModel):
    """Body of ``POST /api/sources/folders``. Both fields are trimmed.

    ``folder`` is a server folder, taken back the way ``POST /api/import`` takes
    its ``path``.
    """

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    folder: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=4096),
        AfterValidator(without_a_nul),
    ]
