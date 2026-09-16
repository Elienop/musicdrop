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
import os
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, ClassVar

from beets import config


class _BindOnlyLib:
    """The bare slice of ``Library`` that ``run_import_worker`` needs to bind:
    a nullcontext stands in (these tests assert on config flags, never rows)."""

    def music_dir_context(self) -> AbstractContextManager[None]:
        return contextlib.nullcontext()


class _RecordingSession:
    """The bare slice of WebImportSession that ``run_import_worker`` touches.

    Empty ``paths`` -> the in-library guard no-ops. ``run()`` records the
    config values the pipeline would see.

    BOTH trash attributes are needed, not just ``_trash_dir``: the post-run
    pass reads ``_trash_origins_dir`` on the same line, BEFORE the
    ``_replace_album_ids`` gate, so a stand-in carrying only one raised an
    AttributeError that ``run_import_worker``'s broad ``except Exception``
    logged and swallowed. Every test here passed anyway, which is why the
    omission survived — the docstring claimed an early return the pass never
    reached."""

    lib: ClassVar[Any] = _BindOnlyLib()
    paths: ClassVar[list[bytes]] = []
    _replace_album_ids: ClassVar[set[int]] = set()
    _trash_dir = None
    _trash_origins_dir = None

    def __init__(self) -> None:
        self.seen: dict[str, Any] = {}

    def run(self) -> None:
        imp = config["import"]
        self.seen["threaded"] = config["threaded"].get(bool)
        self.seen["duplicate_action"] = imp["duplicate_action"].get()
        self.seen["autotag"] = imp["autotag"].get(bool)
        self.seen["singletons"] = imp["singletons"].get(bool)
        for flag in ("move", "copy", "link", "hardlink", "reflink", "delete"):
            self.seen[flag] = imp[flag].get()


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


def test_in_library_copy_refusal_mutates_nothing(tmp_path: Path) -> None:
    """The in-library guard's raise sits BETWEEN the first snapshots and the
    try/finally: a copy-refusal must leave the process-global config exactly
    as found. The deep-review probe caught ``threaded`` and
    ``duplicate_action`` leaking on this early exit — the finally never runs,
    so the only safe shape is "nothing mutates above the last raise"."""
    import pytest

    from app.beets.import_session import InLibraryCopyError, run_import_worker

    config["threaded"] = True  # the user's config
    config["import"]["duplicate_action"] = "skip"  # the user's config

    lib_dir = tmp_path / "music"
    (lib_dir / "Album").mkdir(parents=True)

    class _InLibraryLib(_BindOnlyLib):
        directory = os.fsencode(str(lib_dir))

    session = _RecordingSession()
    session.lib = _InLibraryLib()  # type: ignore[misc]  # ClassVar shadowed on purpose: this test needs the guard to fire
    session.paths = [os.fsencode(str(lib_dir / "Album"))]  # type: ignore[misc]

    with pytest.raises(InLibraryCopyError):
        run_import_worker(session, move=False)  # type: ignore[arg-type]  # minimal stand-in; the guard raises before run()

    assert session.seen == {}  # the pipeline never started
    assert config["threaded"].get(bool) is True
    assert config["import"]["duplicate_action"].get() == "skip"


def test_explicit_copy_pins_every_file_flag_and_never_deletes() -> None:
    """An explicit COPY must reach beets as a copy and nothing else.

    beets resolves move > link > hardlink > reflink > copy, each arm clearing
    the others, and keeps ``delete`` alive whenever copy is on
    (beets/importer/session.py:118-138). The worker used to set only
    move/copy, so under these user flags beets picked HARDLINK for a run the
    app called a copy, and ``delete: yes`` turned that copy into a move -
    removing the user's download. Every flag is pinned for the run and every
    one is restored after it."""
    from app.beets.import_session import run_import_worker

    config["import"]["hardlink"] = True  # the user's config
    config["import"]["link"] = True
    config["import"]["reflink"] = True
    config["import"]["delete"] = True

    session = _RecordingSession()
    run_import_worker(session, move=False)  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised

    # what beets sees at pipeline start: a copy, and only a copy
    assert session.seen["copy"] is True
    assert session.seen["move"] is False
    assert session.seen["link"] is False
    assert session.seen["hardlink"] is False
    assert session.seen["reflink"] is False
    assert session.seen["delete"] is False

    # ...and every user value restored afterwards
    assert config["import"]["hardlink"].get(bool) is True
    assert config["import"]["link"].get(bool) is True
    assert config["import"]["reflink"].get() is True
    assert config["import"]["delete"].get(bool) is True


def test_explicit_move_pins_every_file_flag() -> None:
    """The move half of the same rule: an explicit MOVE must not leave the
    user's link/hardlink/reflink flags standing. beets clears them itself when
    ``move`` wins its precedence chain, so this pins the app's own intent
    rather than relying on that ordering staying as it is."""
    from app.beets.import_session import run_import_worker

    config["import"]["hardlink"] = True  # the user's config

    session = _RecordingSession()
    run_import_worker(session, move=True)  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised

    assert session.seen["move"] is True
    assert session.seen["copy"] is False
    assert session.seen["hardlink"] is False
    assert session.seen["delete"] is False
    assert config["import"]["hardlink"].get(bool) is True


def test_default_operation_pins_delete_off_and_leaves_filing_to_the_user() -> None:
    """The arm every UI path takes, and the one the flag-pinning commit missed.

    No UI request names an operation (a manual import and "Review now" send no
    options, the sweep and bank apply send ``operation: "default"``), so
    ``move=None`` is the real import path. Its five filing flags are the user's
    to choose — copy vs move vs hardlink is a filing preference — but
    ``delete`` is not a filing choice, it is a destroy-the-source choice: beets
    keeps it alive whenever copy survives and then removes the originals, so a
    "copy" under ``delete: yes`` silently moved the user's download into the
    library. MusicDrop never destroys a source, so ``delete`` is pinned off
    here too, and the user's own value is handed back afterwards.

    This is the only test that reaches the central pin: on the two explicit
    arms ``file_flags`` pins ``delete`` as well, so dropping ``"delete": False``
    from ``forced`` turns THIS test red and leaves those two green.
    """
    from app.beets.import_session import run_import_worker

    config["import"]["hardlink"] = True  # the user's config
    config["import"]["reflink"] = "auto"
    config["import"]["delete"] = True

    session = _RecordingSession()
    run_import_worker(session)  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised

    # the destructive flag is ours to pin...
    assert session.seen["delete"] is False
    # ...and the filing flags stay the user's on this path
    assert session.seen["hardlink"] is True
    assert session.seen["reflink"] == "auto"

    # every user value restored verbatim, "auto" included
    assert config["import"]["hardlink"].get(bool) is True
    assert config["import"]["reflink"].get() == "auto"
    assert config["import"]["delete"].get(bool) is True


def test_in_place_pins_every_file_flag_and_never_deletes() -> None:
    """The restore path (``trash_manage._restore_by_import``) files nothing and
    must destroy nothing. It is safe today only because beets clears ``delete``
    when ``copy`` is off — the exact upstream coupling the explicit arm pins
    rather than relying on, so this arm pins it too.

    A state assertion, not a mutant-killer, and the distinction is measured:
    ``delete`` is pinned TWICE on this path (once centrally in ``forced``, once
    by ``file_flags``), so no single-line mutant reaches it — dropping either
    one alone leaves these assertions green, and only removing both turns this
    test red. It is here to pin the guarantee, not a line.
    """
    from app.beets.import_session import run_import_worker

    config["import"]["copy"] = True  # the user's config
    config["import"]["hardlink"] = True
    config["import"]["delete"] = True

    session = _RecordingSession()
    run_import_worker(session, in_place=True)  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised

    for flag in ("move", "copy", "link", "hardlink", "reflink", "delete"):
        assert session.seen[flag] is False, flag
    assert config["import"]["delete"].get(bool) is True  # restored


def test_the_config_force_region_is_serialised() -> None:
    """The snapshot/force/restore block mutates a process-global with a plain
    try/finally, so two overlapping calls interleave: the second snapshots the
    first's FORCED values and its finally writes them in as the user's,
    permanently rewriting the live global. Worse, beets re-reads that global
    late (``ImportTask.finalize`` -> ``cleanup``), so an explicit MOVE can
    reach finalize reading another run's copy+delete and remove a source.

    Asserted from inside ``run()`` — the one point that is provably within the
    region — by trying to take the lock without blocking. A thread test would
    pin the same invariant by racing for it; this pins it deterministically.

    Mutant this kills: dropping ``_CONFIG_FORCE_LOCK`` from the ``with``.
    """
    from app.beets.import_session import _CONFIG_FORCE_LOCK, run_import_worker

    class _LockProbe(_RecordingSession):
        def run(self) -> None:
            super().run()
            got = _CONFIG_FORCE_LOCK.acquire(blocking=False)
            if got:
                _CONFIG_FORCE_LOCK.release()
            self.seen["lock_was_free"] = got

    session = _LockProbe()
    run_import_worker(session)  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised

    assert session.seen["lock_was_free"] is False
    assert not _CONFIG_FORCE_LOCK.locked()  # released on the way out
