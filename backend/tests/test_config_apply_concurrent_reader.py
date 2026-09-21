"""Apply's rebuild vs. a request thread reading beets config at the same moment.

Apply reloads ``beets.config`` while other request threads (``GET /api/config``,
the album pages) keep reading it without any lock. Each test pins the reader to
one instant inside the rebuild with events, not sleeps:

- a reader between the teardown and the reload (it used to trigger confuse's
  lazy read itself, and Apply then resolved the half-read list and 500'd with
  ``pluginpath not found``, leaving zero plugins loaded);
- a reader while Apply's own read is half done (it used to see the user file
  without beets' defaults: no ``timeout``);
- a metadata-source lookup while plugins are reloading (its ``functools.cache``
  kept the empty answer after Apply finished).

The two hooks below are test instrumentation on confuse/beets functions the
reload calls; ``test_confuse_surface_the_reload_relies_on`` pins the confuse
surface ``setup._read_config``/``_install_config`` rely on, so a confuse upgrade
that changes it fails here loudly. The last tests pin that an Apply keeps a
plugin's ``.redact`` flag in force; unloaded plugins are
``tests/test_declared_secrets.py``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import beets
import confuse
import confuse.core
import pytest
import yaml
from beets import metadata_plugins, plugins
from fastapi.testclient import TestClient

from app.beets.library import LibraryHandle
from app.beets.setup import BeetsConfigRead, open_beets, read_beets_config

#: Upper bound on every wait below. A deadline, not a timing assumption: each
#: event is set by the step it waits for, so a pass never sleeps.
_DEADLINE_S = 10.0

_READER = "apply-race-reader"


def _loaded_plugin_names() -> list[str]:
    return sorted(p.name for p in plugins.find_plugins())


def _read_like_a_request() -> object:
    # Two reads beets makes on routine paths: Library.__init__ (timeout) and an
    # unsorted lib.albums() (sort_album). ``timeout`` exists only in beets'
    # config_default.yaml, the LAST source a read loads.
    return (beets.config["timeout"].as_number(), beets.config["sort_album"].as_str_seq())


def test_reader_between_teardown_and_reload_cannot_fail_apply(
    client: TestClient,
    beets_library: LibraryHandle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariants 2 + 3: Apply succeeds and reloads every configured plugin."""
    import app.beets.config_editor as config_editor

    plugins_before = _loaded_plugin_names()
    assert plugins_before, "fixture must load at least one plugin"

    real_add_default = confuse.core.Configuration._add_default_source
    # Set once the reader is either parked inside a read it triggered, or done.
    reader_decided = threading.Event()
    release_reader = threading.Event()

    def add_default_source(self: confuse.Configuration) -> None:
        # A lazy read triggered by the READER parks here, after the user file
        # and before beets' defaults — the state a preempted reader leaves.
        if threading.current_thread().name == _READER:
            reader_decided.set()
            release_reader.wait(_DEADLINE_S)
        real_add_default(self)

    monkeypatch.setattr(confuse.core.Configuration, "_add_default_source", add_default_source)

    reader_out: list[object] = []

    def reader() -> None:
        try:
            reader_out.append(_read_like_a_request())
        except Exception as exc:  # the outcome is the assertion
            reader_out.append(exc)
        finally:
            reader_decided.set()

    def open_after_a_reader(read: BeetsConfigRead) -> LibraryHandle:
        t = threading.Thread(target=reader, name=_READER)
        t.start()
        try:
            assert reader_decided.wait(_DEADLINE_S)
            return open_beets(read)
        finally:
            release_reader.set()
            t.join(_DEADLINE_S)

    monkeypatch.setattr(config_editor, "open_beets", open_after_a_reader)

    r = client.post("/api/config/apply")

    assert r.status_code == 200, r.text
    assert _loaded_plugin_names() == plugins_before
    assert len(reader_out) == 1
    assert not isinstance(reader_out[0], Exception), reader_out[0]


def test_reader_during_applys_own_read_sees_a_complete_config(
    client: TestClient,
    beets_library: LibraryHandle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariant 1: one lookup sees the old source list or the new one, never a
    half-read list. Per lookup only: a multi-lookup walk such as ``flatten()``
    can span the swap (measured in review, ~7 failed polls in 1200 Applies)."""
    import app.beets.config_editor as config_editor

    real_add_default = confuse.core.Configuration._add_default_source
    reader_out: list[object] = []
    # Apply's layout gate reads beets' defaults into a throwaway Configuration
    # before the rebuild (store_layout.py); only Apply's own read counts here.
    in_setup = threading.Event()

    def flagged_read(beets_dir: str, **kw: Any) -> BeetsConfigRead:
        in_setup.set()
        return read_beets_config(beets_dir, **kw)

    monkeypatch.setattr(config_editor, "read_beets_config", flagged_read)

    def reader() -> None:
        try:
            reader_out.append(_read_like_a_request())
        except Exception as exc:  # the outcome is the assertion
            reader_out.append(exc)

    def add_default_source(self: confuse.Configuration) -> None:
        # Apply's read has loaded config.yaml and not yet beets' defaults: a
        # request thread reads NOW, and must finish before Apply continues.
        if in_setup.is_set() and not reader_out and threading.current_thread().name != _READER:
            t = threading.Thread(target=reader, name=_READER)
            t.start()
            t.join(_DEADLINE_S)
        real_add_default(self)

    monkeypatch.setattr(confuse.core.Configuration, "_add_default_source", add_default_source)

    r = client.post("/api/config/apply")

    assert r.status_code == 200, r.text
    assert len(reader_out) == 1, "Apply's reload never reached its defaults read"
    assert not isinstance(reader_out[0], Exception), reader_out[0]


def test_metadata_lookup_during_plugin_reload_is_not_cached_past_apply(
    client: TestClient,
    beets_library: LibraryHandle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariant 3, plugin window: what a lookup cached mid-reload is dropped
    when Apply finishes."""
    sources_before = [p.data_source for p in metadata_plugins.find_metadata_source_plugins()]
    assert sources_before, "fixture must load a metadata source plugin"

    real_load: Callable[[], None] = plugins.load_plugins
    fired: list[bool] = []

    def load_plugins_after_a_lookup() -> None:
        # The instant before plugins exist again: a request thread's lookup
        # (the album page's missing-tracks report) caches what it sees.
        fired.append(True)
        for source in sources_before:
            metadata_plugins.get_metadata_source(source)
        metadata_plugins.find_metadata_source_plugins()
        real_load()

    monkeypatch.setattr(plugins, "load_plugins", load_plugins_after_a_lookup)

    r = client.post("/api/config/apply")

    assert r.status_code == 200, r.text
    assert fired == [True], "Apply's reload did not reach the hooked load_plugins"
    for source in sources_before:
        assert metadata_plugins.get_metadata_source(source) is not None, source
    assert [
        p.data_source for p in metadata_plugins.find_metadata_source_plugins()
    ] == sources_before


def test_confuse_surface_the_reload_relies_on(beets_library: LibraryHandle) -> None:
    """Tripwire for the confuse behaviour ``setup._read_config`` and
    ``setup._install_config`` depend on.

    The reload reads ``_materialized`` (private), builds a second config from
    ``appname``/``modname``, and assigns ``sources``. One lookup is only atomic
    because a materialized LazyConfig resolves ``self.sources`` without
    re-reading files, and reads that list ONCE per ``resolve()``. If a confuse
    upgrade changes any of that, fix ``setup.py`` rather than this test.
    """
    config = beets.config
    assert isinstance(config, confuse.LazyConfig)
    assert config._materialized is True
    assert isinstance(config.sources, list)
    assert isinstance(config.redactions, set)

    fresh = type(config)(config.appname, config.modname)
    assert fresh._materialized is False
    fresh.read()
    assert fresh._materialized is True
    # The fresh read yields the same file sources the live config started with.
    assert [(s.filename, s.default) for s in fresh.sources] == [
        (s.filename, s.default) for s in config.sources if isinstance(s, confuse.YamlSource)
    ]

    def no_reread(*_a: object, **_k: object) -> None:
        raise AssertionError("a materialized LazyConfig re-read its files on resolve")

    fresh.read = no_reread  # instance-level spy
    assert fresh["timeout"].exists()

    # One resolve() takes the list once: a swap after it starts does not
    # change what it yields (confuse ``RootView.resolve``, a generator
    # expression over ``self.sources``).
    old_sources = list(fresh.sources)
    in_flight = fresh.resolve()
    fresh.sources = [confuse.ConfigSource({"timeout": 12345})]
    assert [source for _value, source in in_flight] == old_sources

    # Assigning the list is what readers see next, with no read in between.
    assert fresh["timeout"].as_number() == 12345


_SECRET = "PIN-USER-4242"
# Neither BEETS_DECLARED_SECRETS nor SECRET_KEY_PATTERN covers this key, so only a
# plugin's own ``.redact`` flag masks it: where a third-party plugin's secret is.
_SECTION, _KEY = "thirdparty", "user_ident"


def _load_flagged_secret(client: TestClient, handle: LibraryHandle, *, via_include: bool) -> None:
    """Apply a config holding ``thirdparty.user_ident``, then flag it ``.redact``
    the way a loaded plugin's ``__init__`` does."""
    cfg = handle.config_path
    head = [
        line
        for line in cfg.read_text(encoding="utf-8").splitlines()
        if line.startswith(("directory:", "library:"))
    ]
    secret = f"{_SECTION}:\n  {_KEY}: {_SECRET}"
    lines = [*head, "plugins:\n  - musicbrainz"]
    if via_include:
        (cfg.parent / "secrets.yaml").write_text(secret + "\n", encoding="utf-8")
        lines.append("include:\n  - secrets.yaml")
    else:
        lines.append(secret)
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert client.post("/api/config/apply").status_code == 200
    assert _served_user(client) == _SECRET  # the control: nothing else masks it
    beets.config[_SECTION][_KEY].redact = True
    assert _served_user(client) == "REDACTED"


def _served_user(client: TestClient) -> object:
    r = client.get("/api/config")
    assert r.status_code == 200, r.text
    return yaml.safe_load(r.json()["effective_yaml"])[_SECTION][_KEY]


def test_an_included_plugin_secret_is_masked_after_a_real_apply(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    _load_flagged_secret(client, beets_library, via_include=True)
    assert client.post("/api/config/apply").status_code == 200
    assert _served_user(client) == "REDACTED"


def test_a_plugin_secret_stays_masked_while_plugins_reload(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GET between the swap and the end of ``load_plugins`` still masks it."""
    _load_flagged_secret(client, beets_library, via_include=False)
    real_load: Callable[[], None] = plugins.load_plugins
    served: list[object] = []

    def load_plugins_after_a_get() -> None:
        served.append(_served_user(client))
        real_load()

    monkeypatch.setattr(plugins, "load_plugins", load_plugins_after_a_get)

    assert client.post("/api/config/apply").status_code == 200
    assert served == ["REDACTED"]
    assert _served_user(client) == "REDACTED"


def test_a_plugin_secret_stays_masked_after_load_plugins_fails(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state a failed reload leaves serves every later GET."""
    _load_flagged_secret(client, beets_library, via_include=False)

    def load_plugins_fails() -> None:
        raise RuntimeError("plugin load failed")

    monkeypatch.setattr(plugins, "load_plugins", load_plugins_fails)

    assert client.post("/api/config/apply").status_code == 500
    assert _served_user(client) == "REDACTED"
