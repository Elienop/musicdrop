"""Folder identity for bank rows.

SHA-256 over the sorted (relative path, size, mtime_ns) of every AUDIO file
under the folder. Only audio identifies the banked album: non-audio sidecars
(cover art, .DS_Store, Thumbs.db, .lrc/.txt) can appear after banking — written
by Plex, macOS, or SMB — and hashing them would wrongly stale the row on apply
even though the audio is unchanged. ``mtime_ns`` participates ONLY inside the
hash input as a string — it never crosses a JSON boundary as a number (JS would
silently lose precision past 2**53). The apply runner (chunk 4) recomputes this
before any file moves; a mismatch means the audio changed since banking and the
row goes ``stale`` instead of applying against the wrong files.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from app.beets.audio import is_audio_file


def folder_fingerprint(folder: Path) -> str:
    """Raise ``FileNotFoundError`` when the folder is gone (caller maps it)."""
    if not folder.is_dir():
        raise FileNotFoundError(folder)
    digest = hashlib.sha256()
    entries: list[tuple[str, int, int]] = []
    for root, _dirs, files in os.walk(folder):
        for name in files:
            if not is_audio_file(name):
                continue  # sidecars (art, .DS_Store, .lrc) don't identify the album
            path = Path(root) / name
            try:
                stat = path.stat()
            except OSError:
                continue  # vanished mid-walk: identity reflects what remains
            entries.append((path.relative_to(folder).as_posix(), stat.st_size, stat.st_mtime_ns))
    for rel, size, mtime_ns in sorted(entries):
        digest.update(f"{rel}\n{size}\n{mtime_ns}\n".encode())
    return digest.hexdigest()
