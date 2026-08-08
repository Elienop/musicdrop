"""The shared degrade decisions: the log throttle and derive-or-degrade."""

from __future__ import annotations

import io
import logging

import pytest
from PIL import Image

from app.artwork.degrade import (
    LOG_THROTTLE_SECONDS,
    derive_thumb_or_degrade,
    reset_log_throttle,
    warn_throttled,
)


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(buf, format="PNG")
    return buf.getvalue()


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == "musicdrop.artwork"]


def test_a_repeated_condition_reports_once(caplog: pytest.LogCaptureFixture) -> None:
    """A broken cache dir is per-REQUEST by nature: with nothing persisting, a
    48-card roster paint re-ran the whole resolve for every card and emitted
    288-336 records. Unthrottled, the one fact an operator needs is buried in
    its own repetitions.
    """
    with caplog.at_level(logging.WARNING, logger="musicdrop.artwork"):
        for _ in range(50):
            warn_throttled("cache-write", "dir is unwritable: %s", "EACCES")

    assert len(_records(caplog)) == 1
    assert "EACCES" in _records(caplog)[0].getMessage()


def test_distinct_conditions_each_report(caplog: pytest.LogCaptureFixture) -> None:
    """Throttling is per CONDITION, so a second, different fault is never
    silenced by the first — that would trade spam for a blind spot."""
    with caplog.at_level(logging.WARNING, logger="musicdrop.artwork"):
        warn_throttled("cache-write", "unwritable")
        warn_throttled("cache-read", "unreadable")
        warn_throttled("cache-write", "unwritable again")

    assert [record.getMessage() for record in _records(caplog)] == ["unwritable", "unreadable"]


def test_the_condition_reports_again_after_the_interval(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Throttled, not muted: a fault that is still happening an hour later has
    to still be visible, or the log stops being evidence of the CURRENT state."""
    clock = {"now": 1000.0}
    monkeypatch.setattr("app.artwork.degrade.time.monotonic", lambda: clock["now"])
    with caplog.at_level(logging.WARNING, logger="musicdrop.artwork"):
        warn_throttled("cache-write", "first")
        clock["now"] += LOG_THROTTLE_SECONDS - 0.01
        warn_throttled("cache-write", "suppressed")
        clock["now"] += 0.02
        warn_throttled("cache-write", "second")

    assert [record.getMessage() for record in _records(caplog)] == ["first", "second"]


def test_reset_clears_the_throttle() -> None:
    """The seam the autouse fixture uses: without it, one test's warning
    suppresses the next test's assertion and the suite goes order-dependent."""
    warn_throttled("cache-write", "first")
    reset_log_throttle()
    logger = logging.getLogger("musicdrop.artwork")
    seen: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: seen.append(record.getMessage())  # type: ignore[method-assign]  # test probe
    logger.addHandler(handler)
    try:
        warn_throttled("cache-write", "again")
    finally:
        logger.removeHandler(handler)

    assert seen == ["again"]


def test_derive_returns_a_thumb_for_a_real_image() -> None:
    data, mime = derive_thumb_or_degrade(_png(1000, 1000), "image/png", subject="artist 'ABBA'")

    assert mime == "image/webp"
    assert Image.open(io.BytesIO(data)).size == (320, 320)


def test_derive_degrades_to_the_original_and_says_so(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="musicdrop.artwork"):
        data, mime = derive_thumb_or_degrade(b"not-an-image", "image/png", subject="album 7")

    assert (data, mime) == (b"not-an-image", "image/png")
    (record,) = _records(caplog)
    assert "album 7" in record.getMessage()
    # The type name is what separates "one bad file" from "WebP support is gone".
    assert "UnidentifiedImageError" in record.getMessage()


@pytest.mark.parametrize("poison", ["image/日本語", "image/png\nX-Injected: yes", "", "   "])
def test_derive_sanitises_the_originals_content_type(poison: str) -> None:
    """On the degrade path the ORIGINAL's type is what goes on the wire, so it
    clears the same bar a stored one does."""
    _data, mime = derive_thumb_or_degrade(b"not-an-image", poison, subject="artist 'X'")

    assert mime == "application/octet-stream"
