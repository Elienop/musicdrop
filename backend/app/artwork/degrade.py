"""What this package does when something underneath it is broken.

Two pieces, both shared by every image path so the answer is the same wherever
the failure surfaces:

* :func:`warn_throttled` — one record per CONDITION per interval. The failures
  here are per-request by nature (a roster paint is 48 images, each re-running
  the whole resolve when the cache cannot persist), so an unthrottled warning is
  hundreds of lines per paint and buries the single fact an operator needs.
* :func:`derive_thumb_or_degrade` — the serve-or-degrade decision itself, in one
  place: both on-disk caches and the endpoint's uncached path make the same
  choice and emit the same record.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Final

from app.artwork.images import FALLBACK_CONTENT_TYPE, header_safe_content_type
from app.artwork.thumbs import THUMB_MIME, ThumbError, make_thumb

_log = logging.getLogger("musicdrop.artwork")

LOG_THROTTLE_SECONDS: Final = 60.0

# Keyed by CONDITION, never by entity — "the cache dir is unwritable" is one
# fact whether it stops 1 artist or 5,000, so this dict is bounded by the small
# set of string literals passed below, not by library size.
_throttle_lock = threading.Lock()
_last_logged: dict[str, float] = {}


def warn_throttled(condition: str, message: str, *args: object) -> None:
    """Log ``message`` at WARNING, at most once per ``condition`` per interval.

    WARNING rather than INFO for the reason the degrade log picked it: nothing
    in this app configures the root logger, so under uvicorn's default config
    anything below WARNING is dropped entirely and the line would not exist.
    """
    now = time.monotonic()
    with _throttle_lock:
        last = _last_logged.get(condition)
        if last is not None and now - last < LOG_THROTTLE_SECONDS:
            return
        _last_logged[condition] = now
    _log.warning(message, *args)


def reset_log_throttle() -> None:
    """Forget every throttled condition.

    A test seam: the throttle state is module-global, so without a reset between
    tests one test's warning silently suppresses the next one's assertion and
    the suite becomes order-dependent. Wired as an autouse fixture in conftest.
    """
    with _throttle_lock:
        _last_logged.clear()


def derive_thumb_or_degrade(data: bytes, content_type: str, *, subject: str) -> tuple[bytes, str]:
    """A thumbnail of ``data``, or ``data`` itself when none can be derived.

    Degrading is right — a grid that shows SOME image beats a 500 — but it must
    not be silent. Before ``make_thumb``'s guard was widened to
    ``except Exception``, a SYSTEMIC encoder failure (Pillow built without WebP,
    a broken format plugin) surfaced as a 500: loud and diagnosable. Unlogged it
    serves every image full-size forever, which looks exactly like the thumb
    feature never having been deployed.

    ``subject`` names what degraded (``"artist 'ABBA'"``, ``"album 7"``) and the
    ``ThumbError`` carries the underlying exception's type name, which is what
    separates "one bad file" from "WebP support is gone".

    The returned content-type clears the same bar a stored one does: on the
    degrade path the ORIGINAL's type goes on the wire, and an unsendable value
    there is a 500 (non-ASCII) or a dropped connection (embedded newline).
    """
    try:
        return make_thumb(data), THUMB_MIME
    except ThumbError as exc:
        warn_throttled(
            "thumb-degrade",
            "thumbnail degraded to the original for %s: %s",
            subject,
            exc,
        )
        # The RETURN value, not the input: an original whose type only
        # needed trimming is served trimmed, and one that cannot be a
        # header at all takes the generic type.
        return data, header_safe_content_type(content_type) or FALLBACK_CONTENT_TYPE
