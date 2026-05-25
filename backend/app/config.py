from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MUSICDROP_", env_file=".env", extra="ignore")

    app_name: str = "MusicDrop"
    version: str = "0.1.0"
    # beets integration:
    beets_config_path: str | None = None
    beets_library_path: str | None = None
    beets_library_directory: str | None = None

    # Artist images (app/artwork/) — opt-in, conservative defaults.
    artist_images_enabled: bool = False
    artist_image_source: str = "deezer"
    artist_image_cache_dir: str = "data/cache/artist-images"
    # Deezer allows ~50 req / 5s; stay well under it.
    artist_image_rate_per_sec: float = 5.0
    artist_image_max_concurrency: int = 2
    # How many Deezer search hits to consider before verifying names.
    artist_image_search_limit: int = 5
    # Negative cache: confirmed/transient no-image is honored for this long
    # (seconds) before we re-try. Default ~7 days.
    artist_image_negative_ttl_seconds: int = 7 * 24 * 60 * 60


settings = Settings()
