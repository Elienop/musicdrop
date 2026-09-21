import logging
import os
from datetime import datetime
from pathlib import Path

import pytest

from app.beets.library import close_library
from app.beets.setup import setup_beets

# The ``_clear_beets_globals`` autouse fixture is now defined in
# tests/conftest.py and applies to every test in the suite, so each test here
# still starts with a clean ``beets.config`` singleton and a clean plugin
# registry without us having to repeat the fixture body.


def test_setup_copies_starter_when_missing(tmp_path: Path) -> None:
    import beets

    handle = setup_beets(str(tmp_path))
    try:
        cfg = tmp_path / "config.yaml"
        assert cfg.exists()
        starter = Path(__file__).parent.parent / "app" / "beets" / "config.starter.yaml"
        assert cfg.read_text() == starter.read_text()
        assert isinstance(handle.loaded_at, datetime)
        assert handle.config_path == cfg
        assert handle.beets_dir == tmp_path.resolve()
        # Prove the user file (starter copy) was actually read into the confuse
        # singleton — a force-resolve regression (e.g. BEETSDIR set after
        # resolve) would still pass the field/existence asserts above.
        assert beets.config["plugins"].as_str_seq() == ["musicbrainz", "deezer"]
        assert beets.config["import"]["copy"].get(bool) is True
        assert beets.config["import"]["autotag"].get(bool) is True
    finally:
        close_library(handle.lib)


def test_setup_honors_library_and_directory_from_file(tmp_path: Path) -> None:
    music_dir = tmp_path / "my_music"
    music_dir.mkdir()
    lib_file = tmp_path / "my_library.db"

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"directory: {music_dir}\n"
        f"library: {lib_file}\n"
        "plugins:\n  - musicbrainz\n"
        "import:\n  autotag: yes\n"
    )
    handle = setup_beets(str(tmp_path))
    try:
        # Library.path and Library.directory are bytes in beets.
        assert Path(os.fsdecode(handle.lib.path)) == lib_file
        assert Path(os.fsdecode(handle.lib.directory)) == music_dir
    finally:
        close_library(handle.lib)


def test_setup_loads_user_plugins(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "directory: ../music\nlibrary: library.db\n"
        "plugins:\n  - musicbrainz\n  - deezer\n"
        "import:\n  autotag: yes\n"
    )
    handle = setup_beets(str(tmp_path))
    try:
        from beets import metadata_plugins

        names = {p.name for p in metadata_plugins.find_metadata_source_plugins()}
        assert "musicbrainz" in names
        assert "deezer" in names
    finally:
        close_library(handle.lib)


def test_setup_leaves_existing_config_alone(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    custom = (
        "directory: ../music\nlibrary: library.db\n"
        "plugins:\n  - musicbrainz\n"
        "import:\n  autotag: yes\n  copy: no\n"
    )
    cfg.write_text(custom)
    handle = setup_beets(str(tmp_path))
    try:
        assert cfg.read_text() == custom
    finally:
        close_library(handle.lib)


def test_fixture_resets_confuse_between_tests(tmp_path: Path) -> None:
    """Regression for the _clear_beets_globals fixture.

    The first two asserts are the fixture's own work: an earlier test's
    sources and ``config.set()`` overrides are gone and confuse is re-armed
    (``LazyConfig.clear()`` alone leaves ``_materialized`` set, confuse
    core.py:749). Without that, a test reading ``beets.config`` without calling
    ``setup_beets`` would see the earlier test's config. ``setup_beets`` itself
    re-reads the user file either way (``setup._read_config``).

    This test runs AFTER test_setup_copies_starter_when_missing (which
    materialized confuse with the starter), writes a NON-starter config, and
    asserts the new values are visible.
    """
    import beets

    assert beets.config._materialized is False
    assert beets.config.sources == []

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "directory: ../music\n"
        "library: library.db\n"
        "plugins:\n  - musicbrainz\n"
        "import:\n  autotag: no\n  copy: no\n"
    )
    handle = setup_beets(str(tmp_path))
    try:
        assert beets.config["plugins"].as_str_seq() == ["musicbrainz"]
        assert beets.config["import"]["autotag"].get(bool) is False
        assert beets.config["import"]["copy"].get(bool) is False
    finally:
        close_library(handle.lib)


def test_starter_directory_default_dev(tmp_path: Path) -> None:
    handle = setup_beets(str(tmp_path))
    try:
        text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "directory: ../music" in text
        assert "directory: /music\n" not in text
    finally:
        close_library(handle.lib)


def test_starter_directory_default_container(tmp_path: Path) -> None:
    handle = setup_beets(str(tmp_path), container_music_default=True)
    try:
        text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "directory: /music" in text
        assert "../music" not in text
    finally:
        close_library(handle.lib)


def test_setup_fails_fast_on_invalid_yaml(tmp_path: Path) -> None:
    from confuse.exceptions import ConfigReadError

    cfg = tmp_path / "config.yaml"
    cfg.write_text("directory: ../music\n  this: is: not [valid YAML\n")
    with pytest.raises(ConfigReadError):  # confuse raises during first-resolve
        setup_beets(str(tmp_path))


def test_setup_logs_deprecation_for_old_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("MUSICDROP_BEETS_LIBRARY_PATH", "/leftover/path")
    monkeypatch.setenv("MUSICDROP_BEETS_LIBRARY_DIRECTORY", "/leftover/dir")
    with caplog.at_level(logging.WARNING, logger="app.beets.setup"):
        handle = setup_beets(str(tmp_path))
    try:
        msgs = [r.message for r in caplog.records]
        assert any("MUSICDROP_BEETS_LIBRARY_PATH" in m for m in msgs)
        assert any("MUSICDROP_BEETS_LIBRARY_DIRECTORY" in m for m in msgs)
    finally:
        close_library(handle.lib)


def test_starter_replace_rules_map_typographic_to_ascii() -> None:
    """The starter's ``replace:`` block sanitizes MusicBrainz typographic Unicode.

    The literal characters are visually identical to their ASCII twins (that is
    the entire bug class — MB spells "blink-182" with a U+2010 HYPHEN, which
    minted a folder twin next to the ASCII one), so both this test and the config
    spell them with ``\\uXXXX`` escapes. Assert (a) the YAML loads a ``replace``
    mapping, (b) every pattern compiles under Python ``re``, and (c) applying the
    compiled rules collapses the twins back to ASCII.
    """
    import re

    import yaml

    starter = Path(__file__).parent.parent / "app" / "beets" / "config.starter.yaml"
    data = yaml.safe_load(starter.read_text(encoding="utf-8"))

    # (a) the YAML round-trips into a non-empty replace mapping.
    replace = data["replace"]
    assert isinstance(replace, dict)
    assert replace

    # (b) every pattern compiles under Python re (single-quoted YAML keeps the
    # \uXXXX escapes as literal text, which re then decodes).
    compiled = [(re.compile(pattern), replacement) for pattern, replacement in replace.items()]

    def sanitize(text: str) -> str:
        for pattern, replacement in compiled:
            text = pattern.sub(replacement, text)
        return text

    # (c) the blink-182 (U+2010) twin class + MB's curly apostrophe collapse to ASCII.
    # Spelled with escapes because the literals are indistinguishable on screen.
    assert sanitize("blink\u2010182") == "blink-182"  # U+2010 HYPHEN
    assert sanitize("Don\u2019t Stop") == "Don't Stop"  # U+2019 RIGHT SINGLE QUOTE
