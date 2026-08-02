"""Shared single-slot job-registry base for the background sweeps.

Four packages (``reorganize_jobs`` / ``disk_sync_jobs`` / ``lyrics_jobs`` /
``artist_art_jobs``) each run at most one background job at a time on a daemon
thread, tracked by an in-memory registry that the worker thread mutates while
API threads read status. The registry lifecycle is byte-identical across all
four — this module owns that common core so the four can't drift apart.

Each package subclasses :class:`SingleSlotRegistry` with a job dataclass that
extends :class:`JobState` and keeps the parts that genuinely differ: its own
``start`` (differing signatures), ``record`` (differing outcome types +
counters), ``state`` (differing wire status models), and any extra counters.

beets-free by construction — no beets import belongs here.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar, Generic, TypeVar


@dataclass
class JobState:
    """Fields every single-slot job carries.

    Subclasses add task-specific counters/fields and narrow ``phase`` to their
    own status ``Literal`` (each package's phase type is a superset that adds
    ``"idle"`` for the empty-slot state).
    """

    id: str
    phase: str = "running"
    total: int = 0
    processed: int = 0
    current: str | None = None
    error: str | None = None
    stop_requested: bool = False
    # When this job reached a terminal phase (UTC), or None while it still runs.
    # Stamped by ``finish``/``fail`` — the only two terminal transitions — so a
    # slot holding a finished job can say WHEN its result was produced. A job's
    # result outlives the run (the slot keeps the last job until the next start
    # replaces it), and without this the UI cannot tell a result from five
    # seconds ago from one produced before the user fixed the problem.
    finished_at: datetime | None = None


JobT = TypeVar("JobT", bound=JobState)


class SingleSlotRegistry(Generic[JobT]):
    """Holds at most one active (or last-finished) job behind a lock.

    Owns the lifecycle methods that are identical across the four job packages.
    Subclasses supply ``start``, ``record``, and ``state`` (plus any extra
    counters) and assign the constructed job to ``self._job`` inside ``start``.
    """

    def __init__(self) -> None:
        self._job: JobT | None = None
        self._lock = threading.Lock()

    # The name this job type answers to in ``library_busy``'s union (and in the
    # ``exclude`` that drops its own slot). Subclasses MUST set it — an unset
    # value would make a registry exclude nothing and refuse itself.
    job_type: ClassVar[str] = ""

    @contextmanager
    def _claim(self, message: str) -> Iterator[None]:
        """Hold the cross-job claim lock + this registry's lock while claiming.

        Every single-slot job funnels its claim through here so the union check
        ("is any OTHER library job running?") and the slot claim happen as ONE
        atomic step. Checking them separately let a daemon-started import slip
        between a user-started sweep's check and its claim, running a beets
        import and a whole-library reorganize over the same files at once.
        """
        from app.library_busy import claim_slot

        with claim_slot(self.job_type, message=message), self._lock:
            yield

    def is_running(self) -> bool:
        with self._lock:
            return self._job is not None and self._job.phase == "running"

    def set_total(self, total: int) -> None:
        with self._lock:
            if self._job is not None:
                self._job.total = total

    def set_current(self, label: str | None) -> None:
        with self._lock:
            if self._job is not None:
                self._job.current = label

    def should_stop(self) -> bool:
        with self._lock:
            return self._job is not None and self._job.stop_requested

    def request_stop(self) -> None:
        with self._lock:
            if self._job is not None and self._job.phase == "running":
                self._job.stop_requested = True

    def finish(self, phase: str) -> None:
        with self._lock:
            if self._job is not None and self._job.phase == "running":
                self._job.phase = phase
                self._job.current = None
                self._job.finished_at = datetime.now(UTC)

    def fail(self, message: str) -> None:
        with self._lock:
            # Deliberately NOT gated on running (unlike finish, pinned by
            # test_fail_overrides_a_finished_job): the runner's except clause is
            # the only reporter of a crash raised after finish() — a context
            # manager's __exit__ on the way out — and gating here would swallow
            # that error without a trace. The rare double-stamp of finished_at is
            # the price of never hiding a crash behind a green phase.
            if self._job is not None:
                self._job.phase = "failed"
                self._job.error = message
                self._job.current = None
                self._job.finished_at = datetime.now(UTC)

    def spawn_worker(self, target: Callable[[], None], *, name: str) -> None:
        """Spawn the job's daemon worker, freeing the slot if the thread refuses.

        ``Thread.start()`` can raise under resource exhaustion; without this
        guard the just-claimed slot would stay stuck at ``phase="running"``
        forever (no worker will ever run to finish it), and since
        ``library_job_active()`` unions these four slots that wedges EVERY
        library mutation until restart. On failure, fail the job — releasing the
        slot and surfacing the error — before re-raising, mirroring the import
        registry's own Thread.start guard. Call AFTER ``start`` has claimed the
        slot; the API's 500 then rides the re-raise.
        """
        thread = threading.Thread(target=target, name=name, daemon=True)
        try:
            thread.start()
        except Exception as exc:
            self.fail(f"could not start the {name} worker: {exc}")
            raise
