"""Pydantic contract for the folder browser (``GET /api/folders``).

No beets imports: plain shapes. Every name and path in them has already been
through ``app.wire.display_path``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class FolderEntry(BaseModel):
    """One folder directly inside the listed one.

    ``badge`` is ``library`` for the music library and ``musicdrop`` for a
    folder that is, holds or sits inside one of MusicDrop's own. It only informs:
    every folder can still be opened.
    """

    name: str
    path: str
    badge: Literal["library", "musicdrop"] | None


class FolderListing(BaseModel):
    """The folders directly inside ``path``, sorted ignoring case.

    At most 500 are listed; ``total`` counts them all. Folders an import skips
    (beets' ``ignore`` and ``ignore_hidden``) are left out. ``parent`` is null at
    ``/``. ``refusal`` is the sentence an import of ``path`` would be refused
    with, or null.
    """

    path: str
    parent: str | None
    folders: list[FolderEntry]
    total: int
    refusal: str | None
