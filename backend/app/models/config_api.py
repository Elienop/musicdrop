"""Pydantic contract for the read-only Config view.

Lives outside ``app/beets/`` deliberately: it must not ``import beets``. The
adapter at ``app/beets/config_snapshot.py`` builds instances of these models;
everything that crosses an HTTP boundary takes/returns them.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

#: What beets does with an album's files on import, after its own precedence
#: (``app/beets/import_operation.py::file_operation``). ``reflink_auto`` is
#: ``reflink: auto``, which copies where a clone can't be made.
FileOperation = Literal["move", "copy", "link", "hardlink", "reflink", "reflink_auto", "in_place"]


class ImportOperation(BaseModel):
    """What imports do with the files, as loaded at boot or by the last Apply."""

    operation: FileOperation


class SetImportOperation(BaseModel):
    """Keep downloads on (beets hardlinks) or off (beets moves), then reload beets.

    ``base_sha256`` is ``GET /api/config``'s ``sha256``: a file changed since
    then is refused, as a Save is.
    """

    keep_downloads: bool
    base_sha256: str


class BeetsConfigSnapshot(BaseModel):
    """Read-only snapshot of beets' effective config + file freshness."""

    yaml_text: str
    """The RAW on-disk ``config.yaml`` text — the editable document. Comments,
    anchors, key order and quoting are preserved verbatim (secrets are NOT
    masked here: this is the user's own file, and ``POST /config/save`` writes it
    back as-is). Empty string if the file is missing, unreadable, not a regular
    file or not UTF-8."""

    effective_yaml: str
    """The fully-merged EFFECTIVE config (beets + every loaded plugin's defaults)
    rendered as YAML with secrets redacted — a READ-ONLY view for the editor's
    'effective config' panel. Never written back; ``yaml_text`` is the source of
    truth for saves."""

    config_path: str
    """Absolute path to the user-owned ``<BEETSDIR>/config.yaml``."""

    loaded_at: datetime
    """When beets last loaded config.yaml, at boot or Apply (UTC). The in-memory
    snapshot is from this moment."""

    file_modified_at: datetime | None
    """Current ``st_mtime`` of ``config_path`` (UTC). ``None`` if the file is missing."""

    sha256: str
    """Hex SHA-256 of the on-disk file bytes at GET time. The sole
    optimistic-concurrency token used by ``POST /api/config/save``. mtime_ns
    is not echoed back because nanosecond ints exceed JavaScript's
    ``Number.MAX_SAFE_INTEGER`` (2^53 - 1), which would silently corrupt the
    CAS round-trip; the SHA-256 already catches any bytes-changed edit
    (including ones that preserved mtime via ``os.utime``). Empty string when the
    file is missing, unreadable or not a regular file."""

    apply_pending: bool
    """``True`` when the file is missing OR its mtime exceeds ``file_mtime_at_load``.

    (Was ``restart_required`` in Layers 1+2 — semantics unchanged; name updated
    for the Layer-3 Apply button that replaces the restart instruction.)"""
