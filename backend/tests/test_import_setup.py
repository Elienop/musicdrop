from app.beets.setup import setup_beets


def test_setup_beets_registers_a_metadata_source() -> None:
    # Regression for "every album skipped": beets 2.11 ships its metadata
    # sources (MusicBrainz) as plugins, so without plugins.load_plugins() the
    # matcher has no candidate source and skips everything. setup_beets must
    # load the configured plugins so at least one metadata source is registered.
    from beets import metadata_plugins

    setup_beets(library_path=None, directory=None)  # loads plugins; no library
    assert metadata_plugins.find_metadata_source_plugins()
