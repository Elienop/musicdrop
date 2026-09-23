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

import pytest
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

    BOTH trash attributes are needed, not just ``_trash_dir``: the post-run pass
    reads ``_trash_origins_dir`` on the same line, BEFORE the
    ``_replace_album_ids`` gate, so a stand-in carrying only one raised an
    AttributeError that ``run_import_worker``'s broad ``except Exception``
    swallowed while every test here still passed.

    ``_playlists_dir`` and ``_dropped_item_ids`` for the single re-export point,
    which the worker calls in a ``finally`` on EVERY run — not inside a broad
    ``except``, so the same omission fails this file loudly."""

    lib: ClassVar[Any] = _BindOnlyLib()
    paths: ClassVar[list[bytes]] = []
    _replace_album_ids: ClassVar[set[int]] = set()
    _trash_dir = None
    _trash_origins_dir = None
    _playlists_dir = None
    _dropped_item_ids: ClassVar[set[int]] = set()

    def __init__(self) -> None:
        self.seen: dict[str, Any] = {}

    def run(self) -> None:
        imp = config["import"]
        self.seen["threaded"] = config["threaded"].get(bool)
        self.seen["duplicate_action"] = imp["duplicate_action"].get()
        self.seen["autotag"] = imp["autotag"].get(bool)
        self.seen["singletons"] = imp["singletons"].get(bool)
        self.seen["incremental"] = imp["incremental"].get()
        self.seen["incremental_skip_later"] = imp["incremental_skip_later"].get()
        self.seen["resume"] = imp["resume"].get()
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

    beets resolves move > link > hardlink > reflink > copy, each arm clearing the
    others, and keeps ``delete`` alive whenever copy is on
    (beets/importer/session.py:118-138). With only move/copy set, beets picked
    HARDLINK for a run the app called a copy, and ``delete: yes`` turned that
    copy into a move. Every flag is pinned for the run and restored after it."""
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
    """The arm every UI path takes: no request names an operation.

    A manual import and "Review now" send no options, the sweep and bank apply
    send ``operation: "default"``, so ``move=None`` is the real import path. Its
    five filing flags stay the user's, but ``delete`` is a destroy-the-source
    choice: beets keeps it alive whenever copy survives and then removes the
    originals, so a "copy" under ``delete: yes`` moved the user's download into
    the library. It is pinned off here and handed back afterwards.

    The only test that reaches the central pin: on the two explicit arms
    ``file_flags`` pins ``delete`` as well, so dropping ``"delete": False`` from
    ``forced`` turns THIS test red and leaves those two green.
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


def test_an_exception_inside_the_run_restores_every_key_and_frees_the_lock() -> None:
    """The ``finally`` is the only thing standing between a failed import and a
    permanently rewritten global. Every key the worker forces is set to a
    NON-default user value first, so a restore that wrote defaults instead of
    the snapshot would be visible; ``resume``/``reflink`` are set to their
    non-bool spellings because those are the two the snapshot reads verbatim.

    The lock is asserted free afterwards for the same reason: a `with` that
    leaked it would wedge every later import, and the failure would look like a
    hang rather than a raise.

    Mutant this kills: turning the ``try/finally`` into a bare call.
    """
    import pytest

    from app.beets.import_session import _CONFIG_FORCE_LOCK, run_import_worker

    user: dict[str, Any] = {
        "duplicate_action": "skip",
        "autotag": False,
        "singletons": True,
        "incremental": True,
        "incremental_skip_later": True,
        "resume": "ask",  # bool OR "ask"
        "search_ids": ["mbid-from-the-user"],
        "move": True,
        "copy": False,
        "link": True,
        "hardlink": True,
        "reflink": "auto",  # bool OR "auto"
        "delete": True,
    }
    for key, value in user.items():
        config["import"][key] = value
    config["threaded"] = True

    class _Boom(_RecordingSession):
        def run(self) -> None:
            super().run()
            raise RuntimeError("the pipeline died mid-import")

    session = _Boom()
    with pytest.raises(RuntimeError, match="died mid-import"):
        run_import_worker(session)  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised

    # the force was in effect when it died...
    assert session.seen["duplicate_action"] == "ask"
    assert session.seen["delete"] is False
    # ...and every user value came back verbatim anyway
    for key, value in user.items():
        assert config["import"][key].get() == value, key
    assert config["threaded"].get(bool) is True
    assert not _CONFIG_FORCE_LOCK.locked()


def test_a_contended_config_lock_refuses_instead_of_waiting() -> None:
    """An attended import holds the lock for the length of a HUMAN review:
    ``run()`` does not return until the browser answers, and ``park`` ends in an
    untimed ``slot.reply.get()``. The other caller is the Trash restore, which
    arrives on a request thread while holding the swap lock — so a blocking
    acquire there would 409 every library-mutating route for as long as someone
    leaves the review tab open, with nothing naming the cause.

    So a contended acquire is a BOUNDED wait and then a refusal — not an
    instant refusal, which is the distinction this test pins on both sides. The
    grace is deliberate: two legitimate sequential imports can contend for a
    moment as one finishes, and failing those would be worse than waiting.
    What must never happen is waiting on a human.

    The timeout is monkeypatched down so the suite does not sleep for the real
    one; it is read inside ``_config_force_lock`` at call time.

    Two mutants this kills: ``acquire(timeout=...)`` → ``acquire()``, which
    never returns (caught by the outer bound, via pytest-timeout or a hung
    run); and ``acquire(timeout=...)`` → ``acquire(blocking=False)``, which
    refuses instantly and is caught by the lower bound.
    """
    import time

    import pytest

    from app.beets import import_session as mod
    from app.beets.import_session import ImportConfigBusyError, run_import_worker

    grace = 0.05
    original = mod._CONFIG_FORCE_TIMEOUT_S
    mod._CONFIG_FORCE_TIMEOUT_S = grace
    session = _RecordingSession()
    assert mod._CONFIG_FORCE_LOCK.acquire(timeout=1), "lock should be free at test start"
    started = time.monotonic()
    try:
        with pytest.raises(ImportConfigBusyError, match="another import is in progress"):
            run_import_worker(session)  # type: ignore[arg-type]  # minimal stand-in; refused before .run()
    finally:
        mod._CONFIG_FORCE_LOCK.release()
        mod._CONFIG_FORCE_TIMEOUT_S = original
    elapsed = time.monotonic() - started
    assert session.seen == {}, "the pipeline must not have started"
    assert elapsed >= grace, f"refused in {elapsed:.3f}s — the grace period is not being used"
    assert elapsed < grace + 2, f"waited {elapsed:.2f}s — that is a wait, not a bounded one"


def test_the_resolved_file_operation_is_logged(caplog: Any) -> None:
    """The log line is the only record of what beets did to the user's files —
    and, since the config editor's own comment now points at it, the only signal
    a user gets that an inbox import overrode their ``hardlink: yes``. That
    makes it load-bearing for a documented promise, so it gets a reader.

    Two arms, because one alone proves less than it looks. On the DEFAULT arm a
    user ``hardlink: yes`` reads the same before and after the force — the arm
    leaves the filing flags alone — so that case cannot pin where the line
    sits. The EXPLICIT arm can: the force turns ``hardlink`` into ``copy``, so
    a line read above ``config.set`` logs the user's operation instead of the
    one beets actually ran, which is the whole point of the record.

    The logger is ``uvicorn.error``, not this module's own, and the test
    asserts it there on purpose: under the Dockerfile CMD uvicorn's
    LOGGING_CONFIG leaves app-namespace loggers at WARNING, so the same call on
    ``app.beets.import_session`` emits NOTHING in the shipped container
    (measured: effective level 30, ``isEnabledFor(INFO)`` False).
    ``main._boot_log`` documents the same trap for the same reason.
    """
    import logging

    from app.beets.import_session import run_import_worker

    config["import"]["hardlink"] = True  # the user's config

    # default arm: the user's own operation is what beets will run
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        run_import_worker(_RecordingSession())  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised
    assert "import file operation: hardlink" in caplog.text

    # explicit arm: the force wins, and the line must report the force
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        run_import_worker(_RecordingSession(), move=False)  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised
    assert "import file operation: copy" in caplog.text
    assert "hardlink" not in caplog.text, "logged the user's flag, not the forced one"


def test_a_non_bool_copy_is_refused_before_anything_is_filed() -> None:
    """``copy`` and ``move`` are the only file flags a default import leaves to
    the user's config, and beets reads both with ``.get(bool)`` — at
    ``importer/tasks.py:307-311``, inside ``finalize``, which runs AFTER
    ``manipulate_files`` has already filed the album. So a hand-edited
    ``copy: 1`` (YAML parses bare ``1`` as int, and confuse's bool template
    validates rather than coerces) used to file the album and THEN fail the job.

    Reading them above the first mutation puts the raise back where the other
    early exits are: nothing forced, nothing filed, nothing to restore.

    Mutant this kills: deleting the validating loop.
    """
    import pytest
    from confuse import ConfigTypeError

    from app.beets.import_session import run_import_worker

    config["import"]["copy"] = 1  # hand-edited config.yaml; Settings would coerce it

    session = _RecordingSession()
    with pytest.raises(ConfigTypeError, match="must be a bool"):
        run_import_worker(session)  # type: ignore[arg-type]  # minimal stand-in; raises before .run()

    assert session.seen == {}  # the pipeline never started
    assert config["import"]["copy"].get() == 1  # nothing was forced or restored over it


def test_a_string_write_is_refused_before_any_row_is_added() -> None:
    """beets reads ``write`` with ``.get(bool)`` in ``manipulate_files``
    (``importer/stages.py:296``), after ``_apply_choice`` has added the rows
    (``:319``). beets' loader reads a hand-edited ``write: n`` as the string
    ``'n'``, so every import added its rows and then failed.

    Mutant this kills: dropping ``write`` from the validating loop.
    """
    import pytest
    from confuse import ConfigTypeError

    from app.beets.import_session import run_import_worker

    config["import"]["write"] = "n"  # hand-edited config.yaml; Validate and Save refuse it

    session = _RecordingSession()
    with pytest.raises(ConfigTypeError, match=r"^import\.write: must be a bool, not str$"):
        run_import_worker(session)  # type: ignore[arg-type]  # minimal stand-in; raises before .run()

    assert session.seen == {}  # the pipeline never started
    assert config["import"]["write"].get() == "n"  # nothing was forced or restored over it


def test_the_lock_covers_the_post_run_trash_pass(monkeypatch: Any) -> None:
    """The lock is held for the WHOLE call, not just the config mutation.

    ``_trash_replaced_albums`` runs after the ``finally`` has already restored
    the config, so narrowing the lock to the config region looks free — it was
    tried and reverted. That pass moves albums and drops rows through the same
    ``Library`` handle, and a Trash restore reaches ``run_import_worker`` on a
    request thread through the check-then-act window ``library_busy`` documents
    against itself. Releasing early lets that restore start a second import
    while the first is still writing. The import job slot cannot stop it —
    ``on_finish`` runs only after this function returns — and in that window the
    restore already HOLDS the swap lock, so the swap lock serialises nothing.
    This lock is the only thing left.

    Mutant this kills: moving ``_config_force_lock()`` off the outer ``with``
    and around the config region alone.
    """
    from app.beets import import_session as mod

    seen: dict[str, bool] = {}

    def _probe(session: object) -> None:
        seen["locked_during_trash_pass"] = mod._CONFIG_FORCE_LOCK.locked()

    monkeypatch.setattr(mod, "_trash_replaced_albums", _probe)
    mod.run_import_worker(_RecordingSession())  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised

    assert seen["locked_during_trash_pass"] is True
    assert not mod._CONFIG_FORCE_LOCK.locked()  # and released on the way out


def test_the_force_and_restore_cost_two_config_sources_not_two_per_key() -> None:
    """confuse never removes a source. ``config[view][key] = v`` is
    ``RootView.set``, which does ``sources.insert(0, ...)``, so the natural
    per-key shape appended one permanent overlay PER KEY PER IMPORT — measured
    17-23 per import before this changed, against 2 after, and the cost lands
    on every read of a key no overlay sets plus every "Effective config"
    flatten, for the life of the process (measured: 1.5 us -> 434 us and
    0.8 ms -> 129 ms at 500 imports' worth of stack).

    Nothing else would notice a refactor back to the per-key form, which is why
    this counts instead of asserting values. Exactly 2: one force, one restore.
    ``_RecordingSession.run`` does not call beets' ``set_config``, so the count
    here is the app's own contribution with the engine's excluded.

    Two imports, not one, because the cost that matters is the PER-IMPORT one:
    a flat 2 each is the property, and the first read of a cold ``LazyConfig``
    materialises ``config_default.yaml`` and adds 2 one-time sources of its own
    (measured: first call delta 4, every later call 2). Warming it first keeps
    this measuring the app instead of confuse's lazy init.

    Mutant this kills: either ``config.set({...})`` expanded to a per-key loop.
    """
    from app.beets.import_session import run_import_worker

    config["import"]["copy"].get()  # materialise the lazy config, once
    before = len(config.sources)

    run_import_worker(_RecordingSession())  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised
    assert len(config.sources) - before == 2, "one force + one restore"

    run_import_worker(_RecordingSession())  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised
    assert len(config.sources) - before == 4, "2 per import, flat — not 2 per KEY"


# --- import.incremental: the four forcing arms, in order ---------------------
#
# beets records a folder in its import history when ``incremental`` is on and
# the album was not SKIPped under ``incremental_skip_later``
# (``importer/tasks.py:301-305``), and skips a recorded folder before any
# session hook fires (``importer/session.py:246-256``). The arms are exclusive
# and ordered, so each test below sets an ambient config that a LATER arm would
# answer differently — otherwise a mutant that drops one arm is caught by the
# next one's default.


def test_a_bank_apply_directive_beats_a_hardlink_config() -> None:
    """Arm 1. The sweep history-recorded every folder it banked, so an apply
    run of one of those folders must be non-incremental or beets skips it
    before any hook fires and the apply silently does nothing.

    The ambient config is the case arm 4 would answer the other way (a user
    hardlinking, with their own ``incremental: yes``), so this pins the
    ORDER too: ``incremental`` off, and ``incremental_skip_later`` left alone
    because only the sweep arm has a reason to touch it.

    Ambient ``incremental_skip_later`` is ``False`` on purpose — arm 4 forces it
    ``True``, so the "untouched" assertion below can only pass for this arm.
    """
    from app.beets.import_session import run_import_worker
    from app.models.bank import BankApplyDirective

    config["import"]["hardlink"] = True  # the user's config: keep downloads
    config["import"]["incremental"] = True
    config["import"]["incremental_skip_later"] = False

    session = _RecordingSession()
    directive = BankApplyDirective(action="asis")
    run_import_worker(session, directive=directive)  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised

    assert session.seen["incremental"] is False
    assert session.seen["incremental_skip_later"] is False  # the user's, untouched
    assert config["import"]["incremental"].get(bool) is True  # restored


def test_a_sweep_records_the_folders_it_banks_over_a_user_skip_later() -> None:
    """Arm 2. A user's ``incremental_skip_later: yes`` stops beets recording a
    folder the sweep SKIPped — which is every folder the sweep banks — so every
    later sweep re-banked the same folders. The bank is their re-entry path.

    The ambient config hardlinks, which is arm 4's case and the realistic one
    for a user who sweeps a keep-downloads library: arm 4 would leave
    ``incremental_skip_later`` ON and re-bank the same folders every sweep. This
    is the only test that crosses those two arms, so it is what pins the order.
    """
    from app.beets.import_session import run_import_worker

    config["import"]["hardlink"] = True  # the user's config: keep downloads
    config["import"]["incremental"] = False  # the user's config
    config["import"]["incremental_skip_later"] = True

    session = _RecordingSession()
    run_import_worker(session, sweep=True)  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised

    assert session.seen["incremental"] is True
    assert session.seen["incremental_skip_later"] is False
    # ...and both user values handed back
    assert config["import"]["incremental"].get(bool) is False
    assert config["import"]["incremental_skip_later"].get(bool) is True


def test_the_per_run_override_turns_history_off_under_a_hardlink_config() -> None:
    """Arm 3, the ``beet import -I`` half. A kept folder whose album has left
    the library is in beets' history, so re-adding it imports nothing; this is
    the request field that gets one run past that. Ambient config is arm 4's
    (hardlink), which would otherwise force history ON.

    ``resume`` goes off with it: beets clears ``resume`` only when
    ``incremental`` is ON (``importer/session.py:98-101``), so this is the one
    arm where the coupling gives nothing — and the same task-factory check that
    skips a history folder skips one held by a resume record.
    """
    from app.beets.import_session import run_import_worker

    config["import"]["hardlink"] = True  # the user's config: keep downloads
    config["import"]["incremental"] = True
    config["import"]["resume"] = True  # the user's config

    session = _RecordingSession()
    run_import_worker(session, incremental=False)  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised

    assert session.seen["incremental"] is False
    assert session.seen["resume"] is False
    assert session.seen["hardlink"] is True  # the file operation is untouched
    assert config["import"]["incremental"].get(bool) is True  # restored
    assert config["import"]["resume"].get(bool) is True  # restored


def test_a_hardlink_run_goes_incremental_and_offers_a_skipped_album_again() -> None:
    """Arm 4, the reason this exists. A hardlink leaves the download in place,
    so adding the same folder again would import the album a SECOND time onto
    one set of files — two library rows over one file. beets' own history is
    what refuses that.

    ``incremental_skip_later`` goes on with it: an album the user SKIPped is
    not recorded, so the next run offers it again. Nothing FORCES hardlink (the
    keep-downloads setting writes it into the user's config), so the arm is
    driven the way a user reaches it — their own ``hardlink: yes``.

    ``resume`` off EXPLICITLY, like the sweep arm: beets clears it for an
    incremental run today, and this must not depend on that surviving a bump.
    """
    from app.beets.import_session import run_import_worker

    config["import"]["hardlink"] = True  # the user's config: keep downloads
    config["import"]["incremental"] = False
    config["import"]["incremental_skip_later"] = False
    config["import"]["resume"] = True  # the user's config

    session = _RecordingSession()
    run_import_worker(session)  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised

    assert session.seen["incremental"] is True
    assert session.seen["incremental_skip_later"] is True
    assert session.seen["resume"] is False
    # ...and no value survives the run
    assert config["import"]["incremental"].get(bool) is False
    assert config["import"]["incremental_skip_later"].get(bool) is False
    assert config["import"]["resume"].get(bool) is True


def test_an_inbox_move_under_a_hardlink_config_leaves_history_to_the_user() -> None:
    """Arm 5, and the reason arm 4 reads the FORCED operation rather than the
    live config. An inbox import forces ``move``, so the download does not
    survive the run and nothing can re-import it — but the user's config still
    says ``hardlink: yes``. Reading the config instead of the merged flags would
    turn history on for every inbox drop, and beets then skips a folder whose
    files moved away and came back.
    """
    from app.beets.import_session import run_import_worker

    config["import"]["hardlink"] = True  # the user's config
    config["import"]["incremental"] = False

    session = _RecordingSession()
    run_import_worker(session, move=True)  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised

    assert session.seen["move"] is True
    assert session.seen["hardlink"] is False  # the forced operation is a move
    assert session.seen["incremental"] is False  # the user's, untouched
    assert session.seen["incremental_skip_later"] is False


def test_an_in_place_restore_under_a_hardlink_config_leaves_history_to_the_user() -> None:
    """Arm 5 again, for the Trash restore: ``in_place`` files nothing, so the
    merged flags resolve to ``in_place`` even under ``hardlink: yes``."""
    from app.beets.import_session import run_import_worker

    config["import"]["hardlink"] = True  # the user's config
    config["import"]["incremental"] = False

    session = _RecordingSession()
    run_import_worker(session, in_place=True)  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised

    assert session.seen["hardlink"] is False
    assert session.seen["incremental"] is False
    assert session.seen["incremental_skip_later"] is False


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("copy", True),  # beets' own default, spelled out
        ("link", True),  # a symlink leaves the download exactly as a hardlink does
        ("reflink", True),
        ("reflink", "auto"),
    ],
)
def test_a_config_that_is_not_hardlink_is_left_alone(flag: str, value: object) -> None:
    """Arm 5's deliberate gap, and what the ``== "hardlink"`` predicate means.

    A copy, a symlink and a reflink all leave the download in place, so each has
    the same re-import shape as a hardlink. None of them gets history:
    ``hardlink`` is the spelling the keep-downloads setting writes into beets'
    config (``decisions`` #53), and turning history on for a config the user
    built themselves would change what their existing setup does.

    The config is the one a real user has: beets' shipped ``copy: yes`` plus
    whichever flag they added. ``copy`` is the LOWEST precedence of the five —
    move > link > hardlink > reflink each clear it (``importer/session.py:114-133``)
    — so leaving it on is what the link/reflink rows have to survive.
    """
    from app.beets.import_session import run_import_worker

    config["import"][flag] = value
    config["import"]["incremental"] = False

    session = _RecordingSession()
    run_import_worker(session)  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised

    assert session.seen[flag] == value
    assert session.seen["hardlink"] is False
    assert session.seen["incremental"] is False
    assert session.seen["incremental_skip_later"] is False


def test_a_hardlink_run_still_costs_two_config_sources() -> None:
    """The arm that forces TWO extra keys must not cost a source per key:
    confuse never removes a source, so a per-key shape grows the stack for the
    life of the process (see the flat-cost test above)."""
    from app.beets.import_session import run_import_worker

    config["import"]["hardlink"] = True
    config["import"]["copy"].get()  # materialise the lazy config, once
    before = len(config.sources)

    run_import_worker(_RecordingSession())  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised

    assert len(config.sources) - before == 2, "one force + one restore"
