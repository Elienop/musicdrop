"""Null-safe shortcut to publish a library-changed event from a request handler
or a background job. The broker is absent in tests that skip the lifespan, so
emission is simply a no-op there."""

from __future__ import annotations

from typing import Any


def emit_library_changed(app: Any) -> None:
    broker = getattr(app.state, "event_broker", None)
    if broker is not None:
        broker.publish_library_changed()
