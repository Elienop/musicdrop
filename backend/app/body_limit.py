"""ASGI middleware that bounds request-body size (a DoS guard).

Refuses a request whose declared ``Content-Length`` exceeds ``max_bytes`` with a
413 before the body is read, so the JSON/YAML/m3u parse endpoints (config save,
playlist import) and the image uploads can't be handed an unbounded body. Clients
in this single-user deployment always send ``Content-Length``; a reverse proxy's
own ``client_max_body_size`` is the belt-and-suspenders for the chunked case.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self._app = app
        self._max = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            for name, value in scope.get("headers", []):
                if name == b"content-length":
                    try:
                        too_big = int(value) > self._max
                    except ValueError:
                        too_big = False
                    if too_big:
                        await self._reject(send)
                        return
                    break
        await self._app(scope, receive, send)

    async def _reject(self, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"detail":"Request body too large"}'})
