"""Shared pytest fixtures for the backend test suite.

The two beets fixtures here are load-bearing for every test that touches the
adapter:

* ``_clear_beets_globals`` (autouse) resets the global ``beets.config`` confuse
  singleton and the global plugin registry between tests. beets exposes these
  as module globals, so without this any test that calls ``setup_beets()`` (or
  any code path that calls ``plugins.load_plugins()``) leaks state into the
  next test — and the leak is silent because confuse's ``LazyConfig.clear()``
  doesn't reset ``_materialized``, so a subsequent force-resolve short-circuits
  on stale state and the user file is silently ignored.

* ``beets_library`` writes a minimal ``config.yaml`` into a tmp BEETSDIR and
  calls ``setup_beets()`` against it, yielding a fully-formed
  :class:`LibraryHandle`. This is the canonical way to get a hermetic beets
  library in tests now that the old ``MUSICDROP_BEETS_LIBRARY_*`` settings
  are gone.
"""

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from app.beets.library import LibraryHandle
from app.beets.setup import setup_beets

if TYPE_CHECKING:
    from beets.library import Library


def make_test_handle(lib: "Library", beets_dir: Path) -> LibraryHandle:
    """Wrap a raw ``Library`` in a ``LibraryHandle`` for dep-override fixtures.

    Tests that build their own hermetic ``Library`` directly (rather than going
    through ``setup_beets``) still need to satisfy the ``LibraryHandle`` shape
    that the API endpoints now expect. This helper fills the snapshot fields
    with placeholder values that aren't observable from the endpoints under
    test (they only ever touch ``handle.lib``).
    """
    cfg_path = beets_dir / "config.yaml"
    if not cfg_path.exists():
        # Touch a file so ``file_mtime_at_load`` has a concrete value, in case a
        # future test reaches into ``handle`` for snapshot fields.
        cfg_path.write_text("# test placeholder\n")
    return LibraryHandle(
        lib=lib,
        beets_dir=beets_dir.resolve(),
        config_path=cfg_path,
        loaded_at=datetime.now(UTC),
        file_mtime_at_load=cfg_path.stat().st_mtime,
    )


@pytest.fixture
def anyio_backend() -> str:
    """Run anyio-marked async tests on asyncio only (no trio dependency)."""
    return "asyncio"


@pytest.fixture(autouse=True)
def reset_import_registry() -> Iterator[None]:
    """Reset the global single-slot import registry around every test.

    The registry is module-global mutable state (one active job); without this a
    job started in one test would block ``start`` in the next with a 409.
    """
    from app.import_jobs.registry import reset_registry

    reset_registry()
    yield
    reset_registry()


@pytest.fixture(autouse=True)
def _clear_beets_globals() -> Iterator[None]:
    """Reset beets' global confuse + plugin singletons between every test.

    Why this is autouse for ALL tests (not just adapter tests): beets caches
    the resolved ``beets.config`` and loaded plugins in module globals. Any
    test that calls ``setup_beets()`` mutates those, and a later unrelated
    test that happens to read ``beets.config`` (e.g. via the import session)
    would see the leaked state. Centralising here means individual test files
    no longer have to remember to repeat this fixture.

    Implementation note: confuse's ``LazyConfig.clear()`` (core.py:749) does
    NOT reset ``_materialized``; without flipping it back to False the next
    ``setup_beets()`` force-resolve short-circuits at ``LazyConfig.resolve()``'s
    guard (core.py:728) and the user's ``config.yaml`` is silently ignored.
    """
    import beets
    from beets import metadata_plugins, plugins

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
    # beets caches ``find_metadata_source_plugins()`` with @cache; a test that
    # queried it BEFORE plugins were loaded (any import_session test that
    # transitively walks the matcher) pins an empty list in that cache, and a
    # later setup_beets()+load_plugins() can't see its own work. Clear it so
    # each test rediscovers the freshly-loaded plugin instances.
    metadata_plugins.find_metadata_source_plugins.cache_clear()
    metadata_plugins.get_metadata_source.cache_clear()
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture
def beets_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LibraryHandle]:
    """Hermetic beets library opened under a tmp BEETSDIR.

    Writes a minimal ``config.yaml`` (musicbrainz plugin only — keep startup
    cheap), points ``settings.beets_dir`` at the tmp dir, then runs
    ``setup_beets()`` against it. Tests that want a different ``config.yaml``
    should write their own BEFORE calling ``setup_beets`` directly rather than
    relying on this fixture.
    """
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"directory: {music_dir}\n"
        "library: library.db\n"
        "plugins:\n  - musicbrainz\n"
        "import:\n  autotag: yes\n  copy: yes\n"
    )
    monkeypatch.setattr("app.config.settings.beets_dir", str(tmp_path))
    handle = setup_beets(str(tmp_path))
    try:
        yield handle
    finally:
        handle.lib._close()
