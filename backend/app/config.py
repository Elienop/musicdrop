from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MUSICDROP_", env_file=".env", extra="ignore")

    app_name: str = "MusicDrop"
    version: str = "0.1.0"
    # beets integration (wired in a later slice):
    beets_config_path: str | None = None
    beets_library_path: str | None = None


settings = Settings()
