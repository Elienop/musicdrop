"""Security response headers: the app-wide stamping middleware.

The policy strings are written out as LITERALS here rather than imported from
``app.security_headers``: a test that compares the wire value to the same
constant the middleware emits pins the plumbing and nothing about the policy —
loosen ``script-src`` to ``*`` and both sides move together. Every equality
assertion below is against a literal; the directive-level tests then say WHY
each token is in the string, so a future edit that drops one fails with a
readable reason instead of a byte diff.

Placement is the other load-bearing thing: the middleware is added AFTER THE
THREE GUARDS in ``app.main`` so it wraps outside all of them (Starlette applies
middleware in reverse add order) — the only position from which it can stamp
the host guard's 400, the origin guard's 403 and the body limit's 413 — those
never reach the router. CORS is added last and sits outermost, as
python:S8414 requires. The rejection tests below are the behavioral proof of
that placement.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse
from starlette.types import Message, Receive, Scope, Send

from app.body_limit import BodySizeLimitMiddleware
from app.events.broker import EventBroker
from app.host_guard import HostGuardMiddleware
from app.main import app as real_app
from app.origin_guard import OriginGuardMiddleware
from app.security_headers import SecurityHeadersMiddleware, csp_for_path
from app.static_files import mount_static

# The two policies, byte for byte. See the module docstring for why these are
# literals and not imports.
_STRICT = (
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
_DOCS = (
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

_DOCS_PATHS = ("/docs", "/docs/oauth2-redirect", "/redoc")


def _client() -> TestClient:
    # No lifespan (TestClient skips it unless used as a context manager): none of
    # these routes need the beets library, and the middleware runs before the
    # router regardless.
    return TestClient(real_app)


def _directives(policy: str) -> dict[str, list[str]]:
    """A CSP string as ``{directive: [source, ...]}``."""
    out: dict[str, list[str]] = {}
    for part in policy.split(";"):
        tokens = part.split()
        if tokens:
            out[tokens[0]] = tokens[1:]
    return out


def _assert_stamped(response: httpx.Response, *, csp: str) -> None:
    """Every response carries the five headers, exactly once each."""
    assert response.headers.get_list("x-content-type-options") == ["nosniff"]
    assert response.headers.get_list("x-frame-options") == ["DENY"]
    assert response.headers.get_list("referrer-policy") == ["same-origin"]
    assert response.headers.get_list("cross-origin-resource-policy") == ["same-origin"]
    assert response.headers.get_list("content-security-policy") == [csp]


# ---------------------------------------------------------------------------
# The strict policy on ordinary responses.
# ---------------------------------------------------------------------------


def test_api_route_carries_every_header() -> None:
    r = _client().get("/api/health")
    assert r.status_code == 200
    _assert_stamped(r, csp=_STRICT)


def test_api_route_body_is_untouched() -> None:
    # No behavior change besides headers: the route still answers normally.
    r = _client().get("/api/health")
    assert r.json()["status"] == "ok"


def test_404_carries_every_header() -> None:
    r = _client().get("/api/definitely-not-a-route")
    assert r.status_code == 404
    _assert_stamped(r, csp=_STRICT)


def test_validation_422_carries_every_header() -> None:
    # FastAPI renders the validation echo itself, below the router.
    r = _client().post("/api/config/validate", json={})
    assert r.status_code == 422
    _assert_stamped(r, csp=_STRICT)


def test_openapi_json_carries_the_strict_policy() -> None:
    # /openapi.json is NOT one of the relaxed docs paths: it is data, it loads
    # nothing, and it sits one character away from a path that is relaxed.
    r = _client().get("/openapi.json")
    assert r.status_code == 200
    _assert_stamped(r, csp=_STRICT)


# ---------------------------------------------------------------------------
# The outer middlewares' rejections — the placement proof.
# ---------------------------------------------------------------------------


def _middleware_index(cls: object) -> int:
    for i, m in enumerate(real_app.user_middleware):
        if m.cls is cls:
            return i
    raise AssertionError(f"middleware not registered: {cls!r}")


def test_security_headers_wrap_outside_the_guards() -> None:
    # Starlette's add_middleware inserts at index 0, so user_middleware[0] is the
    # OUTERMOST wrapper. The invariant that matters is not "outermost" per se
    # but OUTSIDE all three guards: their rejections (400/413/403) never reach
    # the router, so only a wrapper outside them can stamp them. CORS may sit
    # outside the stamper — it must, added last per python:S8414 — because it
    # never rejects an ordinary request. The FULL order is asserted so a future
    # reshuffle that slips the stamper inside a guard fails here, not quietly.
    expected = (
        CORSMiddleware,
        SecurityHeadersMiddleware,
        HostGuardMiddleware,
        BodySizeLimitMiddleware,
        OriginGuardMiddleware,
    )
    assert [_middleware_index(c) for c in expected] == list(range(len(expected)))


def test_host_guard_rejection_carries_the_headers() -> None:
    r = _client().get("/api/health", headers={"Host": "evil.test:3030"})
    assert r.status_code == 400
    assert r.json() == {"detail": "invalid host"}
    _assert_stamped(r, csp=_STRICT)


def test_origin_guard_rejection_carries_the_headers() -> None:
    r = _client().post(
        "/api/config/validate",
        json={"yaml_text": "a: 1"},
        headers={"Origin": "http://evil.test"},
    )
    assert r.status_code == 403
    assert r.json() == {"detail": "cross-origin request rejected"}
    _assert_stamped(r, csp=_STRICT)


def test_body_limit_rejection_carries_the_headers() -> None:
    r = _client().post(
        "/api/config/validate",
        content=b"x" * (26 * 1024 * 1024),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413
    _assert_stamped(r, csp=_STRICT)


def test_cors_preflight_is_answered_by_cors_itself() -> None:
    """Preflight: 200 + the CORS headers, and deliberately NO security headers.

    CORSMiddleware is now outermost (added last per python:S8414), so it
    answers the preflight itself, before the stamper ever runs. That absence
    is the accepted trade, not a bug: a preflight is a bodiless OPTIONS
    response, and every stamped header is inert on it — nothing renders
    (content-security-policy), nothing is framed (x-frame-options), nothing is
    embedded (cross-origin-resource-policy), there is no body to sniff
    (x-content-type-options), and it carries no navigation intent to govern
    (referrer-policy). Pinning the absence deliberately is better than deleting
    the test: it stops a later "restore" of the old order from landing without
    anyone having understood the trade.
    """
    r = _client().options(
        "/api/config/validate",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    # The five stamped headers are ABSENT on the preflight — the S8414 trade,
    # pinned here so it is never silently "fixed" away.
    assert "x-content-type-options" not in r.headers
    assert "x-frame-options" not in r.headers
    assert "referrer-policy" not in r.headers
    assert "cross-origin-resource-policy" not in r.headers
    assert "content-security-policy" not in r.headers


def test_a_route_cannot_weaken_the_headers_it_sets_itself() -> None:
    # The middleware OWNS these four: a handler that sets its own is replaced,
    # not appended to. Two X-Frame-Options values are ignored outright by some
    # browsers, which would turn a weakening attempt into no protection at all.
    app = FastAPI()

    @app.get("/framed")
    def framed() -> PlainTextResponse:
        return PlainTextResponse(
            "x",
            headers={
                "X-Frame-Options": "SAMEORIGIN",
                "Content-Security-Policy": "default-src *",
                "Referrer-Policy": "unsafe-url",
            },
        )

    app.add_middleware(SecurityHeadersMiddleware)
    r = TestClient(app).get("/framed")
    assert r.status_code == 200
    _assert_stamped(r, csp=_STRICT)


@pytest.mark.anyio
async def test_capitalized_inner_headers_are_still_replaced() -> None:
    # The test above cannot reach the case branch: Starlette's Response
    # lowercases every header name it emits, so only a raw-ASGI sender can
    # produce a capitalized name. Without the .lower() in _stamp, these would
    # ride through NEXT TO ours — and a duplicated X-Frame-Options is ignored
    # outright by some browsers, i.e. the weakening would win.
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"X-Frame-Options", b"SAMEORIGIN"),
                    (b"Content-Security-Policy", b"default-src *"),
                    (b"Cross-Origin-Resource-Policy", b"cross-origin"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"x"})

    wrapped = SecurityHeadersMiddleware(inner)
    sent: list[Message] = []

    async def capture(message: Message) -> None:
        sent.append(message)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    await wrapped({"type": "http", "path": "/anything", "headers": []}, receive, capture)
    headers = [(n.lower(), v) for n, v in sent[0]["headers"]]
    assert headers.count((b"x-frame-options", b"DENY")) == 1
    assert (b"x-frame-options", b"SAMEORIGIN") not in headers
    assert (b"cross-origin-resource-policy", b"same-origin") in headers
    assert (b"cross-origin-resource-policy", b"cross-origin") not in headers
    assert [v for n, v in headers if n == b"content-security-policy"] == [_STRICT.encode()]


# ---------------------------------------------------------------------------
# The SSE stream: a StreamingResponse goes through the same stamping.
# ---------------------------------------------------------------------------


async def _response_start(path: str) -> Message:
    """Drive the real app by hand and return its ``http.response.start``.

    ``/api/events`` never completes — httpx's ASGI transport buffers the whole
    body and would hang — so this reads the response head and cancels. The
    ``receive`` channel yields the (empty) request body once and then blocks
    forever: returning ``http.request`` repeatedly would spin
    ``StreamingResponse``'s disconnect listener.
    """
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 51234),
        "server": ("testserver", 80),
    }
    loop = asyncio.get_running_loop()
    blocked: asyncio.Future[Message] = loop.create_future()
    started: asyncio.Future[Message] = loop.create_future()
    body_sent = False

    async def receive() -> Message:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return await blocked

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start" and not started.done():
            started.set_result(message)

    task = asyncio.create_task(real_app(scope, receive, send))
    try:
        return await asyncio.wait_for(asyncio.shield(started), 5.0)
    finally:
        blocked.cancel()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
async def test_sse_stream_carries_the_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A live broker so the endpoint really streams (no broker = a plain 503,
    # which would prove nothing about StreamingResponse). Deleted afterwards:
    # a leaked broker whose loop is closed poisons every later lifespan-less test.
    monkeypatch.setattr(
        real_app.state,
        "event_broker",
        EventBroker(asyncio.get_running_loop()),
        raising=False,
    )
    start = await _response_start("/api/events")

    assert start["status"] == 200
    headers = {name.decode(): value.decode() for name, value in start["headers"]}
    assert headers["content-type"] == "text/event-stream; charset=utf-8"
    # The endpoint's own headers survive the stamping.
    assert headers["cache-control"] == "no-cache"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "same-origin"
    assert headers["content-security-policy"] == _STRICT


# ---------------------------------------------------------------------------
# Production mode: the SPA fallback and the hashed assets.
# ---------------------------------------------------------------------------


def _prod_client(tmp_path: Path) -> TestClient:
    # Same shape test_static_files.py uses: build our own app over a tmp dist
    # dir, so this does not depend on the real app's import-time settings.
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<!doctype html><div id="root"></div>')
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)")
    app = FastAPI()
    mount_static(app, str(dist))
    app.add_middleware(SecurityHeadersMiddleware)
    return TestClient(app)


def test_spa_index_carries_the_headers(tmp_path: Path) -> None:
    r = _prod_client(tmp_path).get("/")
    assert r.status_code == 200
    assert 'id="root"' in r.text
    _assert_stamped(r, csp=_STRICT)


def test_spa_deep_link_fallback_carries_the_headers(tmp_path: Path) -> None:
    r = _prod_client(tmp_path).get("/artists/Adele")
    assert r.status_code == 200
    assert 'id="root"' in r.text
    assert r.headers["cache-control"] == "no-cache"  # unchanged by the stamping
    _assert_stamped(r, csp=_STRICT)


def test_hashed_asset_carries_the_headers(tmp_path: Path) -> None:
    r = _prod_client(tmp_path).get("/assets/index-abc123.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    _assert_stamped(r, csp=_STRICT)


# ---------------------------------------------------------------------------
# The docs-relaxed policy: exactly three paths.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _DOCS_PATHS)
def test_docs_paths_get_the_relaxed_policy(path: str) -> None:
    r = _client().get(path)
    assert r.status_code == 200
    _assert_stamped(r, csp=_DOCS)


@pytest.mark.parametrize(
    "path",
    [
        "/openapi.json",
        "/docsx",
        "/docs/",  # trailing-slash redirect, still not the docs page itself
        "/docs/oauth2-redirect/",
        "/Docs",
        "/redocly",
        "/api/docs",
        "/api/redoc",
        "/x/docs",
    ],
)
def test_lookalike_paths_stay_strict(path: str) -> None:
    # The match is exact: a prefix/suffix/case-insensitive rewrite of it would
    # hand the relaxed policy (CDN scripts + inline scripts) to app pages.
    r = _client().get(path, follow_redirects=False)
    _assert_stamped(r, csp=_STRICT)


@pytest.mark.parametrize("path", _DOCS_PATHS)
def test_docs_paths_still_carry_the_static_headers(path: str) -> None:
    # Only the CSP relaxes; the four static headers are the same everywhere.
    r = _client().get(path)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "same-origin"
    assert r.headers["cross-origin-resource-policy"] == "same-origin"


def test_relaxed_policy_permits_exactly_the_docs_dependencies() -> None:
    # Exact equality, not membership: this test is the sole end-to-end
    # distinguisher between the two policies (proven by combo mutation), so a
    # membership assert would let an extra origin ride in unnoticed.
    d = _directives(_DOCS)
    # Swagger UI: bundle JS + CSS from jsdelivr, an inline init script,
    # the favicon from fastapi.tiangolo.com, /openapi.json same-origin.
    # ReDoc: a Google Fonts stylesheet, its font files, a blob: web worker,
    # and its default logo from cdn.redoc.ly.
    assert d["script-src"] == ["'self'", "'unsafe-inline'", "https://cdn.jsdelivr.net"]
    assert d["style-src"] == [
        "'self'",
        "'unsafe-inline'",
        "https://cdn.jsdelivr.net",
        "https://fonts.googleapis.com",
    ]
    assert d["img-src"] == [
        "'self'",
        "data:",
        "https://fastapi.tiangolo.com",
        "https://cdn.redoc.ly",
    ]
    assert d["font-src"] == ["'self'", "https://fonts.gstatic.com"]
    assert d["worker-src"] == ["blob:"]
    assert d["connect-src"] == ["'self'"]


def test_relaxed_policy_keeps_the_hardening_directives() -> None:
    d = _directives(_DOCS)
    assert d["default-src"] == ["'self'"]
    assert d["frame-ancestors"] == ["'none'"]
    assert d["object-src"] == ["'none'"]
    assert d["base-uri"] == ["'self'"]
    assert d["form-action"] == ["'self'"]


@pytest.mark.parametrize("policy", [_STRICT, _DOCS])
def test_no_policy_uses_a_wildcard_or_eval(policy: str) -> None:
    assert "*" not in policy
    assert "'unsafe-eval'" not in policy


def test_strict_policy_allows_no_remote_script() -> None:
    # THE difference between the two policies: app pages run same-origin code
    # only — no CDN, no inline. Relax this and the docs carve-out was pointless.
    d = _directives(_STRICT)
    assert d["script-src"] == ["'self'"]
    assert d["default-src"] == ["'self'"]
    assert d["frame-ancestors"] == ["'none'"]
    assert d["object-src"] == ["'none'"]
    assert d["base-uri"] == ["'self'"]
    assert d["form-action"] == ["'self'"]


def test_strict_policy_permits_the_ui_it_has_to_serve() -> None:
    d = _directives(_STRICT)
    # shadcn/Radix set style attributes; the art panels preview user-pasted
    # image URLs (any scheme) and blob: object URLs from local file picks.
    # Exact equality so an extra origin cannot ride in on either directive.
    assert d["style-src"] == ["'self'", "'unsafe-inline'"]
    assert set(d["img-src"]) == {"'self'", "blob:", "data:", "https:", "http:"}
    assert d["connect-src"] == ["'self'"]  # same-origin API + the SSE stream


# ---------------------------------------------------------------------------
# The path predicate in isolation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _DOCS_PATHS)
def test_csp_for_path_relaxes_the_docs_paths(path: str) -> None:
    assert csp_for_path(path) == _DOCS


@pytest.mark.parametrize(
    "path",
    ["/", "/openapi.json", "/docsx", "/docs/", "/DOCS", "/api/docs", "/redoc/", "/xredoc", ""],
)
def test_csp_for_path_is_strict_everywhere_else(path: str) -> None:
    assert csp_for_path(path) == _STRICT


def test_relaxed_paths_match_the_apps_real_docs_urls() -> None:
    # Drift guard: change docs_url/redoc_url on the FastAPI() call and the
    # relaxed policy would land on a path nobody serves while the real docs
    # page breaks under the strict one.
    for url in (real_app.docs_url, real_app.redoc_url, real_app.swagger_ui_oauth2_redirect_url):
        assert url is not None
        assert csp_for_path(url) == _DOCS
