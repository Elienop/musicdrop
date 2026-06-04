from pydantic_settings import BaseSettings, SettingsConfigDict


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


settings = Settings()
