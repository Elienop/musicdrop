"""Build a :class:`BeetsConfigSnapshot` from the in-memory beets config + handle.

Two passes redact secrets before rendering YAML:

1. ``beets.config.flatten(redact=True)`` honors confuse's per-view ``redact``
   flag, which bundled plugins set (e.g. ``spotify.client_secret`` at
   ``beetsplug/spotify.py``). That value comes out as ``"REDACTED"``.
2. A regex safety-net (``SECRET_KEY_PATTERN``) walks the flattened mapping and
   masks any string value whose KEY matches the pattern - protection against
   third-party plugins that forgot to mark their fields ``.redact = True``.

The flattened mapping is a confuse ``OrderedDict`` (a ``dict`` subclass);
PyYAML's ``safe_dump`` refuses non-plain ``dict`` subclasses, so ``_to_plain``
recursively converts every level before rendering.

``find_redacted_paths`` is exported as a module-level helper for the Layer-3
save flow (``app/beets/config_editor.py``): it walks the parsed-YAML map and
returns the dotted paths whose values would be redacted at display time, so
the save merge step can preserve untouched secrets without diffing against the
displayed snapshot.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

import beets
import yaml
from confuse import REDACTED_TOMBSTONE

from app.beets.library import LibraryHandle
from app.models.config_api import BeetsConfigSnapshot

# Field-name pattern for the redaction safety-net. Substring (not anchored) by
# design - confuse's per-view ``redact`` flag is the PRIMARY defense for
# bundled plugins; this safety-net only matters for third-party plugins that
# forgot to mark their fields ``.redact = True``. Trading a few benign
# false-positives for guaranteed coverage of every real-world variant is the
# right call here: a UX wart beats a leaked credential.
#
# catches: client_secret, api_key, api_token, apisecret (beatport),
#          apikey, pwd (kodiupdate), pass, password, auth_token
# over-redacts (harmless): tokenizer, passwordless, secrets (the key itself
#                          is masked; nothing leaks)
# does NOT inspect VALUES: a path like ``directory: /home/me/api_keys`` stays
#                          intact because the key ``directory`` doesn't match.
SECRET_KEY_PATTERN = re.compile(
    r"(secret|token|password|pwd|pass|api_?key|api_?secret|auth_?token)",
    re.IGNORECASE,
)


def find_redacted_paths(data: Any, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Walk a nested mapping (parsed YAML / ruamel ``CommentedMap``) and return
    the dotted paths whose string values would be redacted at display.

    Used by Layer-3 save (``config_editor.merge_preserve_secrets``) to know
    which keys to preserve from disk when the editor still shows ``REDACTED``
    at them. Mirrors the same key-matching policy as
    :func:`_mask_secrets_in_place` (which masks display values); they MUST stay
    in lockstep or the save merge will leak fresh secrets back into the page.
    """
    out: list[tuple[str, ...]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            sub = (*path, str(k))
            if isinstance(v, dict):
                out.extend(find_redacted_paths(v, sub))
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        # NOTE: list-of-dicts walks WITHOUT extending the path
                        # by an index, matching display redaction policy: the
                        # save-flow caller pairs each result with a positional
                        # walk against the on-disk map, so the secret-preserve
                        # merge stays consistent with the visible redaction.
                        out.extend(find_redacted_paths(item, sub))
            elif isinstance(v, str) and SECRET_KEY_PATTERN.search(str(k)):
                out.append(sub)
    return out


def build_config_snapshot(handle: LibraryHandle) -> BeetsConfigSnapshot:
    """Render the current beets config to YAML (redacted) + freshness fields."""
    flat = beets.config.flatten(redact=True)
    _mask_secrets_in_place(flat, SECRET_KEY_PATTERN)
    yaml_text = yaml.safe_dump(_to_plain(flat), sort_keys=False, default_flow_style=False)

    # Single stat() with try/except instead of exists()+stat(): closes a TOCTOU
    # window where the file is deleted between the existence check and the
    # stat call, which would leak FileNotFoundError out of the snapshot
    # builder. OSError also covers permission/IO failures (treated as
    # "missing" for the apply-pending signal).
    file_modified_at: datetime | None = None
    current_mtime: float | None = None
    current_mtime_ns: int = 0
    sha256 = ""
    try:
        st = handle.config_path.stat()
        current_mtime = st.st_mtime
        current_mtime_ns = st.st_mtime_ns  # CPython bpo-39484: integer ns, no float drift
        file_modified_at = datetime.fromtimestamp(current_mtime, tz=UTC)
        sha256 = hashlib.sha256(handle.config_path.read_bytes()).hexdigest()
    except OSError:
        pass

    apply_pending = current_mtime is None or current_mtime > handle.file_mtime_at_load

    return BeetsConfigSnapshot(
        yaml_text=yaml_text,
        config_path=str(handle.config_path),
        loaded_at=handle.loaded_at,
        file_modified_at=file_modified_at,
        mtime_ns=current_mtime_ns,
        sha256=sha256,
        apply_pending=apply_pending,
    )


def _mask_secrets_in_place(d: Any, pattern: re.Pattern[str]) -> None:
    """Recursively walk a flattened-confuse mapping and mask matching keys.

    Descends into both ``dict`` values AND ``list`` values; without the list
    branch a plugin config like ``accounts: [{api_token: "..."}, ...]`` would
    slip through unredacted. Other leaf types (``int``/``bool``) under a
    matching key are left as-is - they're not secrets in any plugin we've seen,
    and forcing them to a string would change the rendered YAML's shape.

    Matching leaves are replaced with confuse's own ``REDACTED_TOMBSTONE``
    sentinel so the two redaction passes agree on a single rendered marker.
    """
    if isinstance(d, list):
        for item in d:
            _mask_secrets_in_place(item, pattern)
        return
    if not isinstance(d, dict):
        return
    for k, v in list(d.items()):
        if isinstance(v, dict | list):
            _mask_secrets_in_place(v, pattern)
        elif isinstance(v, str) and pattern.search(k):
            d[k] = REDACTED_TOMBSTONE


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
