"""Crash-safe atomic text-file write, shared by the playlist store and the
`.m3u8` export so the recipe lives in exactly one place.

Recipe (per the config-editor ``atomic_write`` rationale): write a tempfile in
the SAME directory -> ``flush`` + ``fsync`` -> ``chmod`` -> ``os.replace``
-> ``fsync`` the PARENT DIRECTORY. The parent-dir fsync forces the rename
durable so a power-cut can't lose the new file.

``mode`` defaults to world-readable ``0o644`` (fine for playlists/`.m3u8`);
callers writing a secret (e.g. the Plex admin token) pass ``mode=0o600`` so the
file is owner-only.
"""

from __future__ import annotations

import os
from pathlib import Path


def write_atomic_text(path: Path, text: str, *, mode: int = 0o644) -> None:
    """Atomically (re)write ``path`` with ``text`` (UTF-8), creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
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
