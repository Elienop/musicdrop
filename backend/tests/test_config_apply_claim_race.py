"""Config Apply and a library-job claim must never both proceed.

Apply replaces ``beets.config.sources`` wholesale, which drops the overlay a
running import forces (``delete: False``, ``duplicate_action``); beets'
importer reads ``config["import"]`` live, so the import would run on the
user's own flags. The job gate therefore has to be atomic with holding the
swap lock, not a check made before acquiring it.

The gap the first test drives is ``asyncio.Lock`` itself: ``release()`` sets
``_locked = False`` and only wakes the next waiter, which re-sets it when it
next runs. Between the two, ``locked()`` is False, so a claim made there passes
``_swap_in_progress()`` while the waiting Apply goes on to hold the lock.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Container
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from fastapi import HTTPException, Request

from app import library_busy
from app.beets import config_editor
from app.lyrics_jobs.registry import get_lyrics_backfill, lyrics_backfill_active

_BUSY_DETAIL = "Import in progress; Apply available when it finishes / lyrics backfill"


class _EnteredRebuild(Exception):
    """Raised by the stubbed first step inside the swap lock, to stop Apply there."""


def _request(lock: asyncio.Lock) -> Request:
    state = SimpleNamespace(beets_swap_lock=lock, beets_library=object(), settings=object())
    return cast(Request, SimpleNamespace(app=SimpleNamespace(state=state)))


@pytest.fixture
def entered(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Record, at Apply's first step inside the swap lock, whether a job holds the library."""
    seen: list[bool] = []

    def _stub(_handle: object, _settings: object) -> None:
        seen.append(library_busy.library_job_active())
        raise _EnteredRebuild

    monkeypatch.setattr(config_editor, "on_disk_layout_error", _stub)
    return seen


def test_a_claim_in_the_swap_lock_handoff_is_seen_by_the_waiting_apply(
    monkeypatch: pytest.MonkeyPatch, entered: list[bool]
) -> None:
    async def scenario() -> tuple[bool, BaseException | None]:
        lock = asyncio.Lock()
        monkeypatch.setattr(library_busy, "_SWAP_LOCK", lock)
        await lock.acquire()  # an earlier Apply (or resolve / trash op) holds the lock
        apply_task = asyncio.create_task(config_editor.apply(_request(lock)))
        await asyncio.sleep(0)  # a yield, not a delay: the task runs until it waits on the lock
        assert not apply_task.done()
        assert entered == []

        lock.release()
        assert not lock.locked()  # the handoff gap: the waiter has not resumed yet
        claimed = True
        try:
            get_lyrics_backfill().start(writes_enabled=False)
        except RuntimeError:
            claimed = False

        try:
            await apply_task
        except (HTTPException, _EnteredRebuild) as exc:
            return claimed, exc
        return claimed, None

    claimed, outcome = asyncio.run(scenario())

    assert claimed
    assert lyrics_backfill_active()
    # Apply must not reach its rebuild while the job's slot is set.
    assert entered == []
    assert isinstance(outcome, HTTPException)
    assert outcome.status_code == 409
    assert outcome.detail == _BUSY_DETAIL


def test_a_claim_made_while_apply_holds_the_lock_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused: list[bool] = []

    def _stub(_handle: object, _settings: object) -> None:
        try:
            get_lyrics_backfill().start(writes_enabled=False)
        except RuntimeError:
            refused.append(True)
        raise _EnteredRebuild

    monkeypatch.setattr(config_editor, "on_disk_layout_error", _stub)

    async def scenario() -> bool:
        lock = asyncio.Lock()
        monkeypatch.setattr(library_busy, "_SWAP_LOCK", lock)
        with pytest.raises(_EnteredRebuild):
            await config_editor.apply(_request(lock))
        return lock.locked()

    still_locked = asyncio.run(scenario())

    assert refused == [True]
    assert not still_locked
    assert not lyrics_backfill_active()


def test_apply_409s_and_releases_the_lock_while_a_job_holds_the_library(
    monkeypatch: pytest.MonkeyPatch, entered: list[bool]
) -> None:
    get_lyrics_backfill().start(writes_enabled=False)

    async def scenario() -> tuple[HTTPException, bool]:
        lock = asyncio.Lock()
        monkeypatch.setattr(library_busy, "_SWAP_LOCK", lock)
        with pytest.raises(HTTPException) as info:
            await config_editor.apply(_request(lock))
        return info.value, lock.locked()

    exc, still_locked = asyncio.run(scenario())

    assert exc.status_code == 409
    assert exc.detail == _BUSY_DETAIL
    assert not still_locked
    assert entered == []


# Fail-safe only: every wait below is released by an event the scenario sets.
_DEADLINE_S = 10.0


class _Recorder:
    """A ``_CLAIM_LOCK`` stand-in that says when Apply's thread tries to take it."""

    def __init__(self, apply_thread: list[threading.Thread], tried: threading.Event) -> None:
        self._lock = threading.Lock()
        self._apply_thread = apply_thread
        self._tried = tried

    def __enter__(self) -> None:
        if threading.current_thread() in self._apply_thread:
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


def test_a_claimer_past_its_swap_check_is_seen_by_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real claim parked after it read the swap lock as free, before its slot is set.

    Apply then takes the swap lock. Its job check must wait for the claim to
    finish, so it sees the slot; read without ``_CLAIM_LOCK`` it sees an empty
    union, and both proceed.
    """
    parked, go, claimed = threading.Event(), threading.Event(), threading.Event()
    lock_acquired, gate_decided = threading.Event(), threading.Event()
    apply_thread: list[threading.Thread] = []
    claimer_thread: list[threading.Thread] = []
    real_swap_in_progress = library_busy._swap_in_progress
    real_job_active = library_busy.library_job_active

    def _park_after_swap_check() -> bool:
        busy = real_swap_in_progress()
        if threading.current_thread() in claimer_thread:
            parked.set()
            assert go.wait(_DEADLINE_S)
        return busy

    def _job_active(*, exclude: Container[str] = ()) -> bool:
        active = real_job_active(exclude=exclude)
        if threading.current_thread() in apply_thread:
            gate_decided.set()
        return active

    monkeypatch.setattr(library_busy, "_swap_in_progress", _park_after_swap_check)
    monkeypatch.setattr(library_busy, "library_job_active", _job_active)
    monkeypatch.setattr(library_busy, "_CLAIM_LOCK", _Recorder(apply_thread, gate_decided))
    lock = _SignallingLock(lock_acquired)
    monkeypatch.setattr(library_busy, "_SWAP_LOCK", lock)

    entered: list[bool] = []

    def _stub(_handle: object, _settings: object) -> None:
        assert claimed.wait(_DEADLINE_S)
        entered.append(real_job_active())
        raise _EnteredRebuild

    monkeypatch.setattr(config_editor, "on_disk_layout_error", _stub)

    claim_errors: list[BaseException] = []
    outcome: list[BaseException] = []

    def _claim() -> None:
        try:
            get_lyrics_backfill().start(writes_enabled=False)
        except BaseException as exc:  # recorded and asserted below
            claim_errors.append(exc)
        finally:
            claimed.set()

    def _apply() -> None:
        try:
            asyncio.run(config_editor.apply(_request(lock)))
        except BaseException as exc:  # recorded and asserted below
            outcome.append(exc)

    claimer_thread.append(threading.Thread(target=_claim, daemon=True))
    apply_thread.append(threading.Thread(target=_apply, daemon=True))
    claimer_thread[0].start()
    assert parked.wait(_DEADLINE_S)
    apply_thread[0].start()
    assert lock_acquired.wait(_DEADLINE_S)
    # Apply holds the swap lock and has either read the union or is waiting to.
    assert gate_decided.wait(_DEADLINE_S)
    go.set()
    claimer_thread[0].join(_DEADLINE_S)
    apply_thread[0].join(_DEADLINE_S)
    assert not claimer_thread[0].is_alive()
    assert not apply_thread[0].is_alive()

    assert claim_errors == []
    assert lyrics_backfill_active()
    assert entered == []
    assert len(outcome) == 1
    assert isinstance(outcome[0], HTTPException)
    assert outcome[0].status_code == 409
