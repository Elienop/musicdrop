"""The request-body size-limit middleware."""

from __future__ import annotations

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.body_limit import BodySizeLimitMiddleware


def _app(max_bytes: int, allowed_origins: tuple[str, ...] = ()) -> Starlette:
    async def echo(request: Request) -> PlainTextResponse:
        body = await request.body()
        return PlainTextResponse(str(len(body)))

    app = Starlette(routes=[Route("/", echo, methods=["POST"])])
    app.add_middleware(
        BodySizeLimitMiddleware, max_bytes=max_bytes, allowed_origins=allowed_origins
    )
    return app


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


def test_413_echoes_cors_for_the_dev_frontend_origin() -> None:
    # The body-limit middleware wraps OUTERMOST, so on rejection the inner
    # CORSMiddleware never runs. It must echo the CORS headers itself for an
    # allowed origin, or the dev frontend sees an opaque CORS error, not the body.
    client = TestClient(_app(max_bytes=10, allowed_origins=("http://localhost:5173",)))
    r = client.post("/", content=b"x" * 50, headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 413
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert r.headers["access-control-allow-credentials"] == "true"
    assert r.json()["detail"] == "Request body too large"


def test_413_omits_cors_for_the_dev_origin_under_the_prod_posture() -> None:
    # Prod resolves an empty tuple, so even the Vite origin gets no echo.
    client = TestClient(_app(max_bytes=10))
    r = client.post("/", content=b"x" * 50, headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 413
    assert "access-control-allow-origin" not in r.headers


def test_413_omits_cors_for_a_disallowed_origin() -> None:
    client = TestClient(_app(max_bytes=10))
    r = client.post("/", content=b"x" * 50, headers={"Origin": "http://evil.test"})
    assert r.status_code == 413
    assert "access-control-allow-origin" not in r.headers


def test_413_omits_cors_for_a_foreign_origin_when_the_allowlist_is_non_empty() -> None:
    # Membership, not emptiness: the sibling omission tests use an empty allowlist,
    # so they cannot see an echo that ignores the tuple's contents. Without this,
    # `value if value in self._allowed` degraded to `value if self._allowed` lets a
    # 413 echo ANY origin back, with credentials.
    client = TestClient(_app(max_bytes=10, allowed_origins=("http://localhost:5173",)))
    r = client.post("/", content=b"x" * 50, headers={"Origin": "http://evil.test"})
    assert r.status_code == 413
    assert "access-control-allow-origin" not in r.headers


def test_413_omits_cors_when_no_origin() -> None:
    client = TestClient(_app(max_bytes=10))
    r = client.post("/", content=b"x" * 50)
    assert r.status_code == 413
    assert "access-control-allow-origin" not in r.headers
