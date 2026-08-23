"""ASGI middleware that rejects cross-origin browser WRITES (the CSRF guard).

Every state-changing method (POST/PUT/PATCH/DELETE) is checked against the
Origin header before it reaches the router; GET/HEAD — including the
``/api/events`` SSE stream — pass through untouched, and CORS preflight
OPTIONS never arrive here (CORSMiddleware wraps this middleware and answers
them itself). The policy predicate lives in ``app.api.csrf.origin_allowed``;
this module owns the wire behavior and the dev/prod resolution.

This is a browser-CSRF guard, NOT auth: a request without an Origin header
(curl, LAN tooling, the container healthcheck, the slskd webhook) is allowed.

Runs INSIDE CORSMiddleware (added before it in ``app.main``; Starlette applies
middleware in reverse add order), so a rejected dev-origin request still gets
CORS headers on its 403 and the dev frontend can read the reason.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.csrf import origin_allowed

# The Vite dev server. The ONLY place this literal is written; every consumer
# (this guard, the CORS allowlist, the body-limit 413 echo) reads the tuple
# resolve_extra_origins() returns.
_DEV_FRONTEND_ORIGIN = "http://localhost:5173"

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def resolve_extra_origins(static_dir: str) -> tuple[str, ...]:
    """The non-same-origin origins allowed to write, resolved once at app build.

    Dev (``static_dir`` empty — the SPA comes from Vite on :5173 and is proxied
    to the API) allows the Vite origin. Production single-image mode
    (``static_dir`` set — SPA and API share one origin) allows none: an owner
    decision, 2026-08-23 (see the spec).
    """
    return () if static_dir else (_DEV_FRONTEND_ORIGIN,)


class OriginGuardMiddleware:
    def __init__(self, app: ASGIApp, *, extra_origins: tuple[str, ...]) -> None:
        self._app = app
        self._extra = extra_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in _UNSAFE_METHODS:
            headers = Headers(scope=scope)
            if not origin_allowed(
                headers.get("origin"),
                host=headers.get("host"),
                forwarded_host=headers.get("x-forwarded-host"),
                extra_origins=self._extra,
            ):
                await _reject(send)
                return
        await self._app(scope, receive, send)


async def _reject(send: Send) -> None:
    # Same status, shape, and wording the per-route guard produced, so client
    # handling and the existing test matrix carry over unchanged.
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": b'{"detail":"cross-origin request rejected"}',
        }
    )
