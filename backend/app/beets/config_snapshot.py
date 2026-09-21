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
     masks any value whose KEY matches the pattern — protection against
     third-party plugins that forgot to mark their fields ``.redact = True``,
     AND against pass 1 skipping a subtree entirely (see below).

Pass 1 is NOT a floor. ``View.flatten`` does ``try: view.flatten() except
ConfigTypeError: view.get()``, so a view that is not a mapping is dumped
WHOLESALE by ``view.get()`` and every ``.redact`` flag inside it is ignored.
Any plugin whose config is list-shaped lands there. ``beetsplug/kodiupdate.py``
registers the section ``kodi`` (``super().__init__("kodi")``) and adds a LIST as
its default (``[{"host": ..., "user": ..., "pwd": "kodi"}]``), then marks
``pwd.redact = True``. So the on-disk section a user writes is ``kodi``, not
``kodiupdate``::

    kodi:
      - host: 10.0.0.5
        port: 8080
        user: myuser
        pwd: 4815162342

and the ``redact`` flag buys nothing for it::

    beets.config["kodi"].flatten(redact=True)
    -> ConfigTypeError: kodi must be a dict, not list
    beets.config.flatten(redact=True)["kodi"]
    -> [{'host': '10.0.0.5', 'port': 8080, 'user': 'myuser', 'pwd': 4815162342}]

For those plugins pass 2 is the ONLY defence. Treat it as load-bearing, not as
a belt-and-braces extra.

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
# catches, with the SECTION each bundled plugin actually registers (the section
# is often not the plugin's module name — kodiupdate registers ``kodi``):
#   spotify.client_secret, lyrics.genius_api_key, beatport.{apikey,apisecret},
#   kodi[].pwd, subsonic.pass, emby.{password,apikey}, plex.token, auth_token
# and a key named ``key`` or ending ``_key``: fetchart.{fanarttv,google,
#   lastfm}_key leaked whenever fetchart was not loaded. Measured over beets
#   2.13's bundled defaults (61 plugins loaded, the other 18 grepped), those
#   three are the only default keys this arm adds.
# over-redacts (harmless): tokenizer, passwordless, secrets, and one real
#                          bundled default — ``spotify.tokenfile``, a FILENAME.
#                          The key itself is masked; nothing leaks.
# does NOT inspect VALUES: a path like ``directory: /home/me/api_keys`` stays
#                          intact because the key ``directory`` doesn't match.
SECRET_KEY_PATTERN = re.compile(
    r"(secret|token|password|pwd|pass|api_?key|api_?secret|auth_?token|(?:^|_)key$)",
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


def _plain_redacted(value: Any, key: object, pattern: re.Pattern[str]) -> Any:
    """Return a fresh, plain, secret-masked copy of a flattened-confuse value.

    Masking and plain-ifying are ONE pass on purpose: masking in place is not an
    option here. ``View.flatten()`` rebuilds each *mapping* level but falls back
    to ``view.get()`` for anything that isn't a mapping, which hands back the
    LIVE object — so a list-nested credential (``kodi[].pwd``, the list-shaped
    section kodiupdate registers) lives in the same list the running config
    holds: ``beets.config.flatten(redact=True)["kodi"] is
    beets.config["kodi"].get()`` is True. Writing the tombstone into it
    destroyed that credential in-process on every Settings load. Building new
    containers on the way out means the returned tree shares no ``dict`` or
    ``list`` with ``beets.config``, so the mutation is structurally impossible
    rather than merely avoided by discipline. An exotic mutable leaf — a
    ``!!set`` in the YAML — is still passed through by identity, but nothing
    here writes into a leaf, only rebinds the key above it. The same
    masking-in-place mistake once ate comments and list-nested credentials on
    the editable document; the effective view had kept its own copy of the bug.

    Plain-ifying is required by PyYAML: ``yaml.safe_dump`` only represents plain
    ``dict``/``list``/scalars, and any subclass — including the confuse
    ``OrderedDict`` every flattened level is — raises a ``RepresenterError``.

    Masking rules. ``dict`` and ``list`` values are recursed into first —
    without the list branch a plugin config like
    ``accounts: [{api_token: "..."}, ...]`` would slip through. A list does NOT
    propagate its own key to its items (``key=None``), so a bare list of strings
    under a matching key is left alone; that is a deliberate behavior decision,
    kept separate. Any remaining leaf whose own KEY matches ``pattern`` becomes
    confuse's ``REDACTED_TOMBSTONE``, so both redaction passes render one marker.

    The leaf rule is type-blind ON PURPOSE — do not reintroduce an
    ``isinstance(value, str)`` filter. It was there once, justified as "non-str
    leaves aren't secrets in any plugin we've seen". That reasoning is about
    plugin DEFAULTS and does not survive contact with a user-authored file: YAML
    types the VALUE, and an unquoted numeric password is an ``int``::

        pwd: "4815162342"   -> redacted
        pwd: 4815162342     -> 4815162342, in cleartext, in a pane captioned
                               "with secrets redacted"

    ``float``, ``bool``, ``!!binary`` bytes and ``!!set`` leaked identically. A
    changed YAML shape in the read-only view is not a cost worth one leaked
    credential — and for a list-shaped plugin config (see the module docstring)
    this pass is the only thing standing between that value and the browser.

    ``None`` is the one carve-out: a null under a secret-matching key stays
    null. The argument is cost/benefit and needs no survey of what plugins ship.
    A null holds no credential, so masking it cannot prevent a leak — the
    security benefit is exactly zero. The cost is not: this pass matches on KEY
    NAME only and over-matches by design (``spotify.tokenfile`` is a filename),
    so masking a null would make a pane captioned "with secrets redacted"
    assert that a credential is configured on a field we merely GUESSED was
    secret, and that the user can see is empty in their own file.

    Note this disagrees with pass 1, which renders a ``.redact``-marked null as
    ``"REDACTED"`` — so a bundled plugin's unset credential (``emby.password``
    ships as ``None``) is masked before this pass ever sees it. That asymmetry
    is upstream behavior, and it errs in the safe direction: pass 1 only speaks
    for fields a plugin author declared secret, whereas this pass is guessing
    from the key name.

    Be aware this makes the two passes disagree on exactly one input: confuse's
    pass 1 DOES mask a null. ``beets.config["x"]["api_key"].set(None)`` plus
    ``.redact = True`` flattens to ``"REDACTED"``, so a plugin-marked null and
    a safety-net-matched null render differently in the same document. That is
    upstream behaviour we do not control (pass 1 runs inside confuse), and the
    disagreement is in the safe direction — pass 2 reveals a null, never a
    value. Do not "fix" it by masking nulls here without re-deciding the above.

    The KEY guard is ``isinstance(key, str)``, not ``key is not None``. YAML
    keys are not necessarily strings: ``substitute: {112: One Twelve}`` (a band
    name) gives an ``int`` key, and ``types: {no: int}`` gives ``False``,
    because YAML 1.1 resolves a bare ``no``. Handing either to
    ``re.Pattern.search`` raises ``TypeError`` and 500s ``GET /api/config`` —
    permanently, since Settings is both the default landing page and the only
    in-app editor for the file that causes it. Note the crash needs a ``str``
    VALUE under that key; the old ``isinstance(value, str)`` check short-
    circuited the int-valued case, which is why it looked survivable.

    ``key`` is typed ``object`` rather than ``str | None`` for the same reason.
    It arrives from an ``Any``-typed comprehension over the flattened config, so
    a ``str | None`` annotation was a lie mypy had no way to check — under it,
    ``key is not None`` reads as a valid narrowing and strict mode stays silent.
    ``object`` forces the ``isinstance`` at the type level too, so the runtime
    hole cannot be reopened without mypy objecting.
    """
    if isinstance(value, dict):
        return {k: _plain_redacted(v, k, pattern) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain_redacted(item, None, pattern) for item in value]
    if value is not None and isinstance(key, str) and pattern.search(key):
        return REDACTED_TOMBSTONE
    return value
