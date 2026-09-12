"""Crash-safe atomic file write, shared by the playlist store and the
`.m3u8` export so the recipe lives in exactly one place.

The primitive is ``write_atomic_bytes`` (raw bytes, encoding-agnostic); the
``write_atomic_text`` wrapper delegates to it with strict UTF-8, so text
callers keep their exact semantics — and the ``.m3u8`` export can write bytes
carrying undecodable paths (``surrogateescape``-encoded) that strict UTF-8
text output would reject.

Recipe: create a temp in the SAME directory, exclusively and without following
a symlink, under a name nobody can precompute -> write + ``fsync`` -> set the
mode on the temp's own fd -> publish with ``os.replace`` THROUGH the
directory's descriptor -> ``fsync`` that same descriptor. The parent-dir fsync
forces the rename durable so a power-cut can't lose the new file.

Every name this module touches is resolved against a directory descriptor, so
a caller that opened the directory safely (``dir_fd=``) keeps that guarantee:
nothing here re-resolves the path, and a component swapped for a symlink
mid-write cannot move the publish. Without ``dir_fd`` the helper creates the
parents and opens them by name itself.

``mode`` defaults to world-readable ``0o644`` (fine for playlists/`.m3u8`);
callers writing a secret (e.g. the Plex admin token) pass ``mode=0o600`` so the
file is owner-only, and ``mode=None`` preserves an existing regular file's mode
on rewrite (the library-side writers, which must not reset a tightened file).
"""

from __future__ import annotations

import os
import re
import secrets
import stat as stat_mod
import time
from contextlib import suppress
from pathlib import Path

#: ``O_EXCL`` refuses an existing path instead of opening it, ``O_NOFOLLOW``
#: refuses a symlink: a planted symlink at the temp path was followed once and
#: ``os.replace`` published the LINK as the destination (measured on the art
#: writer: ``library.db`` overwritten with JPEG bytes).
_TMP_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW

#: This writer's temp shape, ``.<pid>.<16 hex><suffix>.tmp``, and the only shape
#: :func:`_sweep_stale_temps` removes. The target's name is NOT embedded, so the
#: decoration is a constant 33 bytes (7-digit ``pid_max``) instead of growing
#: with the destination's name, and a name nobody can precompute keeps two
#: concurrent writers of one target off a single inode — with a shared
#: ``.<name>.tmp``, writer B's ``O_TRUNC`` wipes writer A's bytes and A
#: publishes truncated content. The art and lyrics writers each had their own
#: copy of this shape; both were deleted and both now call this helper.
_TMP_NAME_RE = re.compile(r"^\.\d+\.[0-9a-f]{16}(\.[^./]*)?\.tmp$")

#: A temp file older than this hour was left by a process killed mid-write; no
#: live write holds one that long. Leftovers in the OLD shape
#: (``.<name>.<pid>.<hex>.tmp``) and ``artwork.cache``'s name-embedding shape are
#: deliberately not swept: the regex above cannot tell them from a target.
_STALE_TMP_AGE_S = 3600


def _tmp_name(name: str) -> str:
    """The temp sibling's bare name, picked per call (see :data:`_TMP_NAME_RE`)."""
    suffix = os.path.splitext(name)[1]
    return f".{os.getpid()}.{secrets.token_hex(8)}{suffix}.tmp"


def _existing_regular_mode(name: str, dir_fd: int) -> int | None:
    """``name``'s mode if it is a regular file, else ``None``.

    ``follow_symlinks=False``: a symlink at the destination donates no mode (it
    is replaced by a regular file, and its target is left alone).
    """
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return None
    if not stat_mod.S_ISREG(st.st_mode):
        return None
    return stat_mod.S_IMODE(st.st_mode)


def _sweep_stale_temps(dir_fd: int) -> None:
    """Unlink this writer's own abandoned temps in ``dir_fd``, nothing else.

    Regularity is checked with ``follow_symlinks=False``, so a matching-named
    symlink is left in place and its target is never touched.
    """
    cutoff = time.time() - _STALE_TMP_AGE_S
    # Closed before the stat/unlink loop: an fd scandir DUPS the fd but SHARES
    # its offset, so an iterator left open makes every later enumeration of that
    # fd read [] (measured). Never two live on one fd.
    with os.scandir(dir_fd) as entries:
        candidates = [entry.name for entry in entries if _TMP_NAME_RE.match(entry.name)]
    for name in candidates:
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            continue
        if stat_mod.S_ISREG(st.st_mode) and st.st_mtime < cutoff:
            with suppress(OSError):
                os.unlink(name, dir_fd=dir_fd)


def _write_through_dir_fd(name: str, data: bytes, *, mode: int | None, dir_fd: int) -> None:
    """Write ``data`` and publish it as ``name``, every path resolved against
    ``dir_fd`` alone.

    The temp is cleared on every exit: whatever sits at that name is ours,
    unless something guessed the name this call picked and got there first — in
    which case the create already failed and this unlinks the squatter.
    """
    _sweep_stale_temps(dir_fd)
    # The preserved mode is read BEFORE the create so the temp is never briefly
    # looser than the file it replaces. mode=None with no regular file there
    # (absent, symlink, directory) takes the umask default from 0o666.
    final = _existing_regular_mode(name, dir_fd) if mode is None else mode
    tmp = _tmp_name(name)
    try:
        # Carry the final mode on the CREATE (not a default-mode create then
        # chmod), so a secret written with mode=0o600 is never world-readable.
        fd = os.open(tmp, _TMP_CREATE_FLAGS, 0o666 if final is None else final, dir_fd=dir_fd)
        try:
            stream = os.fdopen(fd, "wb")
        except BaseException:  # pragma: no cover - fdopen fails only on a bad fd
            os.close(fd)
            raise
        with stream as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            if final is not None:
                os.fchmod(handle.fileno(), final)  # exact mode; the create honours umask
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        with suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)


def write_atomic_bytes(
    path: Path, data: bytes, *, mode: int | None = 0o644, dir_fd: int | None = None
) -> None:
    """Atomically (re)write ``path`` with ``data``.

    The bytes sink of the shared atomic recipe: the bytes are written exactly
    as given, with no encoding step — callers that need text pass strict
    UTF-8 (see ``write_atomic_text``); callers that need undecodable path
    bytes pass them already encoded (see the ``.m3u8`` export).

    ``mode``: an int is applied to the temp's fd, so it is the published file's
    mode exactly. ``None`` preserves the existing REGULAR file's mode on
    rewrite and otherwise takes the umask default — a symlink at ``path`` is
    replaced by a regular file and donates no mode (its target is untouched); a
    DIRECTORY at ``path`` makes ``os.replace`` raise EISDIR and the temp is
    cleaned up.

    ``dir_fd``: when given, ``path.parent`` is never opened and no directory is
    created — only ``path.name`` is used, resolved against that descriptor.
    Without it the parents are created and opened here; no ``O_NOFOLLOW`` on
    that open, because an operator's beets directory may legitimately be a
    symlink.
    """
    if dir_fd is not None:
        _write_through_dir_fd(path.name, data, mode=mode, dir_fd=dir_fd)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_DIRECTORY: a FIFO swapped in here blocks forever without it (measured: 2 s, no error).
    owned = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _write_through_dir_fd(path.name, data, mode=mode, dir_fd=owned)
    finally:
        os.close(owned)


def write_atomic_text(
    path: Path, text: str, *, mode: int | None = 0o644, dir_fd: int | None = None
) -> None:
    """Atomically (re)write ``path`` with ``text`` as STRICT UTF-8.

    Delegates to the shared bytes sink; the strict ``.encode`` here is the only
    encoding step, so undecodable (lone-surrogate) strings still raise
    ``UnicodeEncodeError`` exactly as before — this is deliberate for the text
    callers (JSON stores, configs) and NOT the ``.m3u8`` export, which encodes
    with ``surrogateescape`` and writes bytes directly.
    """
    write_atomic_bytes(path, text.encode("utf-8"), mode=mode, dir_fd=dir_fd)
