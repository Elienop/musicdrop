"""Regression tests for ``reset_beets_globals``.

Pins the 7-clear teardown contract for beets' process-global state. The Apply
endpoint (Task 8) calls this helper between writing config.yaml and re-running
``setup_beets``; the conftest autouse fixture delegates to the same helper so
tests and production stay in lockstep via a single source of truth. Anything
less than all 7 clears here is a silent leak — see beets' own
``unload_plugins`` (beets/test/helper.py:460-466) for the matched-pair
invariants this regression locks down.
"""

import sqlite3
from pathlib import Path

import beets
import pytest
from beets import metadata_plugins, plugins
from beets.plugins import BeetsPlugin

from app.beets.library import close_library
from app.beets.setup import reset_beets_globals, setup_beets


def test_reset_leaves_no_residual_state(tmp_path: Path) -> None:
    handle = setup_beets(str(tmp_path))
    reset_beets_globals(handle)

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
        names = {p.name for p in metadata_plugins.find_metadata_source_plugins()}
        assert "musicbrainz" in names
        assert "deezer" in names
    finally:
        close_library(new_handle.lib)


def test_reset_closes_the_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The handle's library must be closed exactly once during reset.

    Spies on the ``close_library`` symbol imported into ``app.beets.setup`` so
    a future refactor that drops the close call (or double-calls it) is caught
    here, not in the field where it leaks a sqlite handle per Apply.
    """
    close_calls: list[object] = []

    def spy(lib: object) -> None:
        close_calls.append(lib)
        # Defer to the real implementation so the SQLite handle is actually
        # closed and tmp_path teardown doesn't trip a busy-file error.
        close_library(lib)  # type: ignore[arg-type]

    monkeypatch.setattr("app.beets.setup.close_library", spy)

    handle = setup_beets(str(tmp_path))
    reset_beets_globals(handle)

    assert len(close_calls) == 1
    assert close_calls[0] is handle.lib


def test_reset_propagates_unexpected_close_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Suppress is sqlite3-scoped, not ``Exception``-wide.

    A future beets API drift that makes ``Library._close`` raise
    ``AttributeError`` (the failure mode the 2.11 pin exists to surface) must
    propagate so the regression shows up in test logs, not silently no-op.
    """

    def boom(_lib: object) -> None:
        raise AttributeError("simulated beets-3.x API drift")

    monkeypatch.setattr("app.beets.setup.close_library", boom)

    handle = setup_beets(str(tmp_path))
    try:
        with pytest.raises(AttributeError, match=r"simulated beets-3\.x API drift"):
            reset_beets_globals(handle)
    finally:
        # The real close didn't run; tear the lib down so subsequent tests
        # don't inherit an open SQLite handle on the tmp_path file.
        close_library(handle.lib)


def test_reset_suppresses_sqlite_programming_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An already-closed library raises ``sqlite3.ProgrammingError``; suppress.

    Mirrors the race the narrow suppress is there for: a caller that closed
    the lib itself before Apply rolls forward shouldn't fail the reset.
    """

    def already_closed(_lib: object) -> None:
        raise sqlite3.ProgrammingError("Cannot operate on a closed database.")

    monkeypatch.setattr("app.beets.setup.close_library", already_closed)

    handle = setup_beets(str(tmp_path))
    try:
        # No raise — and the rest of the clears still ran.
        reset_beets_globals(handle)
        assert beets.config._materialized is False
        assert plugins._instances == []
    finally:
        close_library(handle.lib)


def test_reset_accepts_none_handle() -> None:
    """The autouse conftest fixture calls ``reset_beets_globals()`` with no handle.

    Pins the contract that ``handle=None`` is supported and still performs the
    confuse/plugin/cache clears (single source of truth between conftest and
    the Apply endpoint).
    """
    # Materialize confuse + seed one cache entry so the clears have work to do.
    beets.config["dummy"].exists()
    metadata_plugins.find_metadata_source_plugins()

    reset_beets_globals(None)

    assert beets.config._materialized is False
    assert plugins._instances == []
    assert BeetsPlugin.listeners == {}
    assert BeetsPlugin._raw_listeners == {}
    assert metadata_plugins.find_metadata_source_plugins.cache_info().currsize == 0
    assert metadata_plugins.get_metadata_source.cache_info().currsize == 0
    assert metadata_plugins.get_penalty.cache_info().currsize == 0
