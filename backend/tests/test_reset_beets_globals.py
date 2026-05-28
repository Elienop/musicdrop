"""Regression tests for ``reset_beets_globals``.

Pins the 7-clear teardown contract for beets' process-global state. The Apply
endpoint (Task 8) calls this helper between writing config.yaml and re-running
``setup_beets``; the conftest autouse fixture mirrors the same clears so tests
and production stay in lockstep. Anything less than all 7 clears here is a
silent leak — see beets' own ``unload_plugins`` (beets/test/helper.py:460-466)
for the matched-pair invariants this regression locks down.
"""

from pathlib import Path

from app.beets.library import close_library
from app.beets.setup import reset_beets_globals, setup_beets


def test_reset_leaves_no_residual_state(tmp_path: Path) -> None:
    handle = setup_beets(str(tmp_path))
    reset_beets_globals(handle)

    import beets
    from beets import metadata_plugins, plugins
    from beets.plugins import BeetsPlugin

    assert plugins._instances == []
    assert BeetsPlugin.listeners == {}
    assert BeetsPlugin._raw_listeners == {}
    assert beets.config._materialized is False
    assert metadata_plugins.find_metadata_source_plugins.cache_info().currsize == 0
    assert metadata_plugins.get_metadata_source.cache_info().currsize == 0
    assert metadata_plugins.get_penalty.cache_info().currsize == 0


def test_setup_after_reset_loads_plugins_fresh(tmp_path: Path) -> None:
    handle = setup_beets(str(tmp_path))
    reset_beets_globals(handle)
    new_handle = setup_beets(str(tmp_path))
    try:
        from beets import metadata_plugins

        names = {p.name for p in metadata_plugins.find_metadata_source_plugins()}
        assert "musicbrainz" in names
        assert "deezer" in names
    finally:
        close_library(new_handle.lib)
