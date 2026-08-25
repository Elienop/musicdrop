"""Unit tests for the shared single-slot job-registry base.

Exercises the common lifecycle (start -> set_total -> record -> finish/fail/stop)
through a minimal concrete subclass, so the extracted core is covered directly
rather than only via the four package registries.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

import pytest

from app.jobs.base import JobState, SingleSlotRegistry

Phase = Literal["idle", "running", "done", "stopped", "failed"]


@dataclass
class _FakeJob(JobState):
    phase: Phase = "running"
    counted: int = 0


class _FakeRegistry(SingleSlotRegistry[_FakeJob]):
    """Smallest subclass that fills the extension points the base leaves open."""

    def start(self) -> str:
        with self._lock:
            if self._job is not None and self._job.phase == "running":
                raise RuntimeError("already running")
            job = _FakeJob(id=uuid.uuid4().hex)
            self._job = job
            return job.id

    def record(self) -> None:
        with self._lock:
            if self._job is not None:
                self._job.processed += 1
                self._job.counted += 1

    def snapshot(self) -> _FakeJob | None:
        with self._lock:
            return self._job


def test_idle_before_start() -> None:
    reg = _FakeRegistry()
    assert reg.is_running() is False
    assert reg.snapshot() is None


def test_start_marks_running_and_returns_id() -> None:
    reg = _FakeRegistry()
    job_id = reg.start()
    assert reg.is_running() is True
    snap = reg.snapshot()
    assert snap is not None
    assert snap.id == job_id
    assert snap.phase == "running"


def test_double_start_raises() -> None:
    reg = _FakeRegistry()
    reg.start()
    with pytest.raises(RuntimeError):
        reg.start()


def test_set_total_and_record_and_set_current() -> None:
    reg = _FakeRegistry()
    reg.start()
    reg.set_total(5)
    reg.record()
    reg.record()
    reg.set_current("track 2")
    snap = reg.snapshot()
    assert snap is not None
    assert (snap.total, snap.processed, snap.counted) == (5, 2, 2)
    assert snap.current == "track 2"


def test_finish_transitions_and_clears_current() -> None:
    reg = _FakeRegistry()
    reg.start()
    reg.set_current("in flight")
    reg.finish("done")
    snap = reg.snapshot()
    assert snap is not None
    assert snap.phase == "done"
    assert snap.current is None
    assert reg.is_running() is False


def test_finish_is_ignored_once_not_running() -> None:
    reg = _FakeRegistry()
    reg.start()
    reg.finish("done")
    reg.finish("stopped")  # a second terminal call must not overwrite the phase
    snap = reg.snapshot()
    assert snap is not None
    assert snap.phase == "done"


def test_fail_records_error_and_clears_current() -> None:
    reg = _FakeRegistry()
    reg.start()
    reg.set_current("in flight")
    reg.fail("kaboom")
    snap = reg.snapshot()
    assert snap is not None
    assert snap.phase == "failed"
    assert snap.error == "kaboom"
    assert snap.current is None


def test_fail_overrides_a_finished_job() -> None:
    reg = _FakeRegistry()
    reg.start()
    reg.finish("done")
    reg.fail("late crash")  # fail is not gated on running, unlike finish
    snap = reg.snapshot()
    assert snap is not None
    assert snap.phase == "failed"
    assert snap.error == "late crash"


def test_stop_is_cooperative() -> None:
    reg = _FakeRegistry()
    reg.start()
    assert reg.should_stop() is False
    reg.request_stop()
    assert reg.should_stop() is True


def test_request_stop_ignored_once_not_running() -> None:
    reg = _FakeRegistry()
    reg.start()
    reg.finish("done")
    reg.request_stop()
    assert reg.should_stop() is False


def test_lifecycle_methods_noop_before_start() -> None:
    reg = _FakeRegistry()
    reg.set_total(3)
    reg.record()
    reg.set_current("x")
    reg.finish("done")
    reg.fail("boom")
    reg.request_stop()
    assert reg.snapshot() is None
    assert reg.is_running() is False
    assert reg.should_stop() is False


class _SyncThread:
    """A Thread stand-in that runs its target synchronously on start() — keeps
    the test deterministic and leaks no real daemon into teardown."""

    def __init__(self, *, target: object, name: str, daemon: bool) -> None:
        self._target = target

    def start(self) -> None:
        self._target()  # type: ignore[operator]  # target is a callable in the test


def test_spawn_worker_runs_the_target(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = _FakeRegistry()
    reg.start()
    monkeypatch.setattr("app.jobs.base.threading.Thread", _SyncThread)
    ran: list[int] = []
    reg.spawn_worker(lambda: ran.append(1), name="musicdrop-fake")
    assert ran == [1]
    assert reg.is_running() is True


def test_spawn_worker_frees_the_slot_when_the_thread_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Thread.start() can raise under resource exhaustion. Without the guard the
    # slot stays stuck at phase="running" forever (no worker will ever finish
    # it), and since library_job_active() unions these slots that wedges EVERY
    # library mutation until restart. spawn_worker must fail the job (release the
    # slot) and re-raise.
    reg = _FakeRegistry()
    reg.start()

    class _BoomThread:
        def __init__(self, **kwargs: object) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr("app.jobs.base.threading.Thread", _BoomThread)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        reg.spawn_worker(lambda: None, name="musicdrop-fake")

    assert reg.is_running() is False  # slot released — mutations not wedged
    snap = reg.snapshot()
    assert snap is not None
    assert snap.phase == "failed"
    assert snap.error
    assert "musicdrop-fake" in snap.error
