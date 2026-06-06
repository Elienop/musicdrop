"""Playlist track resolution — the beets read-side for playlists.

Given an ordered list of beets ``item.id`` values (the owned playlist
membership), return displayable ``PlaylistTrack`` rows. Reads metadata only
(title/artist/album/length), never file paths, so no ``music_dir_context`` is
needed here — that is the M3U export's concern (Chunk 3). All beets access for
playlists stays in this module (CLAUDE.md rule 3).
"""

from __future__ import annotations

import os
from typing import Any

from beets.library import Library

from app.beets.library import _abs_path, _coerce_duration, _coerce_str
from app.models.playlist import PlaylistTrack
from app.playlists.m3u import M3uEntry


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


def m3u_entries(lib: Library, ids: list[int], export_dir: str) -> list[M3uEntry]:
    """Resolve ``ids`` to EXTM3U rows with paths relative to ``export_dir``.

    Unavailable ids (gone from the library, or a lookup that raises) are dropped
    — only real, resolvable files belong in a `.m3u8`. Read inside
    ``music_dir_context`` so beets re-expands DB-relative paths; paths are made
    relative to the playlist file's directory (Docker-proof) with POSIX
    separators.
    """
    entries: list[M3uEntry] = []
    with lib.music_dir_context():
        for item_id in ids:
            try:
                item = lib.get_item(item_id)
            except Exception:  # a locked/odd row is simply omitted from the export
                item = None
            if item is None:
                continue
            abs_path = _abs_path(lib, item.path)
            rel_path = os.path.relpath(abs_path, export_dir).replace(os.sep, "/")
            entries.append(
                M3uEntry(
                    duration_seconds=int(_coerce_duration(item.length) or 0),
                    artist=_coerce_str(item.artist),
                    title=_coerce_str(item.title),
                    path=rel_path,
                )
            )
    return entries
