"""Persisted Plex configuration (base URL + admin token + library-path mapping).

The token is a secret: it is stored here but never returned by the API (the
settings endpoint exposes only ``has_token``). Env vars seed the INITIAL value;
once saved via the UI the JSON file wins. Mirrors the artist-image toggle's
file-over-env mechanic, using the shared crash-safe text writer.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from app.playlists.atomic import write_atomic_text

# The Plex token file is owner-only — it holds the admin token (full server
# control), so it must not be world-readable like the playlist/`.m3u8` files.
_TOKEN_FILE_MODE = 0o600


class PlexConfig(BaseModel):
    # ``base_url`` is admin-controlled (only the single self-hosted owner can set
    # it via Settings), so the SSRF surface of "the server connects to this URL"
    # is mitigated by the single-user threat model — we don't restrict it.
    base_url: str = ""
    token: str = ""
    library_path: str = ""  # Plex-visible music root; empty = same mount as the app


class PlexConfigStore:
    def __init__(self, path: Path | str, *, env_defaults: PlexConfig) -> None:
        self._path = Path(path)
        self._config = self._load(env_defaults)

    def _load(self, env_defaults: PlexConfig) -> PlexConfig:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return env_defaults
        try:
            return PlexConfig.model_validate_json(raw)
        except ValueError:
            return env_defaults

    def get(self) -> PlexConfig:
        return self._config

    def is_configured(self) -> bool:
        return bool(self._config.base_url and self._config.token)

    def update(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        library_path: str | None = None,
    ) -> PlexConfig:
        current = self._config
        self._config = PlexConfig(
            base_url=current.base_url if base_url is None else base_url,
            token=current.token if token is None else token,
            library_path=current.library_path if library_path is None else library_path,
        )
        write_atomic_text(
            self._path,
            self._config.model_dump_json(indent=2),
            mode=_TOKEN_FILE_MODE,
        )
        return self._config
