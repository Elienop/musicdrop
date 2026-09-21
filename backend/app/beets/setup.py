"""Embedded beets startup — faithful mirror of beets' own _setup.

Reference: beets/ui/__init__.py:749-766 (_setup), :784-802 (_open_library), from
beets 2.13.1 in venv. 2.13 dropped the separate ``_configure`` helper; the config
resolve it used to drive is what ``BEETSDIR`` + the forced resolve below stand in
for.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import beets
from beets import metadata_plugins, plugins
from beets.library import Library
from beets.plugins import BeetsPlugin

from app.beets.library import LibraryHandle, close_library

logger = logging.getLogger(__name__)

#: Operator-facing records go to ``uvicorn.error``: under the Dockerfile CMD
#: uvicorn's LOGGING_CONFIG leaves app-namespace loggers at WARNING, so an app
#: INFO record never reaches ``docker logs`` (same trap as ``main._boot_log``).
operator_logger = logging.getLogger("uvicorn.error")


def setup_beets(beets_dir: str, *, container_music_default: bool = False) -> LibraryHandle:
    """Open a beets Library under ``beets_dir``, honoring its config.yaml.

    Do not reorder the body. Three constraints are load-bearing:

    - ``BEETSDIR`` must be set BEFORE confuse's first resolve. confuse reads
      the env inside ``Configuration.config_dir()``; setting it after the
      first access locks in the platform default and our user file is lost.
    - The file mtime snapshot must be captured BEFORE the first resolve.
      It's the baseline for the Config view's "restart required" check; any
      write during setup would race a freshness comparison taken later.
    - ``plugins.load_plugins()`` must run AFTER the first resolve. It reads
      ``config["plugins"].as_str_seq()`` at call time; running it earlier
      (as the previous implementation did) freezes the bundled defaults and
      the user's ``plugins:`` list is ignored.
    """
    beets_dir_path = Path(beets_dir).resolve()
    beets_dir_path.mkdir(parents=True, exist_ok=True)
    cfg_path = beets_dir_path / "config.yaml"

    if not cfg_path.exists():
        starter = Path(__file__).parent / "config.starter.yaml"
        text = starter.read_text(encoding="utf-8")
        if container_music_default:
            # In the Docker image the music share is mounted at /music; the
            # dev-relative ../music default would point inside the volume.
            text = text.replace("directory: ../music", "directory: /music", 1)
        cfg_path.write_text(text, encoding="utf-8")
        operator_logger.info("Copied starter config to %s", cfg_path)

    os.environ["BEETSDIR"] = str(beets_dir_path)

    for old in ("MUSICDROP_BEETS_LIBRARY_PATH", "MUSICDROP_BEETS_LIBRARY_DIRECTORY"):
        if os.environ.get(old):
            logger.warning(
                "%s is no longer honored. Move the value into %s "
                "(under `library:` or `directory:`), then remove this env var.",
                old,
                cfg_path,
            )

    file_mtime_at_load = cfg_path.stat().st_mtime

    _load_config()

    plugins.load_plugins()
    # A lookup that ran while ``load_plugins`` was still filling the list (e.g.
    # a request thread during Apply) cached a partial answer; drop it.
    _clear_metadata_source_caches()

    lib_path = beets.config["library"].as_filename()
    directory = beets.config["directory"].as_filename()

    # beets 2.12's Library reads path formats + replacements from the global
    # config itself (the constructor kwargs were removed); the config is fully
    # loaded by this point, so naming/placement matches `beet import`.
    lib = Library(lib_path, directory=directory)
    plugins.send("library_opened", lib=lib)

    return LibraryHandle(
        lib=lib,
        beets_dir=beets_dir_path,
        config_path=cfg_path,
        loaded_at=datetime.now(UTC),
        file_mtime_at_load=file_mtime_at_load,
    )


def _load_config() -> None:
    """Read ``config.yaml`` + beets' defaults into ``beets.config``.

    First load: confuse's own lazy resolve. Reload (Apply): the config is still
    materialized from the previous load, so the new source list is read into a
    separate config object and installed with ONE assignment. A reader on
    another thread then resolves either the old list or the new one; clearing
    ``beets.config`` and re-reading it in place let a concurrent reader see (or
    trigger) a half-read list, which Apply then failed on (``pluginpath not
    found``, measured 17 of 200 Applies against a polling ``GET /api/config``).
    """
    config = beets.config
    if not config._materialized:
        config["dummy"].exists()  # force confuse's lazy resolve
        return
    fresh = type(config)(config.appname, config.modname)
    fresh["dummy"].exists()  # reads the files, raising here on a bad config.yaml
    # Same shape ``clear()`` + a lazy read left: file sources only, no
    # redactions. Plugin defaults and redactions come back with load_plugins.
    config.sources = fresh.sources
    config.redactions = fresh.redactions


def _clear_metadata_source_caches() -> None:
    """Clear the three ``functools.cache`` wrappers in ``beets.metadata_plugins``."""
    metadata_plugins.find_metadata_source_plugins.cache_clear()
    metadata_plugins.get_metadata_source.cache_clear()
    metadata_plugins.get_penalty.cache_clear()


def reset_beets_globals(handle: LibraryHandle | None = None, *, keep_config: bool = False) -> None:
    """Tear down all beets/confuse/plugin process-global state.

    Mirrors beets' own ``unload_plugins`` (beets/test/helper.py:509-515) and
    extends it with the confuse + metadata-source cache clears that
    ``setup_beets`` mutates. Calling this leaves the process in a state where a
    fresh ``setup_beets()`` re-reads the user's ``config.yaml`` and reloads
    plugins from scratch — used by the Apply endpoint to re-arm beets after
    rewriting ``config.yaml``, and delegated to (with ``handle=None``) by the
    conftest autouse fixture so tests and production share a SINGLE teardown
    body. The autouse passes no handle because it has no reachable
    ``LibraryHandle`` (every test owns its own); production always passes the
    live handle so the library's SQLite connection is closed first. Apply also
    passes ``keep_config=True``: ``beets.config`` stays readable until
    ``setup_beets`` replaces its sources (:func:`_load_config`).

    THIS IS A BEETS-2.13-PINNED COMPATIBILITY SHIM. Beets 3.x has open TODOs
    around a real plugin manager (see beets/plugins.py FIXME, PR #5887); the
    private surface this touches (``LazyConfig._materialized`` — confuse
    core.py:749 leaves the flag set after ``clear()``; ``plugins._instances``,
    ``BeetsPlugin._raw_listeners``, and the three ``functools.cache`` wrappers
    in ``beets.metadata_plugins``) is the only way to fully reset state on
    2.13. T9 pins ``beets==2.13.*`` in ``pyproject.toml`` so an upstream
    rename can't silently no-op this teardown — it would surface as an
    ``AttributeError`` instead.
    """
    # SQLite-only suppress. The narrow scope is deliberate: a closed-twice
    # library raises ``sqlite3.ProgrammingError`` ("Cannot operate on a closed
    # database"), which is the only expected race here. Anything else — an
    # ``AttributeError`` from a beets-3.x API drift, an ``OSError`` from a
    # torn-down FD — must propagate so the regression shows up in test logs,
    # not silently no-op (the whole point of the 2.13 pin rationale above).
    if handle is not None:
        with suppress(sqlite3.ProgrammingError):
            close_library(handle.lib)

    # confuse: truncate sources + re-arm LazyConfig so the next force-resolve
    # actually re-reads ``config.yaml``. ``LazyConfig.clear()`` alone does NOT
    # reset ``_materialized`` (confuse core.py:749 only resets
    # ``_lazy_prefix``/``_lazy_suffix``); without flipping the flag the next
    # ``setup_beets()`` short-circuits at the ``resolve()`` guard (confuse
    # core.py:728) and the user file is silently ignored.
    #
    # ``keep_config=True`` (Apply) skips this: the running process keeps serving
    # the old config until ``setup_beets`` installs the new one in one step
    # (:func:`_load_config`).
    if not keep_config:
        beets.config.clear()
        beets.config._materialized = False

    # beets plugin teardown — verbatim mirror of unload_plugins.
    plugins._instances.clear()
    BeetsPlugin.listeners.clear()
    BeetsPlugin._raw_listeners.clear()

    # All three ``@cache`` decorators in beets.metadata_plugins. Any of them
    # pinned across a reset would freeze the matcher to plugins from the
    # PREVIOUS load (or an empty list, if queried before plugins loaded),
    # silently shadowing the freshly-loaded plugin instances.
    _clear_metadata_source_caches()
