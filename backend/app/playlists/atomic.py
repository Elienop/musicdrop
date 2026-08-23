"""Crash-safe atomic file write, shared by the playlist store and the
`.m3u8` export so the recipe lives in exactly one place.

The primitive is ``write_atomic_bytes`` (raw bytes, encoding-agnostic); the
``write_atomic_text`` wrapper delegates to it with strict UTF-8, so text
callers keep their exact semantics — and the ``.m3u8`` export can write bytes
carrying undecodable paths (``surrogateescape``-encoded) that strict UTF-8
text output would reject.

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
import secrets
from pathlib import Path


def write_atomic_bytes(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    """Atomically (re)write ``path`` with ``data``, creating parents.

    The bytes sink of the shared atomic recipe: the bytes are written exactly
    as given, with no encoding step — callers that need text pass strict
    UTF-8 (see ``write_atomic_text``); callers that need undecodable path
    bytes pass them already encoded (see the ``.m3u8`` export).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # A UNIQUE tmp name per writer (not a fixed ``.<name>.tmp``) so two concurrent
    # writers of the same target never share one inode. With a shared tmp, writer
    # B's ``O_TRUNC`` wipes writer A's just-written bytes and A's ``os.replace``
    # then publishes truncated content or raises on the vanished tmp — the export
    # self-corrupts. The ``.m3u8`` export runs unserialized on the threadpool
    # (only the JSON store is locked), so overlapping mutations of one playlist
    # genuinely race here. ``os.replace`` still makes the publish atomic, so the
    # last writer wins with an intact file.
    tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    try:
        # Create the tempfile with the final mode UP FRONT (not a default-mode
        # create then chmod), so a secret written with mode=0o600 is never even
        # briefly world-readable in the create->chmod window. The chmod that
        # follows only pins the exact mode (os.open honours umask; chmod doesn't).
        # O_EXCL: the random name is our own fresh inode, never an existing one.
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
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


def write_atomic_text(path: Path, text: str, *, mode: int = 0o644) -> None:
    """Atomically (re)write ``path`` with ``text`` as STRICT UTF-8, creating
    parents.

    Delegates to the shared bytes sink; the strict ``.encode`` here is the only
    encoding step, so undecodable (lone-surrogate) strings still raise
    ``UnicodeEncodeError`` exactly as before — this is deliberate for the text
    callers (JSON stores, configs) and NOT the ``.m3u8`` export, which encodes
    with ``surrogateescape`` and writes bytes directly.
    """
    write_atomic_bytes(path, text.encode("utf-8"), mode=mode)
