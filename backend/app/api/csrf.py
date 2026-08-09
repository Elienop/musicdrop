"""CSRF guard for the state-changing endpoints a browser can reach WITHOUT a
preflight.

Two shapes qualify, and both are guarded:

* a ``multipart/form-data`` POST — a CORS "simple" content type, so a
  cross-origin page can upload a cover / artist image without the strict CORS
  allowlist ever getting a say;
* a **body-less** POST (query params only, no custom headers) — equally simple,
  and the shape ``POST /api/artists/image/reset`` takes. It destroys data (it
  unlinks the user's uploaded override), so it needs this guard just as much.

(The JSON endpoints are already safe: ``application/json`` forces a preflight,
which the CORS policy rejects. So does any non-simple METHOD — the ``DELETE``
that the reset POST replaced was protected by its verb alone, which is why
swapping the verb without adding this dependency would have quietly removed a
protection.)

``verify_upload_origin`` closes that gap by checking the ``Origin`` header: a
browser always sends it on a cross-origin (and same-origin) POST, while non-browser
clients (curl, LAN tooling) send none. We allow a missing Origin, a same-origin
request (Origin authority == Host, or == X-Forwarded-Host behind a Host-rewriting
reverse proxy), and the dev frontend; anything else is 403.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

# Must stay in step with the CORS ``allow_origins`` in app.main (the Vite dev server).
_DEV_FRONTEND_ORIGIN = "http://localhost:5173"


def verify_upload_origin(request: Request) -> None:
    """Reject a cross-origin browser upload; allow same-origin, the dev frontend,
    and non-browser clients (no Origin header)."""
    origin = request.headers.get("origin")
    if origin is None:
        return  # non-browser client (curl, trusted LAN tooling) — allow
    authority = origin.split("://", 1)[-1]
    # Same-origin: the Origin authority matches the host the app sees. Behind a
    # reverse proxy that REWRITES Host to the upstream, the public host arrives in
    # X-Forwarded-Host instead, so accept a match against either header. This is
    # still safe against the CSRF vector: a browser cannot set X-Forwarded-Host on
    # a cross-origin `fetch` without making the request non-simple, which forces a
    # CORS preflight that the strict allowlist rejects.
    for header in ("host", "x-forwarded-host"):
        value = request.headers.get(header)
        if value is not None and authority == value:
            return
    if origin == _DEV_FRONTEND_ORIGIN:
        return  # the dev frontend (matches the CORS allowlist)
    raise HTTPException(status_code=403, detail="cross-origin upload rejected")
