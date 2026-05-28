import os
from datetime import datetime
from pathlib import Path

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

    LazyConfig.clear() does NOT reset ``_materialized`` (confuse core.py:749),
    so without an explicit ``_materialized = False`` the previous test leaves
    confuse in a "files already read" state and setup_beets()'s force-resolve
    in this test silently skips reading the user file. We'd then load only
    beets' bundled defaults — and any Task 2/4 assertion on the user's
    ``plugins:`` / ``import:`` keys would falsely pass against the defaults.

    This test runs AFTER test_setup_copies_starter_when_missing (which
    materialized confuse with the starter), writes a NON-starter config, and
    asserts the new values are visible — proving the fixture re-reads sources.
    """
    import beets

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
