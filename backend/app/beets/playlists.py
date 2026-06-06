"""Playlist track resolution — the beets read-side for playlists.

Given an ordered list of beets ``item.id`` values (the owned playlist
membership), return displayable ``PlaylistTrack`` rows. Reads metadata only
(title/artist/album/length), never file paths, so no ``music_dir_context`` is
needed here — that is the M3U export's concern (Chunk 3). All beets access for
playlists stays in this module (CLAUDE.md rule 3).
"""

from __future__ import annotations

from typing import Any

from beets.library import Library

from app.beets.library import _coerce_duration, _coerce_str
from app.models.playlist import PlaylistTrack


def _resolved(item: Any) -> PlaylistTrack:
    return PlaylistTrack(
        id=int(item.id),
        title=_coerce_str(item.title),
        artist=_coerce_str(item.artist),
        album=_coerce_str(item.album),
        duration_seconds=_coerce_duration(item.length),
        available=True,
    )


def _unavailable(item_id: int) -> PlaylistTrack:
    return PlaylistTrack(
        id=item_id,
        title="",
        artist="",
        album="",
        duration_seconds=None,
        available=False,
    )


def resolve_tracks(lib: Library, ids: list[int]) -> list[PlaylistTrack]:
    """Resolve ``ids`` to ordered tracks; unknown ids become unavailable rows.

    A per-item lookup failure (a missing id, or a locked/odd library row that
    raises) degrades that one entry to "unavailable" rather than failing the
    whole playlist view with a 500 — the owned store still holds the membership.
    """
    tracks: list[PlaylistTrack] = []
    for item_id in ids:
        try:
            item = lib.get_item(item_id)
            track = _resolved(item) if item is not None else _unavailable(item_id)
        except Exception:  # one bad row degrades to unavailable, never 500s the view
            track = _unavailable(item_id)
        tracks.append(track)
    return tracks
