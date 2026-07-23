from __future__ import annotations

from types import SimpleNamespace

from app.events.emit import emit_art_changed, emit_library_changed


class _FakeBroker:
    def __init__(self) -> None:
        self.count = 0
        self.art_scopes: list[str | None] = []

    def publish_library_changed(self) -> None:
        self.count += 1

    def publish_art_changed(self, scope: str | None = None) -> None:
        self.art_scopes.append(scope)


def test_emits_when_broker_present() -> None:
    broker = _FakeBroker()
    app = SimpleNamespace(state=SimpleNamespace(event_broker=broker))
    emit_library_changed(app)
    assert broker.count == 1


def test_noop_when_broker_absent() -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    emit_library_changed(app)  # must not raise


def test_emit_art_changed_forwards_the_scope() -> None:
    broker = _FakeBroker()
    app = SimpleNamespace(state=SimpleNamespace(event_broker=broker))
    emit_art_changed(app, "album:7")
    assert broker.art_scopes == ["album:7"]


def test_emit_art_changed_defaults_to_unscoped() -> None:
    broker = _FakeBroker()
    app = SimpleNamespace(state=SimpleNamespace(event_broker=broker))
    emit_art_changed(app)
    assert broker.art_scopes == [None]


def test_emit_art_changed_noop_when_broker_absent() -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    emit_art_changed(app, "album:7")  # must not raise
