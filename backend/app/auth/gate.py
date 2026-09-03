"""ASGI middleware that refuses API traffic without a valid session cookie.

The guards next door are not authentication: the host guard bounds which
*names* may reach the app, the origin guard bounds which *pages* may write, and
both deliberately let a credential-less curl through. This one is the
authentication, and it is the reason the API is no longer readable by anyone
who can reach the port.

**What is gated:** every path under ``/api/``, plus FastAPI's doc surface
(``/docs``, ``/docs/oauth2-redirect``, ``/redoc``, ``/openapi.json`` — the last
of which is the whole API contract). **What is not:** everything else, so the
built SPA shell and its assets still load for a browser with no cookie. They
have to: the login screen is part of that bundle, and a shell that 401s has no
way to ask for the password.

Five exact paths are exempt (:data:`EXEMPT_PATHS`), each for a caller that
cannot hold a cookie or must not be double-gated:

* ``/api/health`` — the image's HEALTHCHECK calls it with bare ``urllib``;
* ``/api/slskd/webhook`` — machine-to-machine, already fail-closed behind its
  own shared secret;
* ``/api/auth/login`` and ``/api/auth/status`` — the way in, and the question
  the login screen asks before rendering;
* ``/api/auth/setup`` — first-run password setup, which by definition runs
  before any credential exists. It is exempt STATICALLY rather than only while
  unconfigured, because :func:`path_requires_session` is a pure function of the
  path and the OpenAPI overlay asks it the same question at build time: an
  exemption that depended on request state could not be expressed in the
  contract. The route itself refuses with 409 the moment any source exists, so
  the exemption is a way IN on first run and nothing else.

The match is an EXACT string compare, and it is made against BOTH the raw
``scope["path"]`` and the ROUTE path (that same string with any ASGI
``root_path`` prefix removed). Exactness is the anti-traversal invariant: a
path that string-equals an exempt path routes to that exempt route and nothing
else, whereas a prefix or substring compare would hand ``/api/health/../config``
(and every lookalike) a free pass to a different handler.

Checking both paths is the fix for a measured fail-open. Starlette's router
matches on the ROOT-STRIPPED path, so under ``uvicorn --root-path /musicdrop``
a request arrives with ``scope["path"] == "/musicdrop/openapi.json"`` and is
routed to ``/openapi.json``. A gate that consulted only the raw path saw a
string not starting with ``/api/``, passed it through, and the router served
the entire API contract to an anonymous caller — 200 with a 309,375-byte body,
reproduced against this app before the fix. ``path_requires_session`` is
therefore asked about the route path (the correct question — that is what picks
the handler) OR the raw path (a fail-closed backstop, in case
:func:`_route_path` ever diverges from Starlette's own stripping).

The one cost of that OR: a deployment whose ``root_path`` is ITSELF ``/api``-
shaped over-gates, because a static asset at ``/api/assets/x.js`` has a raw path
matching the API prefix while its route path does not. Erring closed is the
deliberate choice here — the failure is "asked to log in for a stylesheet",
not "library served to a stranger" — and no deployment mounts this app under
``/api``.

Added FIRST in ``app.main``, which makes it the INNERMOST middleware — the
last thing before the router. That position is deliberate: the security-headers
stamper wraps outside it, so a 401 is stamped like every other rejection; CORS
still answers preflights outermost; and the host guard's 400, the body limit's
413 and the origin guard's 403 all keep winning over it, so their precedence
pins are untouched. The cheapest guard fires first, and a request that never
should have reached this host is refused without spending an HMAC on it.
"""

from __future__ import annotations

from typing import Final

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.auth.passwords import password_is_configured
from app.auth.session import SESSION_COOKIE_NAME, session_token_is_valid
from app.auth.source import PasswordSource, effective_password, password_file_location
from app.security_headers import DOCS_PATHS

API_PREFIX: Final = "/api/"

#: FastAPI's built-in documentation surface. ``DOCS_PATHS`` is reused rather
#: than re-typed — it is already the set of "FastAPI's own docs pages", already
#: pinned against the live ``docs_url``/``redoc_url``/``swagger_ui_oauth2_redirect_url``
#: by test_security_headers.py, so the two consumers cannot drift apart.
#: ``/openapi.json`` is added here: it is not a CSP concern, but it IS the
#: entire API contract and must not be readable anonymously.
DOC_SURFACE_PATHS: Final = frozenset({*DOCS_PATHS, "/openapi.json"})

#: Exact paths that stay reachable without a session. Shared with
#: ``app/openapi_overlay.py`` through :func:`path_requires_session`, so the
#: contract's 401 declarations cannot drift from what the gate actually does.
EXEMPT_PATHS: Final = frozenset(
    {
        "/api/health",
        "/api/slskd/webhook",
        "/api/auth/login",
        "/api/auth/status",
        "/api/auth/setup",
    }
)

#: The gate-EXEMPT half of the sign-in surface, whose responses are per-caller
#: anyway: ``status`` reports whether THIS browser is signed in, and ``login``
#: carries the Set-Cookie. Neither may be stored by a shared cache, and neither
#: is covered by the gated arm of :func:`scope_is_private`, so this set is what
#: reaches them.
#:
#: ``/api/auth/logout`` is deliberately NOT here. It is gated, so the first
#: disjunct already marks it private; adding it would be a member no test could
#: distinguish from its absence, and a set that looks better covered than it is.
AUTH_ROUTE_PATHS: Final = frozenset({"/api/auth/login", "/api/auth/status"})

_UNAUTHENTICATED_DETAIL: Final = "authentication required"
# Written as literal bytes for the same reason the origin guard's 403 body is:
# this rejection never reaches the router, so there is no response model to
# serialise through. Kept byte-identical to what ``ErrorDetail`` would render.
_REJECT_BODY: Final = b'{"detail":"' + _UNAUTHENTICATED_DETAIL.encode("ascii") + b'"}'


def auth_posture(password_hash: str, source: PasswordSource) -> str:
    """One clause for the startup posture line: can anyone sign in at all?

    Four states, not two. "Unset", "the env var is set but unreadable" and "the
    stored file is unreadable" have three different fixes, and the operator sees
    this line once at boot and nowhere else — without it a typo'd hash presents
    identically to a working one until the first login attempt fails. The
    fourth state (configured) names the LIVE source, because with two sources
    an operator who removed the compose line has to be told the file is still
    there.

    Takes both halves of :func:`app.auth.source.effective_password` rather than
    re-resolving them, so the line reports the same answer the gate and the
    login route will act on.
    """
    if password_is_configured(password_hash):
        if source == "file":
            return f"password configured from {password_file_location()}"
        return "password configured from the environment"
    if source == "env":
        return (
            "MUSICDROP_PASSWORD_HASH is set but UNREADABLE, so every gated API "
            "request will be rejected until it is replaced (generate one with "
            "`python -m app.auth.hash_password`; in docker-compose every `$` in "
            "the value must be doubled to `$$`)"
        )
    if source == "file":
        return (
            f"the stored password hash at {password_file_location()} is UNREADABLE, "
            "so every gated API request will be rejected until that file is deleted "
            "and MusicDrop restarted (which re-opens the sign-in screen's setup form)"
        )
    return (
        "NO password configured, so every gated API request will be rejected "
        "until one is set on the sign-in screen's setup form (or "
        "MUSICDROP_PASSWORD_HASH is set to override it; generate a hash with "
        "`python -m app.auth.hash_password`)"
    )


def path_requires_session(path: str) -> bool:
    """Whether one request path is behind the session gate.

    The single predicate both the middleware and the OpenAPI overlay ask, so
    "what is gated" is defined exactly once. Callers holding a live ASGI scope
    want :func:`scope_requires_session`, which asks this about the route path
    as well as the raw one.
    """
    if path in EXEMPT_PATHS:
        return False
    return path.startswith(API_PREFIX) or path in DOC_SURFACE_PATHS


def _route_path(scope: Scope) -> str:
    """``scope["path"]`` with any ASGI ``root_path`` prefix removed.

    This must be the string Starlette's router will match, or the gate is back
    to guarding a different path from the one that picks the handler — the
    fail-open in the module docstring.

    Deliberately reimplemented rather than imported: Starlette spells this
    ``starlette._utils.get_route_path``, an underscore-private module, and a
    production import of it would turn a future rename into a container that
    refuses to boot. The equivalence is pinned instead, by
    ``test_route_path_matches_starlettes_own_router`` — which DOES import the
    private symbol (a test may) and compares the two across a table. If
    Starlette changes the algorithm, that test goes red in CI rather than the
    app failing in production, and the fail-closed OR in
    :func:`scope_requires_session` keeps a divergence from being exploitable in
    the meantime.
    """
    path: str = scope.get("path", "")
    root_path: str = scope.get("root_path", "")
    if not root_path or not path.startswith(root_path):
        return path
    if path == root_path:
        return ""
    # Only a prefix ending at a segment boundary is a real mount point:
    # root_path=/musicdrop must not strip /musicdropX/... down to X/....
    if path[len(root_path)] == "/":
        return path[len(root_path) :]
    return path


def scope_requires_session(scope: Scope) -> bool:
    """Whether this request is behind the gate, asked of BOTH forms of its path.

    The route path is the correct question; the raw path is the backstop. See
    the module docstring for the measured fail-open this exists to close.
    """
    return path_requires_session(_route_path(scope)) or path_requires_session(scope.get("path", ""))


def scope_has_valid_session(scope: Scope) -> bool:
    """Whether this connection carries a cookie signed by the live secret.

    Fails CLOSED when ``app.state.session_secret`` is absent: a middleware
    stack built at import time can outlive (or precede) the lifespan that seeds
    the secret, and the safe reading of "no secret" is "nobody is
    authenticated", never "everybody is". The gate never generates a secret of
    its own — that would hand each request a key it alone knows.
    """
    app = scope.get("app")
    secret = getattr(getattr(app, "state", None), "session_secret", None)
    if not isinstance(secret, bytes):
        return False
    token = HTTPConnection(scope).cookies.get(SESSION_COOKIE_NAME)
    # The EFFECTIVE hash, resolved at REQUEST time and never captured: the
    # token's signing key is derived from it, so a password changed through
    # ``POST /api/auth/password`` (which rewrites the file) must invalidate live
    # cookies on the next request rather than the next restart.
    return session_token_is_valid(token, secret, effective_password()[0])


def scope_is_private(scope: Scope) -> bool:
    """Whether this response is specific to ONE caller's session.

    Everything behind the gate, plus the sign-in surface — see
    :data:`AUTH_ROUTE_PATHS` for why the two exempt auth routes count.
    """
    return scope_requires_session(scope) or _route_path(scope) in AUTH_ROUTE_PATHS


def _mark_uncacheable(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """``Cache-Control: no-store`` where absent, and ``Vary: Cookie`` always.

    Where-ABSENT for Cache-Control is the load-bearing half. The three binary
    image endpoints (artist portrait, album cover, playlist artwork) serve a
    content-hash ``ETag`` with their own ``Cache-Control: no-cache`` so a browser
    revalidates and gets a bodiless 304; a blanket ``no-store`` would forbid
    storing the image at all and turn every repaint into a full re-download.
    Their explicit value wins, and revalidation is preserved.

    ``Vary: Cookie`` is unconditional, and merged rather than replaced: it says
    the response depends on WHICH session asked, which is true of a 304 and of a
    revalidated image just as much as of a JSON body. Without it a shared cache
    keyed on URL alone could serve one caller's response to another.
    """
    out = list(headers)
    if not any(name.lower() == b"cache-control" for name, _ in out):
        out.append((b"cache-control", b"no-store"))

    merged: list[tuple[bytes, bytes]] = []
    seen_vary = False
    for name, value in out:
        if name.lower() != b"vary":
            merged.append((name, value))
            continue
        seen_vary = True
        parts = [p.strip() for p in value.split(b",") if p.strip()]
        if not any(p.lower() == b"cookie" for p in parts):
            parts.append(b"Cookie")
        merged.append((name, b", ".join(parts)))
    if not seen_vary:
        merged.append((b"vary", b"Cookie"))
    return merged


class SessionGateMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Non-HTTP scopes pass through untouched (the app has no WebSocket
        # routes today; if it grows one, gate it here deliberately rather than
        # inheriting a decision this line never made).
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        # Cache directives are stamped HERE rather than in
        # SecurityHeadersMiddleware next door for two reasons: that module is
        # imported BY this one (for DOCS_PATHS), so the reverse import would be
        # a cycle; and this middleware already owns the "is this private"
        # predicate, so putting the answer anywhere else creates a second copy
        # to drift. The stamper wraps outside and leaves these alone —
        # Cache-Control and Vary are not in its replace-not-merge set.
        send_out = _uncacheable_send(send) if scope_is_private(scope) else send

        if scope_requires_session(scope) and not scope_has_valid_session(scope):
            await _reject(send_out)
            return
        await self._app(scope, receive, send_out)


def _uncacheable_send(send: Send) -> Send:
    async def send_uncacheable(message: Message) -> None:
        if message["type"] == "http.response.start":
            # A shallow copy, not an in-place edit: the sender still owns the
            # message it handed us (the same discipline security_headers keeps).
            message = {**message, "headers": _mark_uncacheable(message.get("headers", []))}
        await send(message)

    return send_uncacheable


async def _reject(send: Send) -> None:
    # No ``WWW-Authenticate`` header: this is a cookie session, not an HTTP
    # authentication scheme, and naming one (``Basic``) would pop the browser's
    # native credential dialog over the SPA's own login screen.
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": _REJECT_BODY})
