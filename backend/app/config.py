from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root is the parent of the backend/ package dir (this file is
# backend/app/config.py). Relative cache paths resolve under it so the
# artist-image cache lands in the gitignored repo-root data/, not backend/data/.
_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MUSICDROP_", env_file=".env", extra="ignore")

    app_name: str = "MusicDrop"
    version: str = "0.1.0"
    # beets integration:
    # data/beets is the user-owned BEETSDIR (config.yaml + library.db live here).
    # All library/directory/plugins are read FROM data/beets/config.yaml at startup
    # by app/beets/setup.py; there are no separate MUSICDROP_BEETS_LIBRARY_* knobs.
    beets_dir: str = "data/beets"

    # Library-wide lyrics backfill: a courtesy pause between LRCLib requests
    # (beets adds none; LRCLib is a free community API).
    # (env MUSICDROP_LYRICS_BACKFILL_DELAY_SECONDS)
    lyrics_backfill_delay_seconds: float = 0.2

    # Duplicate resolution moves the non-kept copies here (a reversible Trash).
    # Empty string = default to <beets_dir>/trash, computed at resolve time from
    # the live library handle (already an absolute path), which sidesteps the
    # cwd-relative gotcha. Set an absolute path to override. (env MUSICDROP_TRASH_DIR)
    trash_dir: str = ""

    # Artist images (app/artwork/) — opt-in, conservative defaults.
    artist_images_enabled: bool = False
    artist_image_cache_dir: str = "data/cache/artist-images"
    # Deezer allows ~50 req / 5s; stay well under it.
    artist_image_rate_per_sec: float = 5.0
    artist_image_max_concurrency: int = 2
    # How many Deezer search hits to consider before verifying names.
    artist_image_search_limit: int = 5
    # Negative cache: a CONFIRMED no-verified-match is honored this long
    # (seconds) before we re-try. Default ~7 days.
    artist_image_negative_ttl_seconds: int = 7 * 24 * 60 * 60
    # A TRANSIENT failure (429 / timeout / malformed body / bad download) is
    # honored only briefly so a Deezer blip doesn't bench a real artist.
    artist_image_transient_ttl_seconds: int = 600

    # Optional artist-image source credentials (env-only). A source joins the
    # resolution chain only when its credentials are present; with none set the
    # behaviour is exactly Deezer-only. (env MUSICDROP_ARTIST_IMAGE_*)
    artist_image_fanarttv_api_key: str = ""
    artist_image_fanarttv_client_key: str = ""
    artist_image_spotify_client_id: str = ""
    artist_image_spotify_client_secret: str = ""

    # Phase 2: write artist art into the library for Plex (off by default).
    # (env MUSICDROP_ARTIST_ART_WRITE_ENABLED)
    artist_art_write_enabled: bool = False

    # Playlists (app/playlists/) — MusicDrop owns playlist state as JSON files.
    # Empty string = default to <beets_dir>/playlists, computed at resolve time
    # (sidesteps the cwd-relative gotcha when beets_dir is absolute in tests).
    # Set an absolute path to override. (env MUSICDROP_PLAYLISTS_DIR)
    playlists_dir: str = ""

    # Where the Plex-readable .m3u8 exports are written. Empty string = default
    # to <music library>/.playlists (resolved from the live library directory).
    # (env MUSICDROP_PLAYLISTS_EXPORT_DIR)
    playlists_export_dir: str = ""

    # Plex sync (app/plex/). Empty plex_settings_dir = <beets_dir>/plex.
    # base URL + admin token + the music-library path AS PLEX SEES IT (for the
    # Docker mount difference). All env-seed the persisted JSON config.
    # (env MUSICDROP_PLEX_URL / MUSICDROP_PLEX_TOKEN / MUSICDROP_PLEX_LIBRARY_PATH)
    plex_settings_dir: str = ""
    plex_url: str = ""
    plex_token: str = ""
    plex_library_path: str = ""

    # Acquisition (app/acquisition/). Where completed downloads land before the
    # unattended import; empty = default to <beets_dir>/inbox, computed at resolve
    # time from the live library handle (sidesteps the cwd-relative gotcha, like
    # trash_dir). (env MUSICDROP_INBOX_DIR)
    inbox_dir: str = ""

    # slskd acquisition source (app/slskd/). The API key + webhook secret are
    # secrets: these env vars seed the INITIAL persisted JSON config (file > env);
    # the file is stored 0o600 and the secrets are never returned by the API.
    # ``slskd_auto_import`` is the OPERATIVE auto-import toggle — a completed slskd
    # download imports itself only when it is on. Empty slskd_settings_dir =
    # <beets_dir>/slskd. (env MUSICDROP_SLSKD_SETTINGS_DIR / _URL / _TOKEN /
    # _DOWNLOADS_PREFIX / _WEBHOOK_SECRET / _AUTO_IMPORT)
    slskd_settings_dir: str = ""
    slskd_url: str = ""
    slskd_token: str = ""
    slskd_downloads_prefix: str = ""
    slskd_webhook_secret: str = ""
    slskd_auto_import: bool = False


settings = Settings()


def resolve_artist_image_cache_dir() -> Path:
    """Resolve the artist-image cache dir, anchoring relatives to the repo root."""
    configured = Path(settings.artist_image_cache_dir)
    if configured.is_absolute():
        return configured
    return _REPO_ROOT / configured
