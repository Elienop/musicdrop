"""The request-body size-limit middleware.

Two stacks are exercised here, and the difference matters. ``_app`` builds a
SYNTHETIC app holding this middleware and nothing else — it does NOT resemble
production, where four other middlewares wrap it — so it can only pin what this
middleware emits by itself. The CORS invariant on the 413 is a property of the
whole stack, so it is pinned against the REAL app (``app.main``) below.
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.body_limit import BodySizeLimitMiddleware
from app.config import settings
from app.main import app as real_app

_DEV_ORIGIN = "http://localhost:5173"


def _app(max_bytes: int) -> Starlette:
    async def echo(request: Request) -> PlainTextResponse:
        body = await request.body()
        return PlainTextResponse(str(len(body)))

    app = Starlette(routes=[Route("/", echo, methods=["POST"])])
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_bytes)
    return app


def _oversize_post(client: TestClient, **headers: str) -> httpx.Response:
    """POST to the real app with an oversize DECLARED Content-Length and no body.

    The middleware refuses on the declared header alone — that is the whole
    point of it, and it is what ``test_over_the_cap_is_413`` proves with a real
    body — so the CORS pins below do not need to push 26 MiB through httpx.
    """
    return client.post(
        "/api/config/validate",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(settings.max_body_bytes + 1),
            **headers,
        },
    )


def test_under_the_cap_passes() -> None:
    client = TestClient(_app(max_bytes=10))
    r = client.post("/", content=b"x" * 5)
    assert r.status_code == 200
    assert r.text == "5"


def test_over_the_cap_is_413() -> None:
    client = TestClient(_app(max_bytes=10))
    r = client.post("/", content=b"x" * 50)
    assert r.status_code == 413
    assert r.json()["detail"] == "Request body too large"


def test_the_413_carries_no_cors_headers_of_its_own() -> None:
    """This middleware stamps nothing but Content-Type — no CORS echo, ever.

    It used to echo Allow-Origin/Allow-Credentials itself, on the premise that
    it wrapped outside CORSMiddleware and the rejection would otherwise skip it.
    That premise died when CORS moved outermost (ede5001); the echo would now be
    a second, differently-keyed answer to a question CORS already answers. The
    synthetic stack here has no CORS in it, so an echo would be the only possible
    source of these headers — which is exactly why this is the test that can see
    it. The keyword that fed the old echo is gone from the signature entirely,
    so there is nothing left to key one off.
    """
    client = TestClient(_app(max_bytes=10))
    r = client.post("/", content=b"x" * 50, headers={"Origin": _DEV_ORIGIN})
    assert r.status_code == 413
    assert r.json()["detail"] == "Request body too large"
    assert "access-control-allow-origin" not in r.headers
    assert "access-control-allow-credentials" not in r.headers


# ---------------------------------------------------------------------------
# The real stack: CORSMiddleware owns the 413's CORS headers.
# ---------------------------------------------------------------------------


def test_real_stack_413_carries_cors_for_an_allowed_origin() -> None:
    """The invariant the deleted echo used to serve, now met by CORS alone.

    ``Vary: Origin`` is the evidence of authorship: only CORSMiddleware's
    ``allow_explicit_origin`` adds it, so its presence proves the rejection
    really did travel back out through CORS rather than being decorated here.
    Without it the dev frontend would see an opaque CORS error instead of the
    JSON reason.
    """
    r = _oversize_post(TestClient(real_app), Origin=_DEV_ORIGIN)
    assert r.status_code == 413
    assert r.headers["access-control-allow-origin"] == _DEV_ORIGIN
    assert r.headers["access-control-allow-credentials"] == "true"
    assert "Origin" in r.headers["vary"]


def test_real_stack_413_omits_allow_origin_for_a_disallowed_origin() -> None:
    # The other half of the invariant. Allow-Credentials alone is inert without
    # an Allow-Origin (CORSMiddleware stamps it on every simple response), and a
    # foreign page cannot read the body: the rejection stays opaque to it.
    r = _oversize_post(TestClient(real_app), Origin="http://evil.test")
    assert r.status_code == 413
    assert "access-control-allow-origin" not in r.headers


def test_real_stack_413_omits_cors_when_there_is_no_origin() -> None:
    # No Origin header: CORSMiddleware passes the request straight through, so
    # nothing in the stack has anything to echo.
    r = _oversize_post(TestClient(real_app))
    assert r.status_code == 413
    assert "access-control-allow-origin" not in r.headers
    assert "access-control-allow-credentials" not in r.headers
