"""Contract for the reversible delete (move-to-Trash) of albums and artists."""

from __future__ import annotations

from pydantic import BaseModel


class DeleteResult(BaseModel):
    """Outcome of a reversible delete: how many albums went to Trash + where.

    ``trashed_albums`` is 1 for a single-album delete, N for an artist (every
    album of theirs). ``trash_path`` is where to look: the album's own folder
    inside Trash for an album delete, its source folder when the files were
    already there, or the Trash root — an album with no files lands nowhere, and
    an artist delete gives each of its albums a container of its own.
    """

    trashed_albums: int
    trash_path: str
    # Playlists whose `.m3u8` export was rewritten because this delete DROPPED one
    # of their tracks — the export would otherwise keep listing a file that is now
    # in Trash. Best-effort (a failed write still counts). Defaulted because the
    # adapter builds the result before the collateral runs; the endpoint fills it in.
    playlists_reexported: int = 0
