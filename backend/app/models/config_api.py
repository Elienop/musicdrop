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

    restart_required: bool
    """``True`` when the file is missing OR its mtime exceeds ``file_mtime_at_load``."""
