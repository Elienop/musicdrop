"""The serial acquisition queue: FIFO + dedupe + gate-deferred drain.

The queue is the only consumer of the existing import slot for inbox drops. It
drives folders through the SAME ``ImportJobRegistry.start`` manual import uses
(Option A: it is NOT a new mutex participant — it waits for the existing gate to
clear). Tests inject a ``FakeImportRunner``-backed registry so no beets/network
is touched, and ALWAYS ``q.stop()`` in a ``finally`` so no daemon thread leaks
into teardown.
"""

from __future__ import annotations

import errno
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import pytest

from app.acquisition.inbox import list_inbox
from app.acquisition.ledger import AcquisitionLedger
from app.acquisition.queue import AcquisitionQueue
from app.beets.import_session import ImportAbortError, ImportBridge
from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import ImportJob, ImportJobRegistry
from app.import_jobs.runner import (
    LibraryRootUnavailableError,
    SourcePathMissingError,
    unreadable_source_error,
)
from app.models.bank import BankApplyDirective
from app.models.import_api import ImportJobState, ImportPhase
from app.models.import_models import (
    AlbumChange,
    AlbumOutcome,
    AlbumOutcomeStatus,
    Candidate,
    ImportOptions,
    ImportOrigin,
    ParkedAlbum,
    Recommendation,
)

T = TypeVar("T")

_TERMINAL = (ImportPhase.done, ImportPhase.failed)
_INBOX_OPTS = ImportOptions(operation="move", unattended=True)


def _poll(
    get: Callable[[], T], pred: Callable[[T], bool], *, timeout: float = 2.0, interval: float = 0.01
) -> T:
    deadline = time.monotonic() + timeout
    value = get()
    while time.monotonic() < deadline:
        value = get()
        if pred(value):
            return value
        time.sleep(interval)
    return value


def _make_queue(
    tmp_path: Path,
) -> tuple[AcquisitionQueue, FakeImportRunner, ImportJobRegistry, AcquisitionLedger]:
    fake = FakeImportRunner()
    reg = ImportJobRegistry(runner=fake)
    led = AcquisitionLedger(tmp_path / "ledger.json")
    q = AcquisitionQueue(import_registry=reg, ledger=led, poll_interval=0.01, busy_backoff=0.02)
    return q, fake, reg, led


def test_status_idle_initially(tmp_path: Path) -> None:
    q, _fake, _reg, _led = _make_queue(tmp_path)
    s = q.status()
    assert s.phase == "idle"
    assert s.queued == 0
    assert s.current is None
    assert s.processed == 0
    assert s.set_aside == 0
    assert s.failed == 0
    assert s.error is None


def test_queue_drains_to_registry_with_move_unattended_inbox(tmp_path: Path) -> None:
    q, fake, reg, led = _make_queue(tmp_path)
    folder = tmp_path / "inbox" / "Album"
    folder.mkdir(parents=True)

    calls: list[tuple[str | list[str], ImportOptions | None, ImportOrigin]] = []
    real_start = reg.start

    def spy_start(
        source: str | list[str],
        *,
        options: ImportOptions | None = None,
        origin: ImportOrigin = "manual",
        directive: BankApplyDirective | None = None,
    ) -> str:
        calls.append((source, options, origin))
        return real_start(source, options=options, origin=origin, directive=directive)

    reg.start = spy_start  # type: ignore[method-assign]  # test spy delegates to the real start

    q.start()
    try:
        q.enqueue(folder)
        _poll(lambda: calls, lambda c: len(c) > 0)
        assert calls[0] == (
            str(folder),
            ImportOptions(operation="move", unattended=True),
            "inbox",
        )
        assert fake.received_options == ImportOptions(operation="move", unattended=True)
        # The drain marks the ledger + pops the dedupe entry once the import ends.
        _poll(lambda: led.seen(folder), lambda seen: seen is True)
        assert led.seen(folder) is True
        _poll(lambda: q.status().processed, lambda n: n >= 1)
        assert q.status().processed == 1
    finally:
        q.stop()


def test_queue_dedupes_same_folder(tmp_path: Path) -> None:
    q, _fake, _reg, _led = _make_queue(tmp_path)
    folder = tmp_path / "inbox" / "Album"
    folder.mkdir(parents=True)
    # Do NOT start the drain: enqueue twice and assert the FIFO collapsed to one.
    q.enqueue(folder)
    q.enqueue(folder)
    assert q._queue.qsize() == 1
    assert q.status().queued == 1


def test_queue_skips_ledger_seen_folder(tmp_path: Path) -> None:
    q, _fake, _reg, led = _make_queue(tmp_path)
    folder = tmp_path / "inbox" / "Album"
    folder.mkdir(parents=True)
    led.mark(folder, outcome="imported")
    q.enqueue(folder)
    assert q._queue.qsize() == 0
    assert q.status().queued == 0


def test_queue_defers_while_backfill_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    q, fake, _reg, _led = _make_queue(tmp_path)
    folder = tmp_path / "inbox" / "Album"
    folder.mkdir(parents=True)

    monkeypatch.setattr("app.lyrics_jobs.registry.lyrics_backfill_active", lambda: True)
    q.start()
    try:
        q.enqueue(folder)
        # The gate is closed: the import must NOT start while the backfill runs.
        time.sleep(0.2)
        assert fake.received_options is None
        assert q.status().phase == "running"  # waiting on the gate, not idle
        # Clear the backfill: the drain proceeds.
        monkeypatch.setattr("app.lyrics_jobs.registry.lyrics_backfill_active", lambda: False)
        _poll(lambda: fake.received_options, lambda o: o is not None)
        assert fake.received_options == ImportOptions(operation="move", unattended=True)
    finally:
        q.stop()


def test_queue_defers_while_swap_lock_held(tmp_path: Path) -> None:
    import asyncio

    fake = FakeImportRunner()
    reg = ImportJobRegistry(runner=fake)
    led = AcquisitionLedger(tmp_path / "ledger.json")
    swap_lock = asyncio.Lock()
    q = AcquisitionQueue(
        import_registry=reg,
        ledger=led,
        swap_lock=swap_lock,
        poll_interval=0.01,
        busy_backoff=0.02,
    )
    folder = tmp_path / "inbox" / "Album"
    folder.mkdir(parents=True)

    async def hold_then_release() -> None:
        await swap_lock.acquire()
        try:
            q.start()
            q.enqueue(folder)
            await asyncio.sleep(0.2)
            assert fake.received_options is None  # blocked while the lock is held
        finally:
            swap_lock.release()
        await asyncio.sleep(0.05)

    try:
        asyncio.run(hold_then_release())
        _poll(lambda: fake.received_options, lambda o: o is not None)
        assert fake.received_options == ImportOptions(operation="move", unattended=True)
    finally:
        q.stop()


def test_queue_refuses_out_of_inbox_path(tmp_path: Path) -> None:
    # Defense in depth: even though the webhook contains() first, the queue
    # performs the destructive MOVE import, so it re-rejects any path not under
    # its configured inbox_dir. Do NOT start the drain (deterministic).
    fake = FakeImportRunner()
    reg = ImportJobRegistry(runner=fake)
    led = AcquisitionLedger(tmp_path / "ledger.json")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    q = AcquisitionQueue(
        import_registry=reg,
        ledger=led,
        inbox_dir=inbox,
        poll_interval=0.01,
        busy_backoff=0.02,
    )

    outside = tmp_path / "outside" / "Album"
    outside.mkdir(parents=True)
    q.enqueue(outside)
    assert q._queue.qsize() == 0
    assert q.status().queued == 0
    assert len(q._dedupe) == 0

    # A path under the inbox is still accepted.
    inside = inbox / "Album"
    inside.mkdir()
    q.enqueue(inside)
    assert q._queue.qsize() == 1
    assert q.status().queued == 1


def test_queue_refuses_inbox_root_itself(tmp_path: Path) -> None:
    # The inbox ROOT is contained (contain() admits ``resolved == root``) but is
    # NOT a valid MOVE target: importing it would sweep the whole inbox. The queue
    # re-rejects it (defense in depth behind the webhook). Drain not started.
    fake = FakeImportRunner()
    reg = ImportJobRegistry(runner=fake)
    led = AcquisitionLedger(tmp_path / "ledger.json")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    q = AcquisitionQueue(
        import_registry=reg,
        ledger=led,
        inbox_dir=inbox,
        poll_interval=0.01,
        busy_backoff=0.02,
    )

    q.enqueue(inbox)
    assert q._queue.qsize() == 0
    assert q.status().queued == 0
    assert len(q._dedupe) == 0

    # A strict descendant is still accepted.
    inside = inbox / "Artist" / "Album"
    inside.mkdir(parents=True)
    q.enqueue(inside)
    assert q._queue.qsize() == 1
    assert q.status().queued == 1


def test_the_drop_log_escapes_a_folder_name_that_was_never_on_disk(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The dropped folder's NAME is caller-supplied, so the record must escape it.

    ``enqueue`` checks containment and dedupe but NOT existence, so a name that
    never named anything reaches this log. Under ``%s`` a newline in it produced
    a second, fully-formed record attributed to another module at another
    severity, through the real route with only the slskd webhook secret (which
    is gate-exempt - no session cookie needed).
    """
    q, fake, _reg, _led = _make_queue(tmp_path)
    fake.validate_error = SourcePathMissingError("That folder doesn't exist.")
    forged = tmp_path / (
        "Album\n2026-09-20 12:00:00 CRITICAL app.auth.gate: session gate DISABLED by operator"
    )
    caplog.set_level(logging.WARNING, logger="app.acquisition.queue")

    q._process_one(forged)

    records = [r for r in caplog.records if r.name == "app.acquisition.queue"]
    assert len(records) == 1, records
    message = records[0].getMessage()
    assert "\n" not in message, message
    # Escaped, not dropped: the operator still gets to see what was refused.
    assert "session gate DISABLED" in message


def test_failed_inbox_import_marks_failed_not_imported(tmp_path: Path) -> None:
    # The primary path: the drain reads OUR job's terminal (failed) state and
    # classifies it as failed — incrementing the failed counter and recording
    # "failed" in the ledger, never a phantom "imported".
    fake = FakeImportRunner(fail_with="boom")
    reg = ImportJobRegistry(runner=fake)
    led = AcquisitionLedger(tmp_path / "ledger.json")
    q = AcquisitionQueue(import_registry=reg, ledger=led, poll_interval=0.01, busy_backoff=0.02)
    folder = tmp_path / "inbox" / "Album"
    folder.mkdir(parents=True)

    q.start()
    try:
        q.enqueue(folder)
        _poll(lambda: q.status().failed, lambda n: n >= 1)
        s = q.status()
        assert s.failed == 1
        assert s.processed == 1
        assert s.set_aside == 0
        entry = next(e for e in led.entries() if e.path == str(folder))
        assert entry.outcome == "failed"
    finally:
        q.stop()


def test_raced_handoff_result_for_does_not_assume_imported(tmp_path: Path) -> None:
    # After our inbox job reaches a terminal phase, a SECOND import claims the
    # single slot, replacing ours. Reading _result_for for the original job_id
    # can no longer see our outcome (state() raises KeyError) — the fallback must
    # surface it as needs-attention ("failed"), NOT silently assume "imported".
    q, _fake, reg, _led = _make_queue(tmp_path)
    folder1 = tmp_path / "inbox" / "A"
    folder1.mkdir(parents=True)
    job1 = reg.start(str(folder1), options=_INBOX_OPTS, origin="inbox")
    _poll(lambda: reg.get(job1), lambda j: j is not None and j.phase in _TERMINAL)

    folder2 = tmp_path / "inbox" / "B"
    folder2.mkdir(parents=True)
    reg.start(str(folder2), options=_INBOX_OPTS, origin="inbox")
    assert reg.get(job1) is None  # our job was replaced in the slot

    assert q._result_for(job1) == ("failed", None)


def test_raced_handoff_wait_for_import_returns_failed_fallback(tmp_path: Path) -> None:
    # _wait_for_import polls the slot BY IDENTITY: once our job has been replaced
    # (get() returns None), it returns the raced-handoff fallback rather than
    # blocking on — or misreading — the unrelated job now in the slot.
    q, _fake, reg, _led = _make_queue(tmp_path)
    folder1 = tmp_path / "inbox" / "A"
    folder1.mkdir(parents=True)
    job1 = reg.start(str(folder1), options=_INBOX_OPTS, origin="inbox")
    _poll(lambda: reg.get(job1), lambda j: j is not None and j.phase in _TERMINAL)

    folder2 = tmp_path / "inbox" / "B"
    folder2.mkdir(parents=True)
    job2 = reg.start(str(folder2), options=_INBOX_OPTS, origin="inbox")
    # Advance the replacing job to terminal too so the assertion is deterministic
    # (no active slot to block a poll loop under any implementation).
    _poll(lambda: reg.get(job2), lambda j: j is not None and j.phase in _TERMINAL)
    assert reg.get(job1) is None

    assert q._wait_for_import(job1) == ("failed", None)


def test_stop_is_idempotent_and_unblocks_drain(tmp_path: Path) -> None:
    q, _fake, _reg, _led = _make_queue(tmp_path)
    q.start()
    q.stop()
    q.stop()  # second stop must not raise
    # A new enqueue after stop is refused (no thread to drain it): the folder is
    # never accepted into the dedupe set / FIFO.
    folder = tmp_path / "inbox" / "Album"
    folder.mkdir(parents=True)
    q.enqueue(folder)
    assert len(q._dedupe) == 0
    assert q.status().queued == 0


def _parked_album(index: int, folder: Path) -> ParkedAlbum:
    """One canned park, so the fake blocks and the stop has a worker to release."""
    album = AlbumChange(
        artist="Radiohead", album="Kid A", year=2000, label=None, country=None, media=None
    )
    return ParkedAlbum(
        album_index=index,
        folder=str(folder),
        candidate=Candidate(
            recommendation=Recommendation.medium,
            confidence=75.5,
            data_source="MusicBrainz",
            data_url="https://mb/a1",
            cover_after_url=None,
            has_current_art=False,
            changed_fields=[],
            album_before=album,
            album_after=album,
            tracks=[],
            missing=[],
            unmatched=[],
            options=[],
        ),
    )


class _HoldingRunner:
    """A runner whose worker touches no abort point: it emits one applied album,
    waits, then finishes. Models the window after the last hook, where a stop is
    accepted but has nothing left to raise at."""

    def __init__(self, release: threading.Event, folder: Path) -> None:
        self._release = release
        self._folder = folder
        self.validate_forgiven: str | None = None

    def validate(self, paths: list[str], options: ImportOptions | None = None) -> str | None:
        return None

    def run(
        self,
        paths: list[str],
        bridge: ImportBridge,
        on_finish: Callable[[], None],
        on_error: Callable[[str], None],
        options: ImportOptions | None = None,
        directive: BankApplyDirective | None = None,
    ) -> None:
        def target() -> None:
            outcome = AlbumOutcome(
                album_index=0,
                folder=str(self._folder),
                artist="Radiohead",
                album="Kid A",
                recommendation=Recommendation.strong,
                confidence=99.0,
                status=AlbumOutcomeStatus.applied,
                album_id=11,
            )
            bridge.note_outcome(outcome)
            self._release.wait(5.0)
            on_finish()

        threading.Thread(target=target, name="holding-import", daemon=True).start()


class _SlotStealingRegistry(ImportJobRegistry):
    """A registry whose ``state()`` hands the single slot to another job.

    Models a manual import (or the bank apply runner) claiming the slot in the
    instant between ``_result_for``'s two registry reads — the terminal phase
    that lets the queue read a result is the same phase that frees the slot.
    """

    def state(self, job_id: str) -> ImportJobState:
        answer = super().state(job_id)
        self._job = ImportJob(id="next-one", bridge=ImportBridge())
        return answer


def test_result_for_reads_the_abort_flag_before_the_slot_can_be_replaced(
    tmp_path: Path,
) -> None:
    """A start() between the two reads must not turn a cut-short folder into "imported".

    ``state()`` raises for a replaced slot (-> _raced_handoff) but ``job_aborted``
    answers False for it, so the forgiving read has to come first.
    """
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bridge = ImportBridge()
    bridge.request_stop()
    parked = _parked_album(0, inbox / "Kid A")
    with pytest.raises(ImportAbortError):  # the raise is what sets the abort flag
        bridge.park(parked)

    reg = _SlotStealingRegistry()
    reg._job = ImportJob(id="ours", bridge=bridge, phase=ImportPhase.done, stopped=True)
    led = AcquisitionLedger(inbox / ".musicdrop-ledger.json")
    q = AcquisitionQueue(import_registry=reg, ledger=led, poll_interval=0.01, busy_backoff=0.02)

    assert q._result_for("ours") == (
        "failed",
        "The import was stopped before this folder finished.",
    )


def test_a_stop_that_aborted_nothing_records_the_folder_as_it_landed(tmp_path: Path) -> None:
    """A stop accepted after the last abort point leaves the folder fully imported.

    ``state.stopped`` alone said "failed", which bumped the failure counter and
    wrote an error sentence for a folder every track of which is in the library.
    ``job_aborted`` is the signal that separates the two: nothing raised here.
    """
    inbox = tmp_path / "inbox"
    folder = inbox / "Radiohead - Kid A"
    folder.mkdir(parents=True)
    (folder / "01 track.flac").write_bytes(b"\0")

    release = threading.Event()
    reg = ImportJobRegistry(runner=_HoldingRunner(release, folder))
    led = AcquisitionLedger(inbox / ".musicdrop-ledger.json")
    q = AcquisitionQueue(import_registry=reg, ledger=led, poll_interval=0.01, busy_backoff=0.02)

    q.start()
    try:
        q.enqueue(folder)
        job_id = _poll(lambda: reg.active_status().job_id, lambda j: j is not None)
        assert job_id is not None
        _poll(lambda: len(reg.state(job_id).albums), lambda n: n == 1)

        reg.request_stop(job_id)
        assert reg.job_aborted(job_id) is False  # no park, no hook: nothing raised
        release.set()

        _poll(lambda: q.status().processed, lambda n: n >= 1)
        s = q.status()
        assert (s.failed, s.processed, s.set_aside) == (0, 1, 0)
        assert s.error is None
        entry = next(e for e in led.entries() if e.path == str(folder))
        assert entry.outcome == "imported"
    finally:
        release.set()
        q.stop()


def test_a_stopped_inbox_import_is_recorded_failed_not_imported(tmp_path: Path) -> None:
    """A stop is not a result: the folder is still in the inbox, every track of it.

    Classifying a stopped run by phase alone read ``done`` + nothing set aside as
    ``imported``, which retires the drop in the ledger — no webhook retry, no
    badge, and the user only notices the album is missing.
    """
    inbox = tmp_path / "inbox"
    folder = inbox / "Radiohead - Kid A"
    folder.mkdir(parents=True)
    (folder / "01 track.flac").write_bytes(b"\0")

    fake = FakeImportRunner(parked=[_parked_album(0, folder)])
    reg = ImportJobRegistry(runner=fake)
    led = AcquisitionLedger(inbox / ".musicdrop-ledger.json")
    q = AcquisitionQueue(import_registry=reg, ledger=led, poll_interval=0.01, busy_backoff=0.02)

    q.start()
    try:
        q.enqueue(folder)
        job_id = _poll(lambda: reg.active_status().job_id, lambda j: j is not None)
        assert job_id is not None
        _poll(lambda: reg.state(job_id).awaiting_decision, lambda v: v is True)

        reg.request_stop(job_id)

        _poll(lambda: q.status().processed, lambda n: n >= 1)
        s = q.status()
        # "failed" is the needs-attention bucket, the same one the raced handoff
        # uses: the run ended without saying this folder was handled.
        assert (s.failed, s.processed, s.set_aside) == (1, 1, 0)
        assert s.error == "The import was stopped before this folder finished."
        entry = next(e for e in led.entries() if e.path == str(folder))
        assert entry.outcome == "failed"

        # The inbox list annotates it rather than dropping it, so the row the
        # user re-imports by hand carries why it is there.
        items = list_inbox(inbox, led)
        assert [(i.name, i.outcome) for i in items] == [("Radiohead - Kid A", "failed")]
        # MEASURED, and narrower than "importable again": a ledger row of ANY
        # outcome blocks the automatic drain while the folder's (mtime, size) is
        # unchanged, so a webhook retry is a no-op and the re-import is the
        # user's, from the annotated row above.
        q.enqueue(folder)
        assert q._queue.qsize() == 0
    finally:
        q.stop()


def test_an_unreadable_folder_is_deferred_not_dropped_as_gone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``SourcePathMissingError`` covers two faults; one handler must not flatten them.

    A container running as a PUID that cannot search the inbox used to get the
    gone arm: the row finished terminally and the record said "is no longer
    there" for a folder sitting right where the operator left it.

    DEFER and not terminal, and the split is a property of the DURABLE RECORD
    this handler keeps, not of the fault. The bank apply runner may drop an
    EACCES row terminally because a visible, retryable bank row stays on screen.
    This queue writes no ledger row for the drop, and ``_finish`` stores a
    single ``self._error`` slot the next finish overwrites - so terminal here
    loses the download with nothing left naming it. A chmod makes the next pass
    succeed, which is the same shape as the unmounted-share arm above it.
    """
    q, fake, _reg, _led = _make_queue(tmp_path)
    folder = tmp_path / "inbox" / "Album"
    folder.mkdir(parents=True)
    fake.validate_error = unreadable_source_error(
        PermissionError(errno.EACCES, "Permission denied", str(folder))
    )
    caplog.set_level(logging.WARNING, logger="app.acquisition.queue")

    q._process_one(folder)

    # Deferred: back on the queue, and nothing was counted or finished.
    assert q._queue.qsize() == 1
    status = q.status()
    assert status.processed == 0
    assert status.failed == 0

    records = [r for r in caplog.records if r.name == "app.acquisition.queue"]
    assert len(records) == 1, records
    message = records[0].getMessage()
    assert "no longer there" not in message, message
    assert "cannot be read" in message, message
    # The refusal's own sentence rides along; it carries no path (``os_error``
    # does, and is never logged).
    assert "Permission denied" in message, message


def test_a_repeating_unreadable_deferral_logs_once_not_once_per_pass(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A deferral that retries every ``busy_backoff`` must not flood the log.

    The requeue is what keeps the download from being lost, but it means this
    arm runs again roughly once a second for as long as the permissions stay
    wrong. Logged unconditionally that is a 1 Hz flood; the folder is reported
    once and reported again only after it has started.
    """
    q, fake, _reg, _led = _make_queue(tmp_path)
    folder = tmp_path / "inbox" / "Album"
    folder.mkdir(parents=True)
    fake.validate_error = unreadable_source_error(
        PermissionError(errno.EACCES, "Permission denied", str(folder))
    )
    caplog.set_level(logging.WARNING, logger="app.acquisition.queue")

    for _ in range(4):
        q._process_one(folder)

    records = [r for r in caplog.records if r.name == "app.acquisition.queue"]
    assert len(records) == 1, [r.getMessage() for r in records]
    assert q._queue.qsize() == 4


_UNREADABLE = "That folder can't be read. Permission denied."


def _queued_once(q: AcquisitionQueue, folder: Path) -> None:
    """Put ``folder`` in the state the drain sees it in: deduped, and taken off.

    ``_process_one`` alone leaves ``_dedupe`` empty, so ``status().queued``
    would read 0 for reasons that have nothing to do with what is under test.
    """
    q.enqueue(folder)
    assert q._queue.get() == folder


def test_a_deferred_unreadable_folder_stops_claiming_an_import_is_running(
    tmp_path: Path,
) -> None:
    """A defer must release the status, and leave the fault where the page looks.

    ``_process_one`` sets ``_phase``/``_current`` and ONLY ``_finish`` used to
    clear them, so a folder deferring forever left the probe reporting
    ``current`` naming it, ``failed`` at 0 and ``error`` at None - the Review
    page rendered "Importing <folder>" behind a spinner for an import that was
    never started, while ``has_audio`` answers False on OSError so the folder
    appeared in no listing at all.

    Cleared, the same section renders "Waiting for the import slot - 1 queued",
    which is true, and ``status.error`` (RecentSection) names the fault.
    """
    q, fake, _reg, _led = _make_queue(tmp_path)
    folder = tmp_path / "inbox" / "Album"
    folder.mkdir(parents=True)
    fake.validate_error = unreadable_source_error(
        PermissionError(errno.EACCES, "Permission denied", str(folder))
    )
    _queued_once(q, folder)

    q._process_one(folder)

    s = q.status()
    assert s.current is None  # nothing is being imported
    assert s.phase == "running"  # but the queue still has work
    assert s.queued == 1
    assert s.error == _UNREADABLE
    # Not finished, so no counter moved.
    assert (s.processed, s.failed, s.set_aside) == (0, 0, 0)
    assert q._queue.qsize() == 1


def test_the_status_names_the_folder_by_BASENAME_not_by_its_server_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``current`` goes out in the status body and is rendered in two places.

    Both readers (the Review page's "Importing now" line and the activity row's
    scope) reduce it with ``lastSegment`` already, so shipping the absolute
    inbox path bought nothing and disclosed the server's layout - and a
    surrogate in a peer-chosen name would have failed the JSON encode.
    """
    q, _fake, _reg, _led = _make_queue(tmp_path)
    folder = tmp_path / "inbox" / "Album"
    folder.mkdir(parents=True)
    # Hold the gate so the folder stays "current" long enough to read.
    monkeypatch.setattr("app.lyrics_jobs.registry.lyrics_backfill_active", lambda: True)
    q.start()
    try:
        q.enqueue(folder)
        s = _poll(q.status, lambda st: st.current is not None)
        assert s.current == "Album"
        assert str(tmp_path) not in (s.current or "")
    finally:
        q.stop()


def test_the_unmounted_share_defer_releases_the_status_too(tmp_path: Path) -> None:
    """The OTHER defer arm latched ``_current`` identically.

    Two refusals share it and they are not the same event. A dropped music share
    is an outage nothing else on this page names, so it carries its sentence; a
    lost race for the single import slot is not a fault at all - another import
    genuinely is running and the page shows THAT one - so it must not put an
    error line on the page. Both release ``_current``.

    The fake raises from ``validate``; the arm does not care which side of
    ``start`` the refusal came from.
    """
    for raised, expected_error in (
        (
            LibraryRootUnavailableError("Library folder unavailable. Is the music share mounted?"),
            "Library folder unavailable. Is the music share mounted?",
        ),
        (RuntimeError("An import is already running"), None),
    ):
        q, fake, _reg, _led = _make_queue(tmp_path / str(id(raised)))
        folder = tmp_path / str(id(raised)) / "inbox" / "Album"
        folder.mkdir(parents=True)
        fake.validate_error = raised
        _queued_once(q, folder)

        q._process_one(folder)

        s = q.status()
        assert s.current is None, raised
        assert s.queued == 1, raised
        assert s.error == expected_error, raised
        assert (s.processed, s.failed) == (0, 0), raised
        assert q._queue.qsize() == 1, raised


def test_an_unreadable_folder_gives_up_once_the_defer_window_has_passed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The requeue is a growth path, because the producer is unauthenticated.

    ``/api/slskd/webhook`` is auth-exempt and ``enqueue`` never checks
    EXISTENCE, and under the very misconfiguration this arm exists for an inbox
    the app cannot search answers EACCES for names that were never there - so
    every fabricated name used to become a permanent ``_dedupe`` +
    ``_unreadable_since`` entry retrying once per ``busy_backoff`` forever.

    Giving up is not losing it: the folder is still on disk, no ledger row is
    written (so a later webhook re-enqueues it), and the reason lands in the
    ``status.error`` the Review page renders.
    """
    fake = FakeImportRunner()
    reg = ImportJobRegistry(runner=fake)
    led = AcquisitionLedger(tmp_path / "ledger.json")
    q = AcquisitionQueue(
        import_registry=reg,
        ledger=led,
        poll_interval=0.01,
        busy_backoff=0.01,
        unreadable_defer_seconds=0.05,
    )
    folder = tmp_path / "inbox" / "Album"
    folder.mkdir(parents=True)
    fake.validate_error = unreadable_source_error(
        PermissionError(errno.EACCES, "Permission denied", str(folder))
    )
    caplog.set_level(logging.WARNING, logger="app.acquisition.queue")
    _queued_once(q, folder)

    q._process_one(folder)  # inside the window: defers
    assert q._queue.qsize() == 1
    assert q.status().failed == 0
    assert q._queue.get() == folder
    time.sleep(0.05)

    q._process_one(folder)  # past it: terminal

    assert q._queue.qsize() == 0
    s = q.status()
    assert (s.processed, s.failed) == (1, 1)
    assert (s.queued, s.current, s.phase) == (0, None, "idle")
    assert s.error == _UNREADABLE
    assert q._unreadable_since == {}  # and the log-once set drained with it
    assert led.entries() == []  # no ledger row, so a re-download is offered again
    messages = [r.getMessage() for r in caplog.records if r.name == "app.acquisition.queue"]
    assert len(messages) == 2, messages
    assert "stayed unreadable" in messages[1], messages
