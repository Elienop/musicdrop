"""Null-safe shortcut to publish a library-changed event from a request handler
or a background job. The broker is absent in tests that skip the lifespan, so
emission is simply a no-op there."""

from __future__ import annotations

from typing import Any

from app.beets.browse import invalidate_browse_cache


def emit_library_changed(app: Any) -> None:
    # Invalidate BEFORE the broker null-check: lifespan-less tests have no
    # broker but still mutate the library through paths that call this helper.
    invalidate_browse_cache()
    broker = getattr(app.state, "event_broker", None)
    if broker is not None:
        broker.publish_library_changed()


def emit_art_changed(app: Any, scope: str | None = None) -> None:
    """Null-safe ``art:changed`` emit — image bytes changed (cover/artist art),
    so tabs remount their ``<img>`` elements without refetching list data.

    Pass ``scope`` (``"album:12"`` / ``"artist:ABBA"``) when exactly one asset
    changed so tabs remount only that image; omit it for sweeps that touch many.
    """
    broker = getattr(app.state, "event_broker", None)
    if broker is not None:
        broker.publish_art_changed(scope)
