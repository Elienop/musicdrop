"""Filesystem predicates for paths that may carry a client-supplied name.

``pathlib``'s ``Path.exists()`` / ``is_dir()`` only absorb ENOENT/ENOTDIR/
EBADF/ELOOP; any OTHER OSError propagates — in particular ENAMETOOLONG
(errno 36) when a path component a client sent exceeds the kernel's
NAME_MAX (255 bytes). Endpoints whose contract is "unknown name -> refuse
(404)" used to 500 from exactly that spot: the not-found check itself was
the thing that blew up, before the ``ValueError``/None refusal could run.

A name the kernel cannot even look up because it is TOO LONG cannot be one
of our entries, so the caller's unknown-name answer applies — these
wrappers answer ``False`` for that one failure and nothing else. Every
other OSError (EACCES, ESTALE, ...) is re-raised: swallowing a permission
or mount failure would convert a real incident into a silent 404. Same
swallow-vs-reraise posture as the ``exists()`` guards in
``app/artwork/cache.py``, scoped to the failure a request can actually
cause.
"""

from __future__ import annotations

import errno
from pathlib import Path


def exists(path: Path) -> bool:
    """``Path.exists`` that refuses an overlong name the way a missing one does."""
    try:
        return path.exists()
    except OSError as exc:
        if exc.errno != errno.ENAMETOOLONG:
            raise
        return False


def is_dir(path: Path) -> bool:
    """``Path.is_dir`` that refuses an overlong name the way a missing one does."""
    try:
        return path.is_dir()
    except OSError as exc:
        if exc.errno != errno.ENAMETOOLONG:
            raise
        return False
