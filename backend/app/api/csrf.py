"""The origin policy behind the app-wide CSRF guard.

Browsers send CORS-"simple" writes (body-less, query-only, and multipart
POSTs) WITHOUT a preflight, so the strict CORS allowlist never gets a vote on
them, and JSON routes are only protected as long as every client really sends
``Content-Type: application/json``. Rather than reason per-route about which
shapes preflight, ``app.origin_guard.OriginGuardMiddleware`` applies the
predicate below to EVERY state-changing method, app-wide — a new route is
protected the day it is written, and converting a DELETE to a body-less POST
no longer silently sheds a protection.

The predicate is a browser-CSRF guard, not auth: non-browser clients (curl,
LAN tooling, the container healthcheck, the slskd webhook) send no Origin
header and are allowed through unchanged.
"""

from __future__ import annotations


def origin_allowed(
    origin: str | None,
    *,
    host: str | None,
    forwarded_host: str | None,
    extra_origins: tuple[str, ...],
) -> bool:
    """The one origin policy, as a pure predicate (used by the app-wide
    origin-guard middleware).

    Allowed: a missing Origin (non-browser client — curl, LAN tooling, the
    container healthcheck, the slskd webhook); a same-origin request (Origin
    authority equals Host, or X-Forwarded-Host behind a Host-rewriting
    reverse proxy — scheme is deliberately ignored); an explicitly allowed
    extra origin (the Vite dev server, dev mode only). Everything else —
    including ``Origin: null`` — is a cross-origin browser write: rejected.
    """
    if origin is None:
        return True
    authority = origin.split("://", 1)[-1]
    for value in (host, forwarded_host):
        if value is not None and authority == value:
            return True
    return origin in extra_origins
