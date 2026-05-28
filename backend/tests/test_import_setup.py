from pathlib import Path

from app.beets.setup import setup_beets

# The ``_clear_beets_globals`` autouse fixture is now defined in
# tests/conftest.py and applies to every test in the suite, so the global
# ``beets.config`` singleton and plugin registry are reset between tests
# without us having to repeat the fixture body here.


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
