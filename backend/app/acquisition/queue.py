"""The serial acquisition queue — FIFO + dedupe + gate-deferred drain.

A single daemon thread drains enqueued inbox folders through the EXISTING
``ImportJobRegistry.start`` — the same single import slot manual import uses.
That reuse is the whole design (Option A): the queue is NOT a new mutex
participant. Before each ``start`` it waits for the existing gate to clear (the
import slot itself + the three backfill predicates + the optional beets swap
lock), backing off without a tight spin and bailing promptly on shutdown. Its
status is informational only.

No beets/beetsplug imports here: the queue drives imports purely through the
public registry seam.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from pathlib import Path

from app.acquisition.inbox import contain
from app.acquisition.ledger import AcquisitionLedger
from app.import_jobs.gates import import_gate_clear
from app.import_jobs.registry import ImportJobRegistry
from app.import_jobs.runner import LibraryRootUnavailableError, SourcePathMissingError
from app.models.acquisition import AcquisitionQueueStatus, LedgerOutcome
from app.models.import_api import ImportPhase
from app.models.import_models import ImportOptions
from app.wire import display_path

logger = logging.getLogger(__name__)

#: Why a stopped inbox import is recorded as failed rather than imported.
_STOPPED_BEFORE_FINISH = "The import was stopped before this folder finished."


class AcquisitionQueue:
    """Thread-safe FIFO that serially imports inbox folders, deferring on a busy gate."""

    def __init__(
        self,
        *,
        import_registry: ImportJobRegistry,
        ledger: AcquisitionLedger,
        inbox_dir: Path | None = None,
        swap_lock: asyncio.Lock | None = None,
        poll_interval: float = 0.5,
        busy_backoff: float = 1.0,
    ) -> None:
        self._import_registry = import_registry
        self._ledger = ledger
        # When set, enqueue() re-rejects any path not contained under it — belt
        # and suspenders behind the webhook's own contain(), because the drain
        # performs the destructive MOVE import. None = no extra check (the unit
        # tests that drive the queue directly with already-trusted folders).
        self._inbox_dir = inbox_dir
        self._swap_lock = swap_lock
        self._poll_interval = poll_interval
        self._busy_backoff = busy_backoff

        # ``None`` is the shutdown sentinel that unblocks a parked ``get()``.
        self._queue: queue.Queue[Path | None] = queue.Queue()
        self._dedupe: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # Informational status (process-lifetime totals).
        self._phase: str = "idle"
        self._current: str | None = None
        self._processed = 0
        self._set_aside = 0
        self._failed = 0
        self._error: str | None = None

    # ----- lifecycle -----

    def start(self) -> None:
        """Spawn the single drain daemon thread (idempotent-safe to call once)."""
        self._thread = threading.Thread(
            target=self._drain, name="musicdrop-acquisition", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal shutdown, unblock the drain, and join it. Safe to call twice."""
        self._stop.set()
        self._queue.put(None)  # unblock a blocking get()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    # ----- producer -----

    def enqueue(self, folder: Path) -> None:
        """Add ``folder`` unless it is already queued/in-flight or in the ledger.

        Idempotent: a webhook retry (same folder) is a no-op. Refused once
        shutdown has begun so no work is accepted that the drain won't run.
        """
        if self._stop.is_set():
            return
        # ``strict=True``: a strict descendant only. contain() admits the inbox ROOT
        # itself, but MOVE-importing the root would sweep the whole inbox, so the
        # root is rejected here too. Belt-and-suspenders behind the webhook's guard.
        if self._inbox_dir is not None:
            if contain(str(folder), self._inbox_dir, strict=True) is None:
                # ``%r``: the value is caller-supplied (the webhook posts it), and a
                # name carrying a newline forges a whole log record under ``%s``.
                logger.warning("acquisition: refusing non-descendant inbox path %r", folder)
                return
        key = str(folder.resolve())
        with self._lock:
            if key in self._dedupe:
                return
            if self._ledger.seen(folder):
                return
            self._dedupe.add(key)
        self._queue.put(folder)

    # ----- status -----

    def status(self) -> AcquisitionQueueStatus:
        with self._lock:
            in_flight = 1 if self._current is not None else 0
            queued = max(len(self._dedupe) - in_flight, 0)
            phase: str = self._phase
            return AcquisitionQueueStatus(
                phase="running" if phase == "running" else "idle",
                queued=queued,
                current=self._current,
                processed=self._processed,
                set_aside=self._set_aside,
                failed=self._failed,
                error=self._error,
            )

    # ----- drain -----

    def _drain(self) -> None:
        while not self._stop.is_set():
            folder = self._queue.get()
            if folder is None or self._stop.is_set():
                return
            self._process_one(folder)

    def _process_one(self, folder: Path) -> None:
        key = str(folder.resolve())
        with self._lock:
            self._phase = "running"
            # The BASENAME, never ``str(folder)``: this field goes out in
            # ``GET /api/acquisition/status`` and both readers (the Review
            # page's "Importing now" line and the activity row's scope) already
            # reduce it with ``lastSegment``, so the rendered text is unchanged
            # while the absolute inbox path stops leaving the server.
            # ``display_path`` because an inbox name is whatever bytes a remote
            # peer chose, and a surrogate in it would fail the JSON encode.
            self._current = display_path(folder.name)

        if not self._wait_for_gate():
            return  # shutting down

        try:
            job_id = self._import_registry.start(
                str(folder),
                options=ImportOptions(operation="move", unattended=True),
                origin="inbox",
            )
        except (RuntimeError, LibraryRootUnavailableError) as exc:
            # The slot was claimed, or the music share dropped, between the gate
            # check and start() (TOCTOU). Without the second arm the refusal
            # escaped _drain and killed this daemon thread, stranding every later
            # download for the process lifetime (measured).
            #
            # Defer, and release ``_current`` while doing it - see ``_defer``.
            # "Leave status as-is" used to mean the page rendered "Importing
            # <folder>" behind a spinner for the whole outage.
            #
            # Only the library arm carries a REASON. Losing the race for the
            # single slot is not a fault: another import genuinely is running and
            # the page shows that one, so an error line would be noise. A music
            # share that dropped is an outage nothing else on this page names.
            reason = str(exc) if isinstance(exc, LibraryRootUnavailableError) else None
            self._defer(folder, error=reason)
            return
        except SourcePathMissingError as exc:
            # Two faults, ONE terminal outcome, and they must not share a
            # sentence: a folder the OS will not let us search is sitting right
            # there, and logged as gone an operator greps for it and finds it.
            #
            # Terminal for both: while either fault stands ``has_audio`` answers
            # False so the folder is in no listing anyway, and the moment it is
            # fixed "Review all" imports it ATTENDED. No ledger row, because
            # ``mark`` REPLACES any prior entry for that path - ``_finish`` is
            # the durable record, and a re-download re-enqueues.
            #
            # ``%r`` because ``enqueue`` never checks EXISTENCE, so a peer-chosen
            # name that never existed reaches this line; under ``%s`` a newline
            # in it forged a complete log record at another severity (measured).
            fault = "cannot be read" if exc.unreadable else "is no longer there"
            logger.warning("inbox drain: %r %s; dropped (%r)", folder, fault, exc)
            self._finish(key, "failed", str(exc))
            return

        result = self._wait_for_import(job_id)
        if result is None:
            return  # shutting down before the import finished
        outcome, error = result
        try:
            self._ledger.mark(folder, outcome=outcome)
        except OSError:
            pass  # best-effort; never crash the drain on a ledger write
        self._finish(key, outcome, error)

    def _defer(self, folder: Path, *, error: str | None) -> None:
        """Back off, requeue, and stop claiming an import is in flight.

        The dedupe entry STAYS, so ``status()`` keeps counting the folder as
        queued and a webhook retry is still a no-op. ``_current`` does not:
        only ``_finish`` used to clear it, so a folder that deferred forever
        left the status reporting ``phase="running"`` with ``current`` naming
        it, ``failed`` at 0 and ``error`` at ``None`` - "Importing <folder>"
        behind a spinner for an import that was never started. Cleared, the
        same line reads "Waiting for the import slot - N queued", which is what
        is actually happening.

        No counter moves: this folder has not finished. ``error`` is the fault
        the page shows under "Recently landed" when there is one to show.
        """
        with self._lock:
            self._current = None
            if error is not None:
                self._error = error
        self._stop.wait(self._busy_backoff)
        if not self._stop.is_set():
            self._queue.put(folder)

    def _wait_for_gate(self) -> bool:
        """Block until the import slot + gates are free. ``False`` if shutting down."""
        while not self._stop.is_set():
            if self._gate_clear():
                return True
            self._stop.wait(self._poll_interval)
        return False

    def _gate_clear(self) -> bool:
        """The existing job-mutex gate, consumed (never extended) by the queue."""
        return import_gate_clear(self._import_registry, self._swap_lock)

    def _wait_for_import(self, job_id: str) -> tuple[LedgerOutcome, str | None] | None:
        """Wait until OUR job reaches a terminal phase, then classify it. ``None`` on stop.

        Polls the slot BY JOB IDENTITY (``get(job_id)``) — never the global
        ``has_active_job()`` slot. An unrelated manual import claiming the slot
        in the poll gap must not make us block on, or misread, its outcome. The
        worker sets ``job.phase`` to done/failed directly and that terminal
        state persists in the slot until a new ``start()`` replaces it, so once
        our own job is terminal ``_result_for`` reads OUR outcome, not another's.
        """
        while not self._stop.is_set():
            job = self._import_registry.get(job_id)
            if job is None:
                # A newer import claimed the single slot before we could read our
                # job's terminal state (raced handoff) — see _raced_handoff.
                return self._raced_handoff()
            if job.phase in (ImportPhase.done, ImportPhase.failed):
                return self._result_for(job_id)
            self._stop.wait(self._poll_interval)
        return None

    def _result_for(self, job_id: str) -> tuple[LedgerOutcome, str | None]:
        # The abort flag is read FIRST, and the two reads are not interchangeable:
        # this one answers False once the slot holds another job, while state()
        # raises there and lands on _raced_handoff. Read the other way round, a
        # start() between them ledgers a cut-short folder "imported". Safe to
        # read early - the flag is final before the phase goes terminal, and
        # this runs only on a terminal phase.
        aborted = self._import_registry.job_aborted(job_id)
        try:
            state = self._import_registry.state(job_id)
        except KeyError:
            # Our job was replaced in the slot before we could read it — see
            # _raced_handoff.
            return self._raced_handoff()
        if state.phase == ImportPhase.failed:
            return ("failed", state.error)
        if state.stopped and aborted:
            # A stop that ACTUALLY aborted ended the run at the album it was on,
            # so nothing says this folder was handled - recording "imported"
            # would retire it in the ledger and no webhook retry would ever
            # offer it again. "failed" is the bucket the inbox list annotates
            # (_entry_outcome). A stop accepted after the last abort point
            # raises nothing and the folder imported in full.
            return ("failed", _STOPPED_BEFORE_FINISH)
        if state.set_aside > 0:
            return ("set_aside", None)
        return ("imported", None)

    @staticmethod
    def _raced_handoff() -> tuple[LedgerOutcome, str | None]:
        """Fallback when a newer import claimed the single slot before we could
        read our job's terminal outcome. We CANNOT tell whether the download
        imported, was set aside, or failed, so we surface it as ``failed`` —
        needs-attention in the inbox — rather than silently assuming ``imported``
        and losing a failed/set-aside drop. A disk/folder-existence heuristic is
        unreliable (a move-import leaves an empty source dir), so we avoid one."""
        return ("failed", None)

    def _finish(self, key: str, outcome: LedgerOutcome, error: str | None) -> None:
        with self._lock:
            self._dedupe.discard(key)
            self._current = None
            self._processed += 1
            if outcome == "set_aside":
                self._set_aside += 1
            elif outcome == "failed":
                self._failed += 1
            self._error = error
            if not self._dedupe:
                self._phase = "idle"
