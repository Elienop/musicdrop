from __future__ import annotations

from app.beets.import_session import ImportBridge
from app.import_jobs.registry import ImportJob, ImportJobRegistry
from app.models.import_api import ImportPhase


class _FakeBroker:
    def __init__(self) -> None:
        self.count = 0

    def publish_library_changed(self) -> None:
        self.count += 1


def test_on_finish_emits_library_changed_once() -> None:
    reg = ImportJobRegistry()
    broker = _FakeBroker()
    reg.attach_event_broker(broker)
    reg._job = ImportJob(id="abc", bridge=ImportBridge())  # drive the finish callback
    reg._on_finish("abc")
    assert reg._job.phase is ImportPhase.done
    assert broker.count == 1


def test_on_finish_without_broker_does_not_raise() -> None:
    reg = ImportJobRegistry()
    reg._job = ImportJob(id="abc", bridge=ImportBridge())
    reg._on_finish("abc")  # no broker attached; must be a quiet no-op
    assert reg._job.phase is ImportPhase.done
