"""Playlist track resolution — the beets read-side for playlists.

Given an ordered list of beets ``item.id`` values (the owned playlist
membership), return displayable ``PlaylistTrack`` rows. Reads metadata only
(title/artist/album/length), never file paths, so no ``music_dir_context`` is
needed here — that is the M3U export's concern (Chunk 3). All beets access for
playlists stays in this module (CLAUDE.md rule 3).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from beets.library import Library

from app.beets.library import _abs_path, _coerce_duration, _coerce_str
from app.models.playlist import PlaylistTrack
from app.playlists.m3u import M3uEntry
from app.playlists.store import StoredEntry  # store never imports beets - no cycle


@dataclass(frozen=True)
class TrackRef:
    """A resolvable track's absolute path plus the metadata Plex matches on.

    Plex-unaware on purpose (CLAUDE.md rule 3): the API translates ``abs_path``
    into a Plex view and pairs it with this metadata as a ``PlexTrackSpec``.
    """

    abs_path: str
    albumartist: str
    album: str
    title: str
    track: int | None


def _coerce_track(value: object) -> int | None:
    """beets ``item.track`` -> a positive int, or None (0/absent/unparseable)."""
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        n = int(value)
        return n if n > 0 else None
    return None


def _resolved(uid: str, item: Any) -> PlaylistTrack:
    return PlaylistTrack(
        uid=uid,
        id=int(item.id),
        title=_coerce_str(item.title),
        artist=_coerce_str(item.artist),
        album=_coerce_str(item.album),
        duration_seconds=_coerce_duration(item.length),
        available=True,
        pending=False,
    )


def _unavailable(uid: str, item_id: int) -> PlaylistTrack:
    return PlaylistTrack(
        uid=uid,
        id=item_id,
        title="",
        artist="",
        album="",
        duration_seconds=None,
        available=False,
        pending=False,
    )


def _pending_row(entry: StoredEntry) -> PlaylistTrack:
    info = entry.pending
    return PlaylistTrack(
        uid=entry.uid,
        id=None,
        title=(info.title if info else None) or "",
        artist=(info.artist if info else None) or "",
        album=(info.album if info else None) or "",
        duration_seconds=info.duration_seconds if info else None,
        available=False,
        pending=True,
        source=info.source if info else None,
    )


def item_exists(lib: Library, item_id: int) -> bool:
    """True iff ``item_id`` resolves to a library track. The beets access for
    the resolve endpoint's validity check lives here (CLAUDE.md rule 3)."""
    try:
        return lib.get_item(item_id) is not None
    except Exception:  # a locked/odd row is treated as "not a valid target"
        return False


def resolve_entries(lib: Library, entries: list[StoredEntry]) -> list[PlaylistTrack]:
    """Ordered rows for every entry — resolved, pending, or unavailable.

    A per-item lookup failure (a missing id, or a locked/odd library row that
    raises) degrades that one entry to "unavailable" rather than failing the
    whole playlist view with a 500 — the owned store still holds the membership.
    """
    tracks: list[PlaylistTrack] = []
    for entry in entries:
        if entry.item_id is None:
            tracks.append(_pending_row(entry))
            continue
        try:
            item = lib.get_item(entry.item_id)
            row = (
                _resolved(entry.uid, item)
                if item is not None
                else _unavailable(entry.uid, entry.item_id)
            )
        except Exception:  # one bad row degrades, never 500s the view
            row = _unavailable(entry.uid, entry.item_id)
        tracks.append(row)
    return tracks


def track_match_refs(lib: Library, ids: list[int]) -> list[TrackRef]:
    """Ordered ``TrackRef``s for resolvable tracks (missing ids dropped).

    Read inside ``music_dir_context`` so beets re-expands DB-relative paths.
    Carries album-artist/album/title/track so the Plex side can fall back to
    metadata matching when the exact file path isn't present in Plex."""
    refs: list[TrackRef] = []
    with lib.music_dir_context():
        for item_id in ids:
            try:
                item = lib.get_item(item_id)
            except Exception:  # a locked/odd row is simply skipped
                item = None
            if item is None:
                continue
            refs.append(
                TrackRef(
                    abs_path=_abs_path(lib, item.path),
                    albumartist=_coerce_str(item.albumartist),
                    album=_coerce_str(item.album),
                    title=_coerce_str(item.title),
                    track=_coerce_track(item.track),
                )
            )
    return refs


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
            try:
                rel_path = os.path.relpath(abs_path, export_dir).replace(os.sep, "/")
            except ValueError:
                # Different mounts/drives have no relative path (Windows); the
                # track simply can't be expressed in this .m3u8, so skip it.
                continue
            entries.append(
                M3uEntry(
                    duration_seconds=int(_coerce_duration(item.length) or 0),
                    artist=_coerce_str(item.artist),
                    title=_coerce_str(item.title),
                    path=rel_path,
                )
            )
    return entries
