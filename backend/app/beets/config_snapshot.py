"""Build a :class:`BeetsConfigSnapshot` from the in-memory beets config + handle.

The snapshot carries two YAML views:

* ``yaml_text`` — the RAW on-disk ``config.yaml``, byte-for-byte. This is the
  editable document; Save writes it back verbatim, so it is served unredacted.
* ``effective_yaml`` — the fully-merged effective config (read-only), with
  secrets redacted in two passes:

  1. ``beets.config.flatten(redact=True)`` honors confuse's per-view ``redact``
     flag, which bundled plugins set (e.g. ``spotify.client_secret`` at
     ``beetsplug/spotify.py``). That value comes out as ``"REDACTED"``.
  2. A regex safety-net (``SECRET_KEY_PATTERN``) walks the flattened mapping and
     masks any string value whose KEY matches the pattern — protection against
     third-party plugins that forgot to mark their fields ``.redact = True``.

``_plain_redacted`` applies that second pass while COPYING: ``flatten()`` only
rebuilds mapping levels and returns the live object for everything else, so
masking in place reached back into ``beets.config`` and destroyed list-nested
credentials in the running process. The same pass also converts every confuse
``OrderedDict`` (a ``dict`` subclass) to a plain ``dict``, which PyYAML's
``safe_dump`` requires.
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
# design — confuse's per-view ``redact`` flag is the PRIMARY defense for
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


def build_config_snapshot(handle: LibraryHandle) -> BeetsConfigSnapshot:
    """Build the config snapshot: the RAW on-disk file (editable) + the merged
    effective view (read-only, redacted) + freshness fields."""
    # Effective (read-only) view — the fully-merged config incl. beets + every
    # loaded plugin's defaults, secrets redacted. Two passes: confuse's per-view
    # ``redact`` flag, then the SECRET_KEY_PATTERN safety-net for third-party
    # plugins that forgot to mark their fields ``.redact = True``.
    flat = beets.config.flatten(redact=True)
    plain = _plain_redacted(flat, None, SECRET_KEY_PATTERN)
    effective_yaml = yaml.safe_dump(plain, sort_keys=False, default_flow_style=False)

    # Editable document — the user's own config.yaml, byte-for-byte (comments,
    # anchors, key order and quoting preserved). Served RAW, NOT redacted: Save
    # writes it back verbatim, so this is the source of truth. Masking here is
    # exactly what used to destroy comments and overwrite list-nested credentials
    # with "REDACTED" on the round-trip.
    #
    # Read the bytes ONCE (sha + text from the same read) inside a single
    # try/except instead of exists()+stat(): closes the TOCTOU window where the
    # file is deleted mid-check and leaks FileNotFoundError. OSError covers
    # missing/permission/IO failures; UnicodeDecodeError (a ValueError, NOT an
    # OSError) covers a config corrupted to non-UTF-8. Both degrade to the empty
    # editable doc rather than 500-ing the settings page — exactly when a user
    # opens Settings to fix a broken config. The effective view still renders
    # from the in-memory beets.config either way.
    file_modified_at: datetime | None = None
    current_mtime: float | None = None
    sha256 = ""
    yaml_text = ""
    try:
        raw = handle.config_path.read_bytes()
        current_mtime = handle.config_path.stat().st_mtime
        file_modified_at = datetime.fromtimestamp(current_mtime, tz=UTC)
        sha256 = hashlib.sha256(raw).hexdigest()
        yaml_text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        pass

    apply_pending = current_mtime is None or current_mtime > handle.file_mtime_at_load

    return BeetsConfigSnapshot(
        yaml_text=yaml_text,
        effective_yaml=effective_yaml,
        config_path=str(handle.config_path),
        loaded_at=handle.loaded_at,
        file_modified_at=file_modified_at,
        sha256=sha256,
        apply_pending=apply_pending,
    )


def _plain_redacted(value: Any, key: str | None, pattern: re.Pattern[str]) -> Any:
    """Return a fresh, plain, secret-masked copy of a flattened-confuse value.

    Masking and plain-ifying are ONE pass on purpose: masking in place is not an
    option here. ``View.flatten()`` rebuilds each *mapping* level but falls back
    to ``view.get()`` for anything that isn't a mapping, which hands back the
    LIVE object — so a list-nested credential (``kodiupdate.kodi[].pwd``) lives
    in the same list the running config holds. Writing the tombstone into it
    destroyed that credential in-process on every Settings load. Building new
    containers on the way out means the returned tree shares no ``dict`` or
    ``list`` with ``beets.config``, so the mutation is structurally impossible
    rather than merely avoided by discipline. (An exotic mutable leaf — a
    ``!!set`` in the YAML — is still passed through by identity, but nothing
    here writes into a leaf, only rebinds the key above it.) (The same masking-in-place mistake once
    ate comments and list-nested credentials on the editable document; the
    effective view had kept its own copy of the bug.)

    Plain-ifying is required by PyYAML: ``yaml.safe_dump`` only represents plain
    ``dict``/``list``/scalars, and any subclass — including the confuse
    ``OrderedDict`` every flattened level is — raises a ``RepresenterError``.

    Masking rules (unchanged): a ``str`` leaf whose own KEY matches ``pattern``
    becomes confuse's ``REDACTED_TOMBSTONE``, so both redaction passes render
    one marker. ``dict`` and ``list`` values are recursed into — without the
    list branch a plugin config like ``accounts: [{api_token: "..."}, ...]``
    would slip through. A list does NOT propagate its own key to its items
    (``key=None``), so a bare list of strings under a matching key is left
    alone, matching the previous behavior. Non-``str`` leaves (``int``/``bool``)
    under a matching key stay as-is — they're not secrets in any plugin we've
    seen, and stringifying them would change the rendered YAML's shape.
    """
    if isinstance(value, dict):
        return {k: _plain_redacted(v, k, pattern) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain_redacted(item, None, pattern) for item in value]
    if key is not None and isinstance(value, str) and pattern.search(key):
        return REDACTED_TOMBSTONE
    return value
