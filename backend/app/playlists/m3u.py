"""EXTM3U rendering + crash-safe write for the playlist `.m3u8` export.

Pure module: no beets, no Plex. The track rows (``M3uEntry``) are built by
``app.beets.playlists.m3u_entries`` (which owns path resolution); this module
only formats and writes them. The human name rides in a ``#PLAYLIST`` directive
so the id-based filename stays stable across renames.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel


class M3uEntry(BaseModel):
    """One resolved track line for the `.m3u8` file."""

    duration_seconds: int
    artist: str
    title: str
    path: str  # relative to the playlist file's directory, POSIX separators


def render_m3u(name: str, entries: list[M3uEntry]) -> str:
    """Render EXTM3U text (UTF-8). A trailing newline always terminates the file."""
    lines = ["#EXTM3U", f"#PLAYLIST:{name}"]
    for entry in entries:
        lines.append(f"#EXTINF:{entry.duration_seconds},{entry.artist} - {entry.title}")
        lines.append(entry.path)
    return "\n".join(lines) + "\n"


def write_m3u(path: Path, name: str, entries: list[M3uEntry]) -> None:
    """Atomically write the `.m3u8` (tmp -> fsync -> replace -> dir fsync)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render_m3u(name, entries)
    tmp = path.parent / f".{path.name}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def delete_m3u(path: Path) -> None:
    """Remove the `.m3u8` if present (idempotent)."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
