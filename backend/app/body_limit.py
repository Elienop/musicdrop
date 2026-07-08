"""ASGI middleware that bounds request-body size (a DoS guard).

Refuses a request whose declared ``Content-Length`` exceeds ``max_bytes`` with a
413 before the body is read, so the JSON/YAML/m3u parse endpoints (config save,
playlist import) and the image uploads can't be handed an unbounded body. Clients
in this single-user deployment always send ``Content-Length``; a reverse proxy's
own ``client_max_body_size`` is the belt-and-suspenders for the chunked case.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

# Kept in step with the CORS ``allow_origins`` in app.main and the csrf dev origin
# (the Vite dev server): the only cross-origin caller we echo CORS headers to.
_DEV_FRONTEND_ORIGIN = b"http://localhost:5173"


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self._app = app
        self._max = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = scope.get("headers", [])
            for name, value in headers:
                if name == b"content-length":
                    try:
                        too_big = int(value) > self._max
                    except ValueError:
                        too_big = False
                    if too_big:
                        await self._reject(send, _allowed_origin(headers))
                        return
                    break
        await self._app(scope, receive, send)

    async def _reject(self, send: Send, allow_origin: bytes | None) -> None:
        # This middleware wraps OUTERMOST, so on rejection the inner CORSMiddleware
        # never runs. Echo the CORS headers ourselves for an allowed origin, else a
        # cross-origin caller (the dev frontend) gets an opaque CORS error instead
        # of the JSON reason.
        headers = [(b"content-type", b"application/json")]
        if allow_origin is not None:
            headers.append((b"access-control-allow-origin", allow_origin))
            headers.append((b"access-control-allow-credentials", b"true"))
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": b'{"detail":"Request body too large"}'})


def _allowed_origin(headers: list[tuple[bytes, bytes]]) -> bytes | None:
    """The request Origin iff it is an allowed cross-origin caller, else None."""
    for name, value in headers:
        if name == b"origin":
            return value if value == _DEV_FRONTEND_ORIGIN else None
    return None
