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
    # ``base_url`` is admin-controlled (only the single self-hosted owner sets it
    # via Settings), so the "the server connects to this URL" SSRF surface is
    # mitigated by the single-user threat model — we don't restrict it.
    base_url: str = ""
    token: str = ""  # the slskd API key
    downloads_prefix: str = ""  # slskd's container-namespace download root (stripped on remap)
    webhook_secret: str = ""  # the shared secret the inbound webhook authenticates with
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
