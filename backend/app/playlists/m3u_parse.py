"""EXTM3U parsing for playlist import — the read-side sibling of ``m3u.py``.

Pure module: no beets, no Plex, no filesystem. Tolerates the wild: BOM, CRLF,
Windows separators/drive letters (kept verbatim in ``path`` — the matcher only
uses the basename), ``file://`` URIs (percent-decoded), comments, blanks.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from app.models.playlist_import import ParsedPlaylist, SourceEntry

_EXTINF = re.compile(r"\A#EXTINF:\s*(-?\d+(?:\.\d+)?)?\s*,\s*(.*)\Z")
_LEADING_TRACK_NO = re.compile(r"\A\d{1,3}\s*[-._ ]\s*")


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


def _stem_title(path: str) -> str | None:
    stem = path.replace("\\", "/").rsplit("/", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    stem = _LEADING_TRACK_NO.sub("", stem).strip()
    return stem or None


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
            m = _EXTINF.match(line)
            if m:
                secs_raw, text = m.group(1), m.group(2)
                secs = float(secs_raw) if secs_raw is not None and float(secs_raw) > 0 else None
                artist, title = _split_artist_title(text)
                pending_extinf = (secs, artist, title)
            continue  # other directives/comments are skipped
        path = _decode_path(line)
        secs, artist, title = pending_extinf or (None, None, None)
        if title is None:
            title = _stem_title(path)
        entries.append(
            SourceEntry(
                position=len(entries),
                path=path,
                artist=artist,
                title=title,
                album=None,
                duration_seconds=secs,
                source=line,
            )
        )
        pending_extinf = None
    return ParsedPlaylist(name=playlist_name, entries=entries)
