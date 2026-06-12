"""The serial acquisition queue: FIFO + dedupe + gate-deferred drain.

The queue is the only consumer of the existing import slot for inbox drops. It
drives folders through the SAME ``ImportJobRegistry.start`` manual import uses
(Option A: it is NOT a new mutex participant — it waits for the existing gate to
clear). Tests inject a ``FakeImportRunner``-backed registry so no beets/network
is touched, and ALWAYS ``q.stop()`` in a ``finally`` so no daemon thread leaks
into teardown.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import pytest

from app.acquisition.ledger import AcquisitionLedger
from app.acquisition.queue import AcquisitionQueue
from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import ImportJobRegistry
from app.models.bank import BankApplyDirective
from app.models.import_models import ImportOptions, ImportOrigin

T = TypeVar("T")


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

    calls: list[tuple[str, ImportOptions | None, ImportOrigin]] = []
    real_start = reg.start

    def spy_start(
        path: str,
        *,
        options: ImportOptions | None = None,
        origin: ImportOrigin = "manual",
        directive: BankApplyDirective | None = None,
    ) -> str:
        calls.append((path, options, origin))
        return real_start(path, options=options, origin=origin, directive=directive)

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
