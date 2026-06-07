from pathlib import Path

from app.plex.config import PlexConfig, PlexConfigStore


def test_env_default_when_file_absent(tmp_path: Path) -> None:
    env = PlexConfig(base_url="http://seed:32400", token="seedtok", library_path="")
    store = PlexConfigStore(tmp_path / "plex.json", env_defaults=env)
    assert store.get().base_url == "http://seed:32400"
    assert store.is_configured() is True


def test_update_persists_and_wins_over_env(tmp_path: Path) -> None:
    path = tmp_path / "plex.json"
    env = PlexConfig(base_url="http://seed:32400", token="seedtok")
    PlexConfigStore(path, env_defaults=env).update(
        base_url="http://saved:32400", token="savedtok", library_path="/data/music"
    )
    # A fresh store loads the saved file, not the env seed.
    reloaded = PlexConfigStore(path, env_defaults=env)
    c = reloaded.get()
    assert c.base_url == "http://saved:32400"
    assert c.token == "savedtok"
    assert c.library_path == "/data/music"


def test_partial_update_keeps_other_fields(tmp_path: Path) -> None:
    path = tmp_path / "plex.json"
    store = PlexConfigStore(path, env_defaults=PlexConfig())
    store.update(base_url="http://a:32400", token="t1", library_path="/m")
    store.update(library_path="/m2")  # only library_path
    c = store.get()
    assert c.base_url == "http://a:32400"
    assert c.token == "t1"
    assert c.library_path == "/m2"


def test_not_configured_without_url_or_token(tmp_path: Path) -> None:
    store = PlexConfigStore(tmp_path / "plex.json", env_defaults=PlexConfig())
    assert store.is_configured() is False
    store.update(base_url="http://a:32400")
    assert store.is_configured() is False  # token still missing


def test_token_file_is_owner_only(tmp_path: Path) -> None:
    # The persisted config holds the Plex admin token; it must be owner-only
    # (0o600), unlike the world-readable playlist/.m3u8 files.
    path = tmp_path / "plex.json"
    store = PlexConfigStore(path, env_defaults=PlexConfig())
    store.update(base_url="http://a:32400", token="secret")
    assert (path.stat().st_mode & 0o777) == 0o600
