"""Direct tests for the extracted import-gate union (app/import_jobs/gates.py).

The behavioral coverage through the acquisition queue (defer on backfill/swap
lock) stays in test_acquisition_queue.py; these pin the helper's own contract
so the bank apply runner can rely on it without the queue.
"""

import asyncio

import pytest

from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.gates import import_gate_clear
from app.import_jobs.registry import ImportJobRegistry


def test_gate_clear_when_everything_is_idle() -> None:
    reg = ImportJobRegistry(runner=FakeImportRunner())
    assert import_gate_clear(reg, None) is True


def test_gate_blocked_by_the_import_slot() -> None:
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry(runner=FakeImportRunner())
    reg._job = ImportJob(id="j1", bridge=ImportBridge())  # phase scanning = active
    assert import_gate_clear(reg, None) is False


def test_gate_blocked_by_swap_lock_then_clears() -> None:
    reg = ImportJobRegistry(runner=FakeImportRunner())
    lock = asyncio.Lock()

    async def while_held() -> bool:
        async with lock:
            return import_gate_clear(reg, lock)

    assert asyncio.run(while_held()) is False
    assert import_gate_clear(reg, lock) is True


@pytest.mark.parametrize(
    "target",
    [
        "app.lyrics_jobs.registry.lyrics_backfill_active",
        "app.artist_art_jobs.registry.artist_art_backfill_active",
        "app.reorganize_jobs.registry.reorganize_backfill_active",
    ],
)
def test_gate_blocked_by_each_backfill(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    reg = ImportJobRegistry(runner=FakeImportRunner())
    monkeypatch.setattr(target, lambda: True)
    assert import_gate_clear(reg, None) is False
