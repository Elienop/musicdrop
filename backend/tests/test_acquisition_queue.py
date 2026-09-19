"""The serial acquisition queue: FIFO + dedupe + gate-deferred drain.

The queue is the only consumer of the existing import slot for inbox drops. It
drives folders through the SAME ``ImportJobRegistry.start`` manual import uses
(Option A: it is NOT a new mutex participant — it waits for the existing gate to
clear). Tests inject a ``FakeImportRunner``-backed registry so no beets/network
is touched, and ALWAYS ``q.stop()`` in a ``finally`` so no daemon thread leaks
into teardown.
"""

from __future__ import annotations

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
    with pytest.raises(ImportAbortError):  # the raise is what sets the abort flag
        bridge.park(_parked_album(0, inbox / "Kid A"))

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
