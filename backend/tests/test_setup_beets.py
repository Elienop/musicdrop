import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from app.beets.setup import setup_beets


@pytest.fixture(autouse=True)
def _clear_beets_globals() -> Iterator[None]:
    """Each test gets a clean beets.config singleton + plugin registry."""
    import beets
    from beets import plugins

    # Snapshot env keys we may mutate
    saved_env = {
        k: os.environ.get(k)
        for k in (
            "BEETSDIR",
            "MUSICDROP_BEETS_LIBRARY_PATH",
            "MUSICDROP_BEETS_LIBRARY_DIRECTORY",
        )
    }
    yield
    # Reset confuse + plugins so the next test re-reads its own user file.
    # confuse's LazyConfig.clear() (core.py:749) does NOT reset _materialized;
    # without flipping it back to False, the next setup_beets() force-resolve
    # short-circuits at LazyConfig.resolve()'s guard (core.py:728) and the
    # user's config.yaml is silently ignored. read(user=False, defaults=True)
    # is the wrong reset — it materializes the defaults-only state.
    # beets 2.11 plugins module exposes only _instances (no _classes);
    # load_plugins() guards on `if not _instances`, so clearing it is enough.
    beets.config.clear()
    beets.config._materialized = False  # force LazyConfig.resolve() to re-read sources
    plugins._instances.clear()
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


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
        # beets' Library exposes _close (single underscore) not close;
        # see close_library() in app/beets/library.py.
        handle.lib._close()  # type: ignore[no-untyped-call]  # beets internals untyped


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
        handle.lib._close()  # type: ignore[no-untyped-call]  # beets internals untyped
