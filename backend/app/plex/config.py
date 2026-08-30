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
    # ``base_url`` is deliberately unrestricted. What makes that safe is an
    # ENFORCEMENT MECHANISM, not an assumption about who is on the LAN: its
    # runtime writer is ``PUT /api/plex/settings`` (app/api/plex.py:73), under
    # ``/api/`` and absent from ``EXEMPT_PATHS``, so the session gate
    # (app/auth/gate.py) demands a valid cookie; the origin guard additionally
    # refuses it cross-origin. (``MUSICDROP_PLEX_*`` also seeds it via
    # ``env_defaults`` — the operator's own deployment input, not a request path.)
    # Beware the SHAPE of the justification this replaced: "only the owner can
    # set it via Settings" was FALSE for months — an Origin-less curl from
    # anything routable needed no credential — so do not reason from who is
    # presumed to be on the network. Name the mechanism that enforces it.
    #
    # The honest residual: whoever holds the password (or a stolen session) gets
    # a genuine SSRF primitive — the server will connect anywhere this names, and
    # hand over the token below on the way (see the SECRET-BEARING note). It
    # crosses no boundary between MusicDrop PRINCIPALS, because there is exactly
    # one account and that same cookie already buys the beets config editor
    # (``POST /api/config/save``), import, trash and reorganize. It does NOT
    # follow that the capability is contained: this one reaches OFF-BOX, turning
    # a write-only secret field (the API returns only ``has_token``) into
    # something an attacker-chosen host can read. Accepted because the writer is
    # gated — re-open the moment a second, less-privileged account exists.
    #
    # DO NOT "harden" this with ``assert_public_url`` (app/artwork/download.py:38).
    # Measured: it rejects ``http://192.168.1.50:32400``, ``http://plex:32400``
    # (this repo's own test value) and ``http://localhost:32400`` — i.e. every
    # real Plex deployment, because it refuses loopback/link-local/private hosts.
    # The asymmetry against the pasted-image path is deliberate and is explained
    # from the other side in that function's docstring.
    #
    # Treat the value as SECRET-BEARING, not merely secret-adjacent: it decides
    # who RECEIVES the Plex admin token, and it hands it over on the primary
    # path. ``test_connection`` -> ``client.connect`` builds a ``PlexServer``,
    # whose every request carries ``X-Plex-Token`` as a HEADER — so a single
    # ``POST /api/plex/test`` against a hostile ``base_url`` delivers the token
    # to that host directly. Measured against a local echo server: it received
    # ``X-Plex-Token``, plus ``X-Plex-Platform-Version`` (the HOST KERNEL
    # version) and ``X-Plex-Device-Name`` (the hostname) — fingerprinting the
    # box for free. Full account control, one request, no log-reading required.
    # (``playlists_pull.py:129`` separately puts the token in a QUERY STRING via
    # ``includeToken=True``, which leaks it into the destination's access log.
    # That is a real second exposure and is filed under Open bugs, but it is the
    # smaller one — do not let it distract from the header path above. If that
    # entry ships, delete this parenthesis with it; the header path stands
    # regardless, so do not delete the paragraph above along with it.)
    base_url: str = ""
    token: str = ""
    library_path: str = ""  # Plex-visible music root; empty = same mount as the app
    library_section: str = ""  # section TITLE; empty = the SOLE artist section (several -> refuse)


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
        library_section: str | None = None,
    ) -> PlexConfig:
        current = self._config
        self._config = PlexConfig(
            base_url=current.base_url if base_url is None else base_url,
            token=current.token if token is None else token,
            library_path=current.library_path if library_path is None else library_path,
            library_section=(
                current.library_section if library_section is None else library_section
            ),
        )
        write_atomic_text(
            self._path,
            self._config.model_dump_json(indent=2),
            mode=_TOKEN_FILE_MODE,
        )
        return self._config
