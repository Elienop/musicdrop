"""Pydantic contract for the Trash management view (list / restore / empty)."""

from typing import Literal

from pydantic import BaseModel


class TrashedAlbum(BaseModel):
    """One album sitting in Trash (no beets DB row — read off disk).

    ``folder`` is the album's path RELATIVE to the Trash dir; it is the key the
    restore/empty endpoints take (resolved + traversal-checked server-side).
    """

    folder: str
    album_artist: str | None
    album: str | None
    year: int | None
    track_count: int
    format: str | None


class TrashListing(BaseModel):
    albums: list[TrashedAlbum]
    trash_path: str  # absolute Trash dir, shown so the user knows where it lives


class RestoreRequest(BaseModel):
    """Body of ``POST /api/trash/restore`` — the folder (relative to Trash)."""

    folder: str


class RestoreResult(BaseModel):
    """Outcome of an as-is restore. ``already_in_library`` = a matching album is
    already present, so beets safely skipped (files stay in Trash)."""

    restored: bool
    reason: Literal["restored", "already_in_library", "could_not_restore"]
    album_id: int | None = None


class EmptyResult(BaseModel):
    removed: int  # folders permanently removed
