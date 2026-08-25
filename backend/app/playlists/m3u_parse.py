"""EXTM3U parsing for playlist import — the read-side sibling of ``m3u.py``.

Pure module: no beets, no Plex, no filesystem. Tolerates the wild: BOM, CRLF,
Windows separators/drive letters (kept verbatim in ``path`` — the matcher only
uses the basename), ``file://`` URIs (percent-decoded), comments, blanks.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from app.models.playlist_import import ParsedPlaylist, SourceEntry
from app.playlists.stem import filename_stem

_EXTINF_SECS = re.compile(r"-?\d+(?:\.\d+)?")


def _split_artist_title(text: str) -> tuple[str | None, str | None]:
    text = text.strip()
    if not text:
        return None, None
    if " - " in text:
        artist, title = text.split(" - ", 1)
        return artist.strip() or None, title.strip() or None
    return None, text


def _decode_path(line: str) -> str:
    if line.lower().startswith("file://"):
        return unquote(urlparse(line).path)
    return line


def _parse_extinf(line: str) -> tuple[float | None, str | None, str | None] | None:
    """Parse an ``#EXTINF`` line into ``(seconds, artist, title)``; ``None`` if
    the line is not an ``#EXTINF`` (other directives/comments are skipped).

    Split, don't regex, the freeform line: only the short, already-split
    seconds field gets a bounded ``fullmatch`` (no backtracking surface)."""
    if not line.startswith("#EXTINF:"):
        return None
    head, sep, text = line[len("#EXTINF:") :].partition(",")
    if not sep:
        return None  # the comma is mandatory
    secs_raw = head.strip()
    if secs_raw and not _EXTINF_SECS.fullmatch(secs_raw):
        return None  # not a well-formed seconds field — not an EXTINF line
    secs = float(secs_raw) if secs_raw and float(secs_raw) > 0 else None
    artist, title = _split_artist_title(text.lstrip())
    return secs, artist, title


def _entry_for(
    entries: list[SourceEntry],
    line: str,
    pending_extinf: tuple[float | None, str | None, str | None],
) -> SourceEntry:
    """Build the entry for a path line: the pending EXTINF (or empty) metadata,
    the title falling back to the filename stem, at the running position."""
    path = _decode_path(line)
    secs, artist, title = pending_extinf
    if title is None:
        title = filename_stem(path, strip_track_number=True) or None
    return SourceEntry(
        position=len(entries),
        path=path,
        artist=artist,
        title=title,
        album=None,
        duration_seconds=secs,
        source=line,
    )


def parse_m3u(name: str, content: str) -> ParsedPlaylist:
    """Parse one playlist file. ``name`` is the file name (extension dropped)."""
    playlist_name = name.rsplit(".", 1)[0] if "." in name else name
    entries: list[SourceEntry] = []
    pending_extinf: tuple[float | None, str | None, str | None] | None = None
    for raw in content.lstrip("\ufeff").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            pending_extinf = _parse_extinf(line) or pending_extinf
            continue  # other directives/comments are skipped
        entries.append(_entry_for(entries, line, pending_extinf or (None, None, None)))
        pending_extinf = None
    return ParsedPlaylist(name=playlist_name, entries=entries)
