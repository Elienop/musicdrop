"""EXTM3U rendering + crash-safe write for the playlist `.m3u8` export.

Pure module: no beets, no Plex. The track rows (``M3uEntry``) are built by
``app.beets.playlists.m3u_entries`` (which owns path resolution); this module
only formats and writes them. The human name rides in a ``#PLAYLIST`` directive
so the id-based filename stays stable across renames.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from app.playlists.atomic import write_atomic_text


class M3uEntry(BaseModel):
    """One resolved track line for the `.m3u8` file."""

    duration_seconds: int
    artist: str
    title: str
    path: str  # relative to the playlist file's directory, POSIX separators


def _sanitize_line(text: str) -> str:
    """Strip the characters that would break or inject m3u directives.

    The playlist name and a track's artist/title/path go onto their own lines;
    an embedded CR/LF (or form feed) would split a value across lines and could
    forge a fake ``#EXTINF`` entry, so collapse them to a space.
    """
    return text.replace("\r", "").replace("\n", " ").replace("\x0c", " ")


def render_m3u(name: str, entries: list[M3uEntry]) -> str:
    """Render EXTM3U text (UTF-8). A trailing newline always terminates the file."""
    lines = ["#EXTM3U", f"#PLAYLIST:{_sanitize_line(name)}"]
    for entry in entries:
        artist = _sanitize_line(entry.artist)
        title = _sanitize_line(entry.title)
        lines.append(f"#EXTINF:{entry.duration_seconds},{artist} - {title}")
        lines.append(_sanitize_line(entry.path))
    return "\n".join(lines) + "\n"


def write_m3u(path: Path, name: str, entries: list[M3uEntry]) -> None:
    """Atomically write the `.m3u8` (crash-safe; creates the export dir)."""
    write_atomic_text(path, render_m3u(name, entries))


def delete_m3u(path: Path) -> None:
    """Remove the `.m3u8` if present (idempotent)."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
