"""Pydantic contract for the read-only Config view.

Lives outside ``app/beets/`` deliberately: it must not ``import beets``. The
adapter at ``app/beets/config_snapshot.py`` builds instances of these models;
everything that crosses an HTTP boundary takes/returns them.
"""

from datetime import datetime

from pydantic import BaseModel


class BeetsConfigSnapshot(BaseModel):
    """Read-only snapshot of beets' effective config + file freshness."""

    yaml_text: str
    """Rendered post-merge effective config as YAML, with secrets redacted."""

    config_path: str
    """Absolute path to the user-owned ``<BEETSDIR>/config.yaml``."""

    loaded_at: datetime
    """When ``setup_beets()`` ran (UTC). The in-memory snapshot is from this moment."""

    file_modified_at: datetime | None
    """Current ``st_mtime`` of ``config_path`` (UTC). ``None`` if the file is missing."""

    mtime_ns: int
    """``st_mtime_ns`` at GET time. Used as the optimistic-concurrency token by
    ``POST /api/config/save`` together with :attr:`sha256` (CPython bpo-39484:
    nanosecond integer avoids the float-precision loss of ``st_mtime``)."""

    sha256: str
    """Hex SHA-256 of the on-disk file bytes at GET time. Tie-breaker for the
    Save-time CAS check - catches edits that preserved mtime via ``os.utime``."""

    apply_pending: bool
    """``True`` when the file is missing OR its mtime exceeds ``file_mtime_at_load``.

    (Was ``restart_required`` in Layers 1+2 - semantics unchanged; name updated
    for the Layer-3 Apply button that replaces the restart instruction.)"""
