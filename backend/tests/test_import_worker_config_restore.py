"""run_import_worker's beets-config force/restore contract (process-global).

The worker mutates beets' process-global confuse around every import. The
invariant: EVERY key it mutates is snapshotted before mutation and restored
in the same finally — a leaked key (e.g. ``import.duplicate_action: ask`` or
``threaded: false``) reaches the Settings "Effective config" panel, which
flattens the LIVE global, and shows values the user never saved.

The ``_RecordingSession`` stand-in mirrors the fake-session pattern of
``tests/test_import_session.py::test_run_import_worker_forces_single_threaded_and_runs``
(record the flags AT ``run()`` call time — the strongest assertion the
harness supports for "pinned before the pipeline starts") and never opens a
library, so no real ``~/.config/beets`` or real library is touched. The
autouse ``_clear_beets_globals`` conftest fixture resets beets' globals
around every test.
"""

from __future__ import annotations

import contextlib
from contextlib import AbstractContextManager
from typing import Any, ClassVar

from beets import config


class _BindOnlyLib:
    """The bare slice of ``Library`` that ``run_import_worker`` needs to bind:
    a nullcontext stands in (these tests assert on config flags, never rows)."""

    def music_dir_context(self) -> AbstractContextManager[None]:
        return contextlib.nullcontext()


class _RecordingSession:
    """The bare slice of WebImportSession that ``run_import_worker`` touches.

    Empty ``paths`` -> the in-library guard no-ops; ``_trash_dir=None`` ->
    the post-run trash pass returns early. ``run()`` records the config
    values the pipeline would see."""

    lib: ClassVar[Any] = _BindOnlyLib()
    paths: ClassVar[list[bytes]] = []
    _replace_album_ids: ClassVar[set[int]] = set()
    _trash_dir = None

    def __init__(self) -> None:
        self.seen: dict[str, Any] = {}

    def run(self) -> None:
        imp = config["import"]
        self.seen["threaded"] = config["threaded"].get(bool)
        self.seen["duplicate_action"] = imp["duplicate_action"].get()
        self.seen["autotag"] = imp["autotag"].get(bool)
        self.seen["singletons"] = imp["singletons"].get(bool)


def test_worker_restores_duplicate_action_and_threaded() -> None:
    """The two keys that were mutated BEFORE the snapshot block and restored
    NOWHERE: after the run they must read back at their pre-run values (the
    worker still forces them during the run)."""
    from app.beets.import_session import run_import_worker

    config["import"]["duplicate_action"] = "skip"  # the user's config
    config["threaded"] = True  # the user's config

    session = _RecordingSession()
    run_import_worker(session)  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised

    # forced during the run (the web review IS the "ask"; serial execution):
    assert session.seen["duplicate_action"] == "ask"
    assert session.seen["threaded"] is False
    # ...and restored verbatim after it — nothing leaks into the live global:
    assert config["import"]["duplicate_action"].get() == "skip"
    assert config["threaded"].get(bool) is True


def test_worker_pins_singletons_off_for_default_review_import() -> None:
    """A DEFAULT review import (no sweep, no directive) must run with
    ``import.singletons`` pinned False even under a ``singletons: yes`` user
    config — previously only the sweep/apply branches forced it, so the
    review path funnelled every album into choose_item's SKIP funnel:
    importing NOTHING while recording import history. The pin is asserted at
    ``run()`` call time (before the pipeline starts); the user value is
    restored after."""
    from app.beets.import_session import run_import_worker

    config["import"]["singletons"] = True  # the user's config

    session = _RecordingSession()
    run_import_worker(session)  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised

    assert session.seen["singletons"] is False  # pinned for this run
    assert config["import"]["singletons"].get(bool) is True  # user value restored
