"""Persisted slskd configuration (base URL + API key + downloads prefix +
webhook secret + auto-import toggle).

Mirrors ``app/plex/config.py`` exactly: the API key and the webhook secret are
secrets — stored in an owner-only (``0o600``) JSON file and never returned by the
API (the settings endpoint exposes only ``has_token``). Env vars seed the INITIAL
values; once saved via the UI the JSON file wins. Uses the shared crash-safe text
writer with the owner-only mode.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from app.playlists.atomic import write_atomic_text

# The slskd config file holds the API key (account control) AND the webhook
# secret (lets a caller trigger an import) — both secrets, so owner-only, like
# the Plex admin token file, not world-readable like the playlist/`.m3u8` files.
_TOKEN_FILE_MODE = 0o600


class SlskdConfig(BaseModel):
    # ``base_url`` is deliberately unrestricted, gated the same way its Plex twin
    # is: its runtime writer is ``PUT /api/slskd/settings`` (app/api/slskd.py:83),
    # under ``/api/`` and not in ``EXEMPT_PATHS``, so the session gate
    # (app/auth/gate.py) requires a cookie. (``MUSICDROP_SLSKD_URL`` also seeds it
    # via ``env_defaults`` — the operator's own deployment input, not a request
    # path.) Do NOT restrict this with ``assert_public_url``
    # (app/artwork/download.py:38): it rejects ``http://slskd:5030`` (this repo's
    # own test value) and every other private-address slskd, which is all of them.
    # Beware the shape of the retired justification, not just its words: "only the
    # owner can set it, so the threat model covers it" was FALSE here for months,
    # and it is the pattern to refuse — name the mechanism that enforces a claim.
    #
    # Two things make this field's SSRF worse than the Plex twin's. Note the
    # difference is NOT credential exfiltration — Plex leaks a higher-value
    # credential, by header, on its own primary path (see ``PlexConfig.base_url``).
    # It is these:
    #
    # 1. It is REFLECTED, not blind. ``client.check`` (app/slskd/client.py:13)
    #    returns the response body to the caller UNTRUNCATED, surfaced as
    #    ``SlskdConnection.version``, so an internal endpoint's body can be read
    #    back out through ``POST /api/slskd/test``.
    # 2. The caller chooses the whole PATH. The appended
    #    ``/api/v0/application/version`` is no constraint: a trailing ``#`` pushes
    #    it into the fragment. Measured, with the mitigations that do apply, in
    #    BACKLOG.md — kept in one place so the two copies cannot drift apart.
    #
    # And one hypothesis that was tested and REFUTED, recorded because a reader
    # who knows ``/api/slskd/webhook`` is gate-exempt will reasonably doubt the
    # paragraph above: the webhook does NOT reach this field. Verified against the
    # handler (app/api/slskd.py:150) — it reads only ``webhook_secret`` (in its
    # auth dependency), ``auto_import``, ``downloads_prefix``,
    # ``app.state.inbox_dir`` and the queue. There is no anonymous path here.
    base_url: str = ""
    token: str = ""  # the slskd API key
    downloads_prefix: str = ""  # slskd's container-namespace download root (stripped on remap)
    # Authenticates the inbound webhook — the sole credential in front of the one
    # gate-exempt mutating route. Live footgun: ``update`` treats ``None`` as keep
    # but ``""`` as a real write, so blanking this succeeds and
    # ``require_webhook_secret`` then rejects EVERY delivery, silently killing
    # auto-import. Hardening gaps (no min length, no throttle) are in BACKLOG.md.
    webhook_secret: str = ""
    auto_import: bool = False  # the operative toggle: a completed drop imports itself iff True


class SlskdConfigStore:
    def __init__(self, path: Path | str, *, env_defaults: SlskdConfig) -> None:
        self._path = Path(path)
        self._config = self._load(env_defaults)

    def _load(self, env_defaults: SlskdConfig) -> SlskdConfig:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return env_defaults
        try:
            return SlskdConfig.model_validate_json(raw)
        except ValueError:
            return env_defaults

    def get(self) -> SlskdConfig:
        return self._config

    def is_configured(self) -> bool:
        return bool(self._config.base_url and self._config.token)

    def update(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        downloads_prefix: str | None = None,
        webhook_secret: str | None = None,
        auto_import: bool | None = None,
    ) -> SlskdConfig:
        # Every field's keep-current sentinel is ``None`` — including
        # ``auto_import`` (NOT ``False``), so omitting it from a PUT never
        # silently turns auto-import off.
        current = self._config
        self._config = SlskdConfig(
            base_url=current.base_url if base_url is None else base_url,
            token=current.token if token is None else token,
            downloads_prefix=(
                current.downloads_prefix if downloads_prefix is None else downloads_prefix
            ),
            webhook_secret=current.webhook_secret if webhook_secret is None else webhook_secret,
            auto_import=current.auto_import if auto_import is None else auto_import,
        )
        write_atomic_text(
            self._path,
            self._config.model_dump_json(indent=2),
            mode=_TOKEN_FILE_MODE,
        )
        return self._config
