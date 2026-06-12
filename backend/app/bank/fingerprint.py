"""Folder identity for bank rows.

SHA-256 over the sorted (relative path, size, mtime_ns) of every regular file
under the folder. ``mtime_ns`` participates ONLY inside the hash input as a
string — it never crosses a JSON boundary as a number (JS would silently lose
precision past 2**53). The apply runner (chunk 4) recomputes this before any
file moves; a mismatch means the folder changed since banking and the row goes
``stale`` instead of applying against the wrong files.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def folder_fingerprint(folder: Path) -> str:
    """Raise ``FileNotFoundError`` when the folder is gone (caller maps it)."""
    if not folder.is_dir():
        raise FileNotFoundError(folder)
    digest = hashlib.sha256()
    entries: list[tuple[str, int, int]] = []
    for root, _dirs, files in os.walk(folder):
        for name in files:
            path = Path(root) / name
            try:
                stat = path.stat()
            except OSError:
                continue  # vanished mid-walk: identity reflects what remains
            entries.append((path.relative_to(folder).as_posix(), stat.st_size, stat.st_mtime_ns))
    for rel, size, mtime_ns in sorted(entries):
        digest.update(f"{rel}\n{size}\n{mtime_ns}\n".encode())
    return digest.hexdigest()
