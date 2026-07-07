"""The request-body size-limit middleware."""

from __future__ import annotations

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.body_limit import BodySizeLimitMiddleware


def _app(max_bytes: int) -> Starlette:
    async def echo(request: Request) -> PlainTextResponse:
        body = await request.body()
        return PlainTextResponse(str(len(body)))

    app = Starlette(routes=[Route("/", echo, methods=["POST"])])
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_bytes)
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
