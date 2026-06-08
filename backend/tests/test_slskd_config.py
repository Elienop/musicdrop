from pathlib import Path

from app.slskd.config import SlskdConfig, SlskdConfigStore


def test_env_default_when_file_absent(tmp_path: Path) -> None:
    env = SlskdConfig(base_url="http://seed:5030", token="seedtok")
    store = SlskdConfigStore(tmp_path / "slskd.json", env_defaults=env)
    assert store.get().base_url == "http://seed:5030"
    assert store.is_configured() is True


def test_update_persists_and_wins_over_env(tmp_path: Path) -> None:
    path = tmp_path / "slskd.json"
    env = SlskdConfig(base_url="http://seed:5030", token="seedtok")
    SlskdConfigStore(path, env_defaults=env).update(
        base_url="http://saved:5030",
        token="savedtok",
        downloads_prefix="/downloads",
        webhook_secret="hook",
        auto_import=True,
    )
    # A fresh store loads the saved file, not the env seed.
    reloaded = SlskdConfigStore(path, env_defaults=env)
    c = reloaded.get()
    assert c.base_url == "http://saved:5030"
    assert c.token == "savedtok"
    assert c.downloads_prefix == "/downloads"
    assert c.webhook_secret == "hook"
    assert c.auto_import is True


def test_partial_update_keeps_other_fields(tmp_path: Path) -> None:
    path = tmp_path / "slskd.json"
    store = SlskdConfigStore(path, env_defaults=SlskdConfig())
    store.update(
        base_url="http://a:5030",
        token="t1",
        downloads_prefix="/d",
        webhook_secret="s",
        auto_import=True,
    )
    store.update(downloads_prefix="/d2")  # only downloads_prefix
    c = store.get()
    assert c.base_url == "http://a:5030"
    assert c.token == "t1"
    assert c.downloads_prefix == "/d2"
    assert c.webhook_secret == "s"
    assert c.auto_import is True  # None sentinel kept the prior True


def test_auto_import_none_sentinel_preserves_current(tmp_path: Path) -> None:
    # auto_import's keep-current sentinel is None, NOT False — an update that
    # omits it must not silently flip a True back to False.
    store = SlskdConfigStore(tmp_path / "slskd.json", env_defaults=SlskdConfig())
    store.update(auto_import=True)
    assert store.get().auto_import is True
    store.update(base_url="http://a:5030")  # auto_import defaults None -> kept
    assert store.get().auto_import is True
    store.update(auto_import=False)  # an explicit False does flip it
    assert store.get().auto_import is False


def test_not_configured_without_url_or_token(tmp_path: Path) -> None:
    store = SlskdConfigStore(tmp_path / "slskd.json", env_defaults=SlskdConfig())
    assert store.is_configured() is False
    store.update(base_url="http://a:5030")
    assert store.is_configured() is False  # token still missing


def test_token_file_is_owner_only(tmp_path: Path) -> None:
    # The persisted config holds the slskd API key + webhook secret; it must be
    # owner-only (0o600), like the Plex admin token file.
    path = tmp_path / "slskd.json"
    store = SlskdConfigStore(path, env_defaults=SlskdConfig())
    store.update(base_url="http://a:5030", token="secret")
    assert (path.stat().st_mode & 0o777) == 0o600
