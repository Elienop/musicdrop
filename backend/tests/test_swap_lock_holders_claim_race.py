"""No library job may start while a beets swap-lock holder does its work.

The holders beside config Apply (``test_config_apply_claim_race.py``) mutate
the library and, for the Trash restore, beets' process-global import config:
the restore runs ``run_import_worker`` with its own ``move`` overlay, and beets'
importer reads ``config["import"]`` live, so an import running alongside would
take the restore's flags. Each holder therefore has to ask for jobs AFTER it
holds the lock and under ``_CLAIM_LOCK``; a gate read before the acquire leaves
two gaps these tests drive:

* the handoff: ``asyncio.Lock.release()`` clears ``locked()`` before the next
  waiter resumes, so a claim made there passes ``_swap_in_progress()`` while the
  waiting holder goes on to hold the lock (only holders that WAIT on the lock);
* a claim from another thread already past its swap check but not yet in its
  slot when the holder takes the lock (every holder).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from fastapi import HTTPException, Request

from app import library_busy
from app.api import artists, trash
from app.artist_art_jobs.registry import artist_art_backfill_active, get_artist_art_backfill
from app.beets import cover, delete, duplicates, edit, rename
from app.lyrics_jobs.registry import get_lyrics_backfill, lyrics_backfill_active

_ANY = cast(Any, SimpleNamespace())
_LIBRARY_BUSY = library_busy.LIBRARY_BUSY_MESSAGE
_ART_BUSY = "An artist art job is running; image changes available when it finishes"

# Fail-safe only: every wait below is released by an event the scenario sets.
_DEADLINE_S = 10.0


class _EnteredWork(Exception):
    """Raised by a holder's stubbed first step inside the lock, to stop it there."""


def _claim_lyrics() -> None:
    get_lyrics_backfill().start(writes_enabled=False)


def _claim_artist_art() -> None:
    get_artist_art_backfill().start(force=False)


@dataclass(frozen=True)
class _Holder:
    name: str
    call: Callable[[Request], Coroutine[Any, Any, object]]
    # (module, attribute) of the first step the holder takes with the lock held.
    work: tuple[object, str]
    is_async_work: bool
    detail: str
    claim: Callable[[], None]
    job_active: Callable[[], bool]
    waits_on_the_lock: bool


def _reset(request: Request) -> Coroutine[Any, Any, object]:
    return artists.reset_artist_image_endpoint(request, "A", _ANY, _ANY, _ANY, _ANY)


HOLDERS = [
    _Holder(
        "duplicates.resolve",
        lambda r: duplicates.resolve_duplicates_op(r, _ANY),
        (duplicates, "_checked_store"),
        False,
        "Import in progress; resolve available when it finishes",
        _claim_lyrics,
        lyrics_backfill_active,
        True,
    ),
    _Holder(
        "duplicates.resolve_all",
        lambda r: duplicates.resolve_all_op(r, _ANY),
        (duplicates, "_checked_store"),
        False,
        "Import in progress; resolve available when it finishes",
        _claim_lyrics,
        lyrics_backfill_active,
        True,
    ),
    _Holder(
        "delete.album",
        lambda r: delete.delete_album_op(r, 1),
        (delete, "_checked_store"),
        False,
        "A library operation is in progress; delete available when it finishes",
        _claim_lyrics,
        lyrics_backfill_active,
        True,
    ),
    _Holder(
        "delete.artist",
        lambda r: delete.delete_artist_op(r, "A"),
        (delete, "_checked_store"),
        False,
        "A library operation is in progress; delete available when it finishes",
        _claim_lyrics,
        lyrics_backfill_active,
        True,
    ),
    _Holder(
        "rename.artist",
        lambda r: rename.apply_artist_rename_op(r, _ANY),
        (rename, "apply_artist_rename"),
        False,
        "A library operation is in progress; rename available when it finishes",
        _claim_lyrics,
        lyrics_backfill_active,
        True,
    ),
    _Holder(
        "edit.album",
        lambda r: edit.apply_album_edit_op(r, 1, _ANY),
        (edit, "apply_album_edit"),
        False,
        "A library operation is in progress; edit available when it finishes",
        _claim_lyrics,
        lyrics_backfill_active,
        True,
    ),
    _Holder(
        "cover.install",
        lambda r: cover.install_cover_op(r, 1, b""),
        (cover, "install_cover"),
        False,
        "A library operation is in progress; cover changes available when it finishes",
        _claim_lyrics,
        lyrics_backfill_active,
        True,
    ),
    _Holder(
        "trash.restore",
        lambda r: trash.restore_trash(r, cast(Any, SimpleNamespace(folder="x"))),
        (trash, "_child_or_404"),
        False,
        _LIBRARY_BUSY,
        _claim_lyrics,
        lyrics_backfill_active,
        False,
    ),
    _Holder(
        "trash.empty_one",
        lambda r: trash.empty_trash_one(r, "x"),
        (trash, "_child_or_404"),
        False,
        _LIBRARY_BUSY,
        _claim_lyrics,
        lyrics_backfill_active,
        False,
    ),
    _Holder(
        "trash.empty_all",
        lambda r: trash.empty_trash_all(r),
        (trash, "_store"),
        False,
        _LIBRARY_BUSY,
        _claim_lyrics,
        lyrics_backfill_active,
        False,
    ),
    # Narrower by design: only the artist-art sweep refuses the reset.
    _Holder(
        "artists.reset_image",
        _reset,
        (artists, "_move_override_to_trash"),
        True,
        _ART_BUSY,
        _claim_artist_art,
        artist_art_backfill_active,
        False,
    ),
]
_IDS = [h.name for h in HOLDERS]
_WAITERS = [h for h in HOLDERS if h.waits_on_the_lock]


def _request(lock: asyncio.Lock) -> Request:
    state = SimpleNamespace(
        beets_swap_lock=lock,
        beets_library=SimpleNamespace(lib=object()),
        settings=object(),
    )
    return cast(Request, SimpleNamespace(app=SimpleNamespace(state=state)))


def _stub_work(
    monkeypatch: pytest.MonkeyPatch, holder: _Holder, before: Callable[[], None]
) -> list[bool]:
    """Replace the holder's first in-lock step; record whether a job held the library."""
    seen: list[bool] = []

    def _sync(*_a: object, **_k: object) -> None:
        before()
        seen.append(holder.job_active())
        raise _EnteredWork

    async def _async(*_a: object, **_k: object) -> None:
        _sync()

    module, attr = holder.work
    monkeypatch.setattr(module, attr, _async if holder.is_async_work else _sync)
    return seen


def _assert_refused(outcome: BaseException | None, holder: _Holder) -> None:
    assert isinstance(outcome, HTTPException), outcome
    assert outcome.status_code == 409
    assert outcome.detail == holder.detail


@pytest.mark.parametrize("holder", _WAITERS, ids=[h.name for h in _WAITERS])
def test_a_claim_in_the_swap_lock_handoff_is_seen_by_the_waiting_holder(
    monkeypatch: pytest.MonkeyPatch, holder: _Holder
) -> None:
    entered = _stub_work(monkeypatch, holder, lambda: None)

    async def scenario() -> tuple[bool, BaseException | None, bool]:
        lock = asyncio.Lock()
        monkeypatch.setattr(library_busy, "_SWAP_LOCK", lock)
        await lock.acquire()  # another holder has the lock
        task = asyncio.create_task(holder.call(_request(lock)))
        await asyncio.sleep(0)  # a yield, not a delay: the task runs until it waits
        assert not task.done()

        lock.release()
        assert not lock.locked()  # the handoff gap: the waiter has not resumed yet
        claimed = True
        try:
            holder.claim()
        except RuntimeError:
            claimed = False

        outcome: BaseException | None = None
        try:
            await task
        except BaseException as exc:  # asserted below
            outcome = exc
        return claimed, outcome, lock.locked()

    claimed, outcome, still_locked = asyncio.run(scenario())

    assert claimed
    assert holder.job_active()
    assert entered == []
    _assert_refused(outcome, holder)
    assert not still_locked


class _Recorder:
    """A ``_CLAIM_LOCK`` stand-in that says when the holder's thread tries to take it."""

    def __init__(self, holder_thread: list[threading.Thread], tried: threading.Event) -> None:
        self._lock = threading.Lock()
        self._holder_thread = holder_thread
        self._tried = tried

    def __enter__(self) -> None:
        if threading.current_thread() in self._holder_thread:
            self._tried.set()
        self._lock.acquire()

    def __exit__(self, *_exc: object) -> None:
        self._lock.release()


class _SignallingLock(asyncio.Lock):
    def __init__(self, acquired: threading.Event) -> None:
        super().__init__()
        self._acquired = acquired

    async def acquire(self) -> Literal[True]:
        await super().acquire()
        self._acquired.set()
        return True


@pytest.mark.parametrize("holder", HOLDERS, ids=_IDS)
def test_a_claimer_past_its_swap_check_is_seen_by_the_holder(
    monkeypatch: pytest.MonkeyPatch, holder: _Holder
) -> None:
    """A real claim parked after it read the swap lock as free, before its slot is set.

    The holder then passes its pre-lock gate and takes the swap lock. Its job
    check must wait for the claim to finish, so it sees the slot. ``decided`` is
    set by whichever the holder reaches first: the claim lock, or its work.
    """
    parked, go, claimed = threading.Event(), threading.Event(), threading.Event()
    lock_acquired, decided = threading.Event(), threading.Event()
    holder_thread: list[threading.Thread] = []
    claimer_thread: list[threading.Thread] = []
    real_swap_in_progress = library_busy._swap_in_progress

    def _park_after_swap_check() -> bool:
        busy = real_swap_in_progress()
        if threading.current_thread() in claimer_thread:
            parked.set()
            assert go.wait(_DEADLINE_S)
        return busy

    def _reached_work() -> None:
        decided.set()
        assert claimed.wait(_DEADLINE_S)

    entered = _stub_work(monkeypatch, holder, _reached_work)
    monkeypatch.setattr(library_busy, "_swap_in_progress", _park_after_swap_check)
    monkeypatch.setattr(library_busy, "_CLAIM_LOCK", _Recorder(holder_thread, decided))
    lock = _SignallingLock(lock_acquired)
    monkeypatch.setattr(library_busy, "_SWAP_LOCK", lock)

    claim_errors: list[BaseException] = []
    outcome: list[BaseException] = []

    def _claim() -> None:
        try:
            holder.claim()
        except BaseException as exc:  # recorded and asserted below
            claim_errors.append(exc)
        finally:
            claimed.set()

    def _hold() -> None:
        try:
            asyncio.run(holder.call(_request(lock)))
        except BaseException as exc:  # recorded and asserted below
            outcome.append(exc)

    claimer_thread.append(threading.Thread(target=_claim, daemon=True))
    holder_thread.append(threading.Thread(target=_hold, daemon=True))
    claimer_thread[0].start()
    assert parked.wait(_DEADLINE_S)
    holder_thread[0].start()
    assert lock_acquired.wait(_DEADLINE_S)
    assert decided.wait(_DEADLINE_S)
    go.set()
    claimer_thread[0].join(_DEADLINE_S)
    holder_thread[0].join(_DEADLINE_S)
    assert not claimer_thread[0].is_alive()
    assert not holder_thread[0].is_alive()

    assert claim_errors == []
    assert holder.job_active()
    assert entered == []
    assert len(outcome) == 1
    _assert_refused(outcome[0], holder)
    assert not lock.locked()


@pytest.mark.parametrize("holder", HOLDERS, ids=_IDS)
def test_with_no_job_the_holder_reaches_its_work(
    monkeypatch: pytest.MonkeyPatch, holder: _Holder
) -> None:
    """Control: the in-lock check refuses on a job, not on the lock it holds."""
    entered = _stub_work(monkeypatch, holder, lambda: None)

    async def scenario() -> BaseException | None:
        lock = asyncio.Lock()
        monkeypatch.setattr(library_busy, "_SWAP_LOCK", lock)
        try:
            await holder.call(_request(lock))
        except BaseException as exc:  # asserted below
            return exc
        return None

    outcome = asyncio.run(scenario())

    assert entered == [False]
    # The stub's own exception, or the holder's blanket 500 wrapping it.
    assert outcome is not None
    assert isinstance(outcome, _EnteredWork) or isinstance(outcome.__cause__, _EnteredWork)


def test_the_image_reset_still_runs_beside_an_unrelated_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control for the reset's narrower set: a lyrics job does not refuse it."""
    holder = HOLDERS[_IDS.index("artists.reset_image")]
    entered = _stub_work(monkeypatch, holder, lambda: None)
    _claim_lyrics()

    async def scenario() -> BaseException | None:
        lock = asyncio.Lock()
        monkeypatch.setattr(library_busy, "_SWAP_LOCK", lock)
        try:
            await holder.call(_request(lock))
        except BaseException as exc:  # asserted below
            return exc
        return None

    outcome = asyncio.run(scenario())

    assert entered == [False]
    assert isinstance(outcome, _EnteredWork)
