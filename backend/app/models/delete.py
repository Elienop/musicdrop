"""Contract for the reversible delete (move-to-Trash) of albums and artists."""

from __future__ import annotations

from pydantic import BaseModel


class DeleteResult(BaseModel):
    """Outcome of a reversible delete: how many albums went to Trash + where.

    ``trashed_albums`` is 1 for a single-album delete, N for an artist (every
    album of theirs). ``trash_path`` is where the files went — a location inside
    the Trash folder when any moved, and the album's own folder or the Trash root
    when an album had nothing left to move.
    """

    trashed_albums: int
    trash_path: str
    # Playlists whose `.m3u8` export was rewritten because this delete DROPPED one
    # of their tracks — the export would otherwise keep listing a file that is now
    # in Trash. Best-effort (a failed write still counts). Defaulted because the
    # adapter builds the result before the collateral runs; the endpoint fills it in.
    playlists_reexported: int = 0
