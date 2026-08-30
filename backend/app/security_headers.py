"""ASGI middleware that stamps the browser-hardening response headers.

The four guards next door refuse traffic; this one bounds what a browser is
allowed to DO with a response it already has. Five headers on every response:

- ``X-Content-Type-Options: nosniff`` — a JSON body or an uploaded image can
  never be re-read as script because a browser guessed a different type.
- ``X-Frame-Options: DENY`` (and ``frame-ancestors 'none'``) — the UI is not
  framable, so clickjacking a destructive action (delete, reorganize, apply)
  has nothing to overlay.
- ``Referrer-Policy: same-origin`` — library paths, artist names and search
  terms live in this app's URLs; they must not leak in the ``Referer`` of the
  cover-art and portrait requests the pages make to third parties.
- ``Cross-Origin-Resource-Policy: same-origin`` — with no authentication, a
  foreign page could probe the library through no-CORS subresource loads
  (``<img src=".../api/artists/image?name=X">`` 404s when absent, so onerror
  is an existence oracle); browsers now refuse to deliver those loads. Safe in
  dev too: the Vite proxy serves ``/api`` same-origin from the browser's view.
- ``Content-Security-Policy`` — the injection backstop. Tag values, filenames
  and playlist names come from disk and from the network, so the app renders
  strings it did not author; the policy says none of them can become script.

The strict policy is the default and is deliberately byte-stable so the tests
can assert it whole. Two of its allowances are worth the note: ``style-src``
needs ``'unsafe-inline'`` because shadcn/Radix position their popovers by
writing style ATTRIBUTES, and ``img-src`` is wide because the artist-image and
cover panels preview arbitrary user-pasted image URLs plus ``blob:`` object
URLs minted from local file picks. Neither loosens script execution, which is
the property that matters here.

FastAPI's own docs pages are the one exception: Swagger UI and ReDoc load from
a CDN and run an inline init script, so ``/docs``, ``/docs/oauth2-redirect``
and ``/redoc`` get a policy relaxed to exactly those origins — matched
EXACTLY, never by prefix, so ``/openapi.json`` and any ``/docsx`` lookalike
stay strict. The hardening directives (``frame-ancestors``, ``base-uri``,
``form-action``, ``object-src``) are identical in both.

Added after the four guards in ``app.main`` so it wraps OUTSIDE ALL OF THEM
(Starlette applies middleware in reverse add order). That position is
load-bearing rather than cosmetic: the host guard's 400, the origin guard's
403, the body limit's 413 and the session gate's 401 are written straight to
the transport and never reach the router, so only a wrapper outside all four
can stamp them. CORS is
added last — OUTSIDE this stamper — to satisfy SonarQube's ``python:S8414``,
which requires CORSMiddleware to sit outermost. It changes nothing else — it
does not read the body, buffer, reorder or reject; it edits the
``http.response.start`` message in flight and passes everything else through
untouched.

Known gaps, all three benign:
Starlette's ``ServerErrorMiddleware`` sits outside ALL user middleware, so the
bare 500 it synthesises for an unhandled exception never reaches these headers
— and registering a global ``Exception``/500 handler would PROMOTE that
response to the same unstamped outer path, so don't. Likewise uvicorn answers
a protocol-level malformed request (``Invalid HTTP request received.``) before
the ASGI app runs at all. Both of those are fixed text/plain bodies with no
attacker content. The third: ``CORSMiddleware``, sitting outside this stamper,
answers preflight OPTIONS itself, so those responses never reach it — but the
five headers are inert on a bodiless OPTIONS anyway: nothing renders, nothing
is framed or embedded, and there is no body to sniff.
"""

from __future__ import annotations

from collections.abc import Iterable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

STRICT_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' blob: data: https: http:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'"
)

# Swagger UI pulls its bundle + stylesheet from jsdelivr, runs an inline init
# script and loads its favicon from fastapi.tiangolo.com; ReDoc pulls its
# bundle from jsdelivr, a stylesheet from Google Fonts with the font files from
# fonts.gstatic.com, has an inline <style>, renders in a blob: web worker, and
# shows its default logo from cdn.redoc.ly (not in the HTML — the bundle adds
# it). Verified against fastapi/openapi/docs.py (the version pinned in .venv)
# and against both pages rendering violation-free in a real browser.
DOCS_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
    "font-src 'self' https://fonts.gstatic.com; "
    "connect-src 'self'; "
    "worker-src blob:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'"
)

# FastAPI's three built-in docs paths, as configured on the app in app.main
# (the defaults). Pinned against the real ``docs_url``/``redoc_url``/
# ``swagger_ui_oauth2_redirect_url`` by test_security_headers.py.
DOCS_PATHS = ("/docs", "/docs/oauth2-redirect", "/redoc")

_STATIC_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
)

# Dropped from a response before ours are appended, so a value can never appear
# twice (a duplicated CSP is intersected by browsers — the strictest wins — and
# a duplicated X-Frame-Options is ignored outright by some). Replace-not-merge
# is deliberate and cuts BOTH ways: a route cannot weaken these headers, and a
# route wanting a STRICTER per-route policy must change this middleware rather
# than set its own — an inner value would silently vanish here.
_MANAGED = frozenset({name for name, _ in _STATIC_HEADERS} | {b"content-security-policy"})


def csp_for_path(path: str) -> str:
    """The policy for one request path: relaxed on the docs pages, else strict.

    Exact match against ``scope["path"]`` — the same percent-DECODED string the
    router matches on (uvicorn decodes before either sees it), so the predicate
    and the routing decision cannot disagree: ``/%64ocs`` serves Swagger AND
    gets the relaxed policy, while ``/docsx``, ``/docs/``, ``//docs`` and case
    variants all route elsewhere and stay strict. A prefix compare would hand
    the CDN-and-inline-script policy to every route sharing the prefix. One
    divergence exists: behind a path-prefixing proxy (uvicorn ``--root-path``,
    which nothing here sets) the scope path arrives PREFIXED while the router
    strips the prefix — the docs pages would then fail CLOSED to the strict
    policy (blank page, never a loosened one).

    ``app/auth/gate.py`` faces the SAME divergence and cannot resolve it this
    way, because failing closed there means refusing traffic rather than
    over-restricting it: a gate that read only the raw path let
    ``/musicdrop/openapi.json`` through anonymously under ``--root-path``. It
    strips the prefix explicitly and checks both forms. This function stays as
    it is on purpose — a prefixed docs page losing its CDN allowances is a
    cosmetic failure, and adding the same stripping here would be untested
    behaviour bought for nothing.
    """
    return DOCS_CSP if path in DOCS_PATHS else STRICT_CSP


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        csp = csp_for_path(scope.get("path", "")).encode("ascii")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                # A shallow copy, not an in-place edit: the sender still owns
                # the message it handed us.
                message = {**message, "headers": _stamp(message.get("headers", []), csp)}
            await send(message)

        await self._app(scope, receive, send_with_headers)


def _stamp(headers: Iterable[tuple[bytes, bytes]], csp: bytes) -> list[tuple[bytes, bytes]]:
    stamped = [(name, value) for name, value in headers if name.lower() not in _MANAGED]
    stamped.extend(_STATIC_HEADERS)
    stamped.append((b"content-security-policy", csp))
    return stamped
