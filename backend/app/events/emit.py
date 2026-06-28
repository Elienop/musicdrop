"""Null-safe shortcut to publish a library-changed event from a request handler
or a background job. The broker is absent in tests that skip the lifespan, so
emission is simply a no-op there."""

from __future__ import annotations

from typing import Any


def emit_library_changed(app: Any) -> None:
    broker = getattr(app.state, "event_broker", None)
    if broker is not None:
        broker.publish_library_changed()


def emit_art_changed(app: Any) -> None:
    """Null-safe ``art:changed`` emit — image bytes changed (cover/artist art),
    so tabs remount their ``<img>`` elements without refetching list data."""
    broker = getattr(app.state, "event_broker", None)
    if broker is not None:
        broker.publish_art_changed()
