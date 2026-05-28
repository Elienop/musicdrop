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
from fastapi.testclient import TestClient

from app.beets.library import LibraryHandle, close_library
from app.beets.setup import setup_beets

if TYPE_CHECKING:
    from beets.library import Library


def make_test_handle(lib: "Library", beets_dir: Path) -> LibraryHandle:
    """Snapshot fields are SENTINELS — use a real ``setup_beets()`` handle to assert on them.

    For endpoints that only touch ``handle.lib``: this wraps a raw ``Library``
    in a ``LibraryHandle`` so dependency-override fixtures (the per-file
    ``temp_library`` fixtures in test_albums / test_artists / test_search that
    build their own hermetic ``Library`` without going through ``setup_beets``)
    still satisfy the ``LibraryHandle`` shape that the API endpoints now
    expect.

    The snapshot fields use **sentinel values** rather than realistic-looking
    placeholders so a future Task 5/6 ``BeetsConfigSnapshot`` test can't
    silently assert against them and pass for the wrong reason:

    * ``config_path`` points at the literal ``Path("__placeholder__")`` — no
      file backs it, so any ``.stat()`` / ``.read_text()`` against it raises.
    * ``loaded_at`` is the Unix epoch.
    * ``file_mtime_at_load`` is ``0.0``.
    """
    return LibraryHandle(
        lib=lib,
        beets_dir=beets_dir.resolve(),
        config_path=Path("__placeholder__"),
        loaded_at=datetime(1970, 1, 1, tzinfo=UTC),
        file_mtime_at_load=0.0,
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
            "MUSICDROP_BEETS_DIR",
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
        close_library(handle.lib)


@pytest.fixture
def beets_library_config_path(beets_library: LibraryHandle) -> Path:
    """Path to the ``config.yaml`` backing the active :class:`LibraryHandle`.

    Test_config_api uses this to ``os.utime`` the file between two GETs and
    assert the endpoint surfaces the new mtime / sets ``restart_required``.
    Resolved off the handle (not ``tmp_path``) so the two stay in lockstep
    even if the fixture's layout changes.
    """
    return beets_library.config_path


@pytest.fixture
def client(beets_library: LibraryHandle) -> Iterator[TestClient]:
    """TestClient with ``app.state.beets_library`` wired to a real handle.

    Used by endpoints that read ``request.app.state.beets_library`` directly
    (no FastAPI dependency to override) — currently the Config view at
    ``GET /api/config``. The endpoint's snapshot builder calls
    ``handle.config_path.stat()``, so the placeholder handle from
    ``make_test_handle`` would explode; we point at the real ``beets_library``
    fixture instead.

    Construction order matters: TestClient is built WITHOUT a ``with`` block
    so the lifespan handler (which would also try to set ``app.state.beets_library``
    + open an httpx client) does not run.
    """
    from app.main import app

    prior = getattr(app.state, "beets_library", None)
    app.state.beets_library = beets_library
    try:
        yield TestClient(app)
    finally:
        if prior is None:
            del app.state.beets_library
        else:
            app.state.beets_library = prior
