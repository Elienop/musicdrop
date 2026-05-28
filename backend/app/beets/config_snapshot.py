"""Build a :class:`BeetsConfigSnapshot` from the in-memory beets config + handle.

Two passes redact secrets before rendering YAML:

1. ``beets.config.flatten(redact=True)`` honors confuse's per-view ``redact``
   flag, which bundled plugins set (e.g. ``spotify.client_secret`` at
   ``beetsplug/spotify.py``). That value comes out as ``"REDACTED"``.
2. A regex safety-net (``SECRET_KEY_PATTERN``) walks the flattened mapping and
   masks any string value whose KEY matches the pattern — protection against
   third-party plugins that forgot to mark their fields ``.redact = True``.

The flattened mapping is a confuse ``OrderedDict`` (a ``dict`` subclass);
PyYAML's ``safe_dump`` refuses non-plain ``dict`` subclasses, so ``_to_plain``
recursively converts every level before rendering.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import beets
import yaml

from app.beets.library import LibraryHandle
from app.models.config_api import BeetsConfigSnapshot

# Field-name pattern for the redaction safety-net. Anchored to the KEY only —
# we never inspect values, so a secret-looking VALUE under an innocuous key
# (e.g. ``directory: /home/me/api_keys``) is left alone.
SECRET_KEY_PATTERN = re.compile(r"(secret|token|password|apikey|api_key|auth_token)", re.IGNORECASE)
REDACTED = "REDACTED"


def build_config_snapshot(handle: LibraryHandle) -> BeetsConfigSnapshot:
    """Render the current beets config to YAML (redacted) + freshness fields."""
    flat = beets.config.flatten(redact=True)
    _mask_secrets_in_place(flat, SECRET_KEY_PATTERN)
    yaml_text = yaml.safe_dump(_to_plain(flat), sort_keys=False, default_flow_style=False)

    file_missing = not handle.config_path.exists()
    file_modified_at: datetime | None = None
    current_mtime: float | None = None
    if not file_missing:
        current_mtime = handle.config_path.stat().st_mtime
        file_modified_at = datetime.fromtimestamp(current_mtime, tz=UTC)

    restart_required = file_missing or (
        current_mtime is not None and current_mtime > handle.file_mtime_at_load
    )

    return BeetsConfigSnapshot(
        yaml_text=yaml_text,
        config_path=str(handle.config_path),
        loaded_at=handle.loaded_at,
        file_modified_at=file_modified_at,
        restart_required=restart_required,
    )


def _mask_secrets_in_place(d: Any, pattern: re.Pattern[str]) -> None:
    """Recursively walk a flattened-confuse mapping and mask matching keys.

    Only string leaves are masked; nested ``dict``s are descended. Other leaf
    types (``int``/``bool``/``list``) under a matching key are left as-is —
    they're not secrets in any plugin we've seen, and forcing them to a string
    would change the rendered YAML's shape.
    """
    if not isinstance(d, dict):
        return
    for k, v in list(d.items()):
        if isinstance(v, dict):
            _mask_secrets_in_place(v, pattern)
        elif isinstance(v, str) and pattern.search(k):
            d[k] = REDACTED


def _to_plain(d: Any) -> Any:
    """Convert nested confuse ``OrderedDict`` to plain ``dict`` for PyYAML.

    ``yaml.safe_dump`` only knows how to represent plain ``dict``/``list``/
    scalars; any subclass (including stdlib ``OrderedDict``) raises a
    ``RepresenterError``. Recurse so every nested mapping is converted.
    """
    if isinstance(d, dict):
        return {k: _to_plain(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_to_plain(v) for v in d]
    return d
