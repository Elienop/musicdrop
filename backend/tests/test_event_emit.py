from __future__ import annotations

from types import SimpleNamespace

from app.events.emit import emit_library_changed


class _FakeBroker:
    def __init__(self) -> None:
        self.count = 0

    def publish_library_changed(self) -> None:
        self.count += 1


def test_emits_when_broker_present() -> None:
    broker = _FakeBroker()
    app = SimpleNamespace(state=SimpleNamespace(event_broker=broker))
    emit_library_changed(app)
    assert broker.count == 1


def test_noop_when_broker_absent() -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    emit_library_changed(app)  # must not raise
