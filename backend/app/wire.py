"""Wire safety for strings that come off the filesystem.

POSIX filenames are bytes, so a name that is not valid UTF-8 survives
``os.fsdecode`` only as lone surrogates (the ``surrogateescape`` handler maps
each undecodable byte to U+DC80..U+DCFF). JSON is encoded UTF-8 *strictly*, so
one such string anywhere in a response body raises and the endpoint 500s: a
single directory named ``b"Caf\\xe9 Album"`` used to take down every reorganize
endpoint and the whole Trash page.

The invariant this module holds: **no JSON endpoint may fail because a path is
not valid UTF-8.** Valid text of any script passes through byte-for-byte; only
genuinely undecodable bytes become the U+FFFD placeholder.

It is applied on the way OUT, at the three sinks a response can leave by
(:class:`SurrogateSafeJSONResponse` for bodies, plus the ``HTTPException`` and
``RequestValidationError`` handlers, which FastAPI renders with a plain
``JSONResponse`` this module does not own), rather than at each of the three
dozen places a path reaches a model. That placement is load-bearing, not just
tidier:

* Server-side values keep their exact bytes. Several of these strings are
  locators, not labels — a bank row's ``folder`` is re-read to start the import,
  and it round-trips through the same Pydantic model that serves ``GET
  /api/bank``. Scrubbing at the model would corrupt the locator.
* A new field that ships a path cannot reintroduce the bug.

The cost is that FastAPI takes the fast path (Pydantic's Rust ``dump_json``)
only when a route leaves its response class at the default, so naming one here
routes every response through ``dump_python`` + ``json.dumps`` instead. Measured
at 0.06 ms -> 0.17 ms on the largest realistic page (192 albums, 30 KB) — noise
next to the SQLite and beets work behind these endpoints.

:func:`resolve_display_path` is the inverse, for the two names a client sends
straight back (the Trash folder and the inbox item): it maps a scrubbed name
onto the real on-disk entry, and refuses rather than guesses when two entries
scrub alike.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

_log = logging.getLogger(__name__)

# What undecodable bytes look like once they reach the wire. UTF-8's "replace"
# emits ONE of these per maximal invalid SEQUENCE, not per byte, so two
# differently-damaged names can land on the same display form — which is exactly
# why ``resolve_display_path`` refuses to guess between them.
PLACEHOLDER = "�"

StrOrBytesPath = str | bytes | os.PathLike[str] | os.PathLike[bytes]


def wire_safe(text: str) -> str:
    """``text`` with anything UTF-8 cannot carry replaced by :data:`PLACEHOLDER`.

    Puts the original bytes back (undoing ``surrogateescape``) and re-decodes
    them replacing only what is genuinely undecodable, so accented Latin, CJK,
    emoji and every other valid string come back untouched. Never raises.
    """
    if text.isascii():  # no surrogate can hide in ASCII — the overwhelming case
        return text
    try:
        raw = text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        # A surrogate outside surrogateescape's U+DC80..U+DCFF range, so it did
        # not come from a filename. surrogatepass encodes anything, and the
        # bytes it produces are invalid UTF-8, so the decode below replaces them.
        raw = text.encode("utf-8", "surrogatepass")
    return raw.decode("utf-8", "replace")


def display_path(path: StrOrBytesPath) -> str:
    """A filesystem path as a string safe to put on the wire."""
    return wire_safe(os.fsdecode(path))


def scrub_content(value: Any) -> Any:
    """``value`` with every string inside it run through :func:`wire_safe`."""
    if isinstance(value, str):
        return wire_safe(value)
    if isinstance(value, dict):
        return {scrub_content(k): scrub_content(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [scrub_content(item) for item in value]
    return value


class SurrogateSafeJSONResponse(JSONResponse):
    """A JSON body that degrades an undecodable path instead of 500ing on it.

    The strict encode is attempted unchanged first, so an ordinary response is
    byte-identical to Starlette's and pays nothing; only a failure walks the
    payload.
    """

    def render(self, content: Any) -> bytes:
        try:
            return super().render(content)
        except UnicodeEncodeError as exc:
            # Degrading must not be silent. The only string this is EXPECTED to
            # catch is a path we could not decode; anything else reaching here is
            # a genuine bug that would otherwise be masked by the placeholder.
            _log.warning("response body carried non-encodable text, replacing it: %s", exc)
            return super().render(scrub_content(content))


async def surrogate_safe_http_exception_handler(request: Request, exc: Exception) -> Response:
    """FastAPI's own ``HTTPException`` handler, with the detail scrubbed first.

    The failure path carries paths as readily as the success path: several 4xx/500
    details interpolate an ``OSError`` whose message embeds the file it could not
    touch, and that string is rendered by a plain ``JSONResponse`` this class
    does not own.
    """
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    safe = StarletteHTTPException(
        status_code=exc.status_code,
        detail=scrub_content(exc.detail),
        headers=exc.headers,
    )
    return await http_exception_handler(request, safe)


async def surrogate_safe_validation_exception_handler(request: Request, exc: Exception) -> Response:
    """FastAPI's own 422 handler, with the echoed input scrubbed first.

    A 422 body echoes the value that failed validation, and a client can put a
    lone surrogate there without sending a single non-ASCII byte: ``json.loads``
    accepts the escape ``"\\udce9"``. That echo is rendered by a plain
    ``JSONResponse``, so it needs the same treatment as an ``HTTPException``.
    """
    if not isinstance(exc, RequestValidationError):
        raise exc
    safe = RequestValidationError(
        scrub_content(jsonable_encoder(exc.errors())),
        body=exc.body,
        endpoint_ctx=exc.endpoint_ctx,
    )
    return await request_validation_exception_handler(request, safe)


def install_wire_safety(app: FastAPI) -> None:
    """Route the two error paths FastAPI renders itself through the scrubber.

    Response bodies are covered by ``default_response_class`` at app
    construction; these close the ``HTTPException`` detail and the 422
    validation echo, both of which use a plain ``JSONResponse``.
    """
    app.add_exception_handler(StarletteHTTPException, surrogate_safe_http_exception_handler)
    app.add_exception_handler(RequestValidationError, surrogate_safe_validation_exception_handler)


class AmbiguousDisplayName(Exception):
    """Two on-disk entries share one display form, so a name cannot be resolved.

    Deliberately NOT a ``ValueError``: callers already map that to "no such
    entry", and quietly picking one of two real folders to delete or import
    would be far worse than refusing.
    """

    def __init__(self, display_name: str) -> None:
        super().__init__(f"{display_name!r} matches more than one entry")
        self.display_name = display_name


def resolve_display_path(base: Path, rel: str) -> Path:
    """Map a display-form relative path back onto the real names under ``base``.

    A component carrying the placeholder is resolved by SCANNING its parent and
    matching on display form — never by trusting ``base / rel``. U+FFFD is a
    legal filename character, so the literal path can exist and still be the
    wrong target while a damaged sibling displays identically; preferring it
    would silently delete or import the other folder. ``wire_safe`` is the
    identity on valid UTF-8, so a genuinely-placeholder-named entry matches
    itself in that scan and stays reachable when it is the only candidate.

    Placeholder-free components keep the literal path: scrubbing only replaces
    undecodable bytes, so a result without a placeholder means the bytes were
    untouched — no second candidate can exist, and the scan would be waste.

    Containment is deliberately NOT checked here: every caller already has its
    own traversal guard, and this must not become a second, weaker one.
    """
    if PLACEHOLDER not in rel:
        return base / rel
    resolved = base
    for part in Path(rel).parts:
        resolved = _match_display_child(resolved, part) if PLACEHOLDER in part else resolved / part
    return resolved


def _match_display_child(parent: Path, display_name: str) -> Path:
    """The child of ``parent`` whose display form is ``display_name``."""
    try:
        with os.scandir(parent) as entries:
            matches = [entry.path for entry in entries if wire_safe(entry.name) == display_name]
    except (OSError, ValueError):
        # ValueError, not just OSError: os.scandir raises it for an embedded NUL,
        # which a client can put in the name. Let the caller's own guard answer.
        return parent / display_name
    if len(matches) > 1:
        raise AmbiguousDisplayName(display_name)
    if not matches:
        return parent / display_name
    return Path(matches[0])
