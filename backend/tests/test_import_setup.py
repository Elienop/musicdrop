import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.beets.setup import setup_beets


@pytest.fixture(autouse=True)
def _clear_beets_globals() -> Iterator[None]:
    """Each test gets a clean beets.config singleton + plugin registry.

    Mirrors the fixture in test_setup_beets.py; both files share the same global
    beets singletons, so this resets confuse + plugins so neighbouring tests in
    other files don't see this file's load_plugins() side-effects (and so the
    next call to setup_beets() actually re-reads its user file via the
    ``_materialized = False`` flip — see test_setup_beets.py for the rationale).
    """
    import beets
    from beets import plugins

    saved_env = {
        k: os.environ.get(k)
        for k in (
            "BEETSDIR",
            "MUSICDROP_BEETS_LIBRARY_PATH",
            "MUSICDROP_BEETS_LIBRARY_DIRECTORY",
        )
    }
    yield
    beets.config.clear()
    beets.config._materialized = False  # force LazyConfig.resolve() to re-read sources
    plugins._instances.clear()
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_default_starter_loads_musicbrainz_and_deezer(tmp_path: Path) -> None:
    # Regression for "every album skipped": beets 2.11 ships its metadata
    # sources (MusicBrainz, Deezer, ...) as plugins, so without
    # plugins.load_plugins() the matcher has no candidate source and skips
    # everything. setup_beets must load the configured plugins; the default
    # starter config.yaml ships both musicbrainz AND deezer (deezer is no-auth
    # and dramatically improves match rate on modern releases — see spec).
    handle = setup_beets(str(tmp_path))
    try:
        from beets import metadata_plugins

        names = {p.name for p in metadata_plugins.find_metadata_source_plugins()}
        assert "musicbrainz" in names
        assert "deezer" in names
    finally:
        # tests.test_import_setup is in the disallow_untyped_calls=false mypy
        # override, so calling beets' untyped _close() needs no type-ignore.
        handle.lib._close()
