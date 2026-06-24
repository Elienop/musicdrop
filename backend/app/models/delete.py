"""Contract for the reversible delete (move-to-Trash) of albums and artists."""

from __future__ import annotations

from pydantic import BaseModel


class DeleteResult(BaseModel):
    """Outcome of a reversible delete: how many albums went to Trash + where.

    ``trashed_albums`` is 1 for a single-album delete, N for an artist (every
    album of theirs). ``trash_path`` is the Trash location the files were moved
    to (recoverable from there).
    """

    trashed_albums: int
    trash_path: str
