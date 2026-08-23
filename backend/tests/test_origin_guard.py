"""The app-wide origin guard: policy, middleware, and real-app wiring.

Spec: docs/superpowers/specs/2026-08-23-origin-guard-design.md. The policy is
a browser-CSRF guard, not auth: a missing Origin (curl, the container
healthcheck, the slskd webhook) is allowed by design.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.api.csrf import origin_allowed
from app.origin_guard import OriginGuardMiddleware, resolve_extra_origins

_EXTRA = ("http://localhost:5173",)


def test_missing_origin_is_allowed() -> None:
    # Non-browser clients (curl, LAN tooling, webhooks) send no Origin.
    assert origin_allowed(None, host="x", forwarded_host=None, extra_origins=())


def test_origin_matching_host_is_allowed() -> None:
    assert origin_allowed("http://nas:3030", host="nas:3030", forwarded_host=None, extra_origins=())


def test_scheme_is_stripped_before_the_host_compare() -> None:
    # Today's semantics (csrf.py splits the scheme off): an https Origin
    # matches an http-hosted authority.
    assert origin_allowed(
        "https://nas:3030", host="nas:3030", forwarded_host=None, extra_origins=()
    )


def test_origin_matching_forwarded_host_is_allowed() -> None:
    # A Host-rewriting reverse proxy: public host arrives in X-Forwarded-Host.
    assert origin_allowed(
        "https://music.example",
        host="127.0.0.1:3030",
        forwarded_host="music.example",
        extra_origins=(),
    )


def test_extra_origin_is_allowed() -> None:
    assert origin_allowed(
        "http://localhost:5173", host="nas:3030", forwarded_host=None, extra_origins=_EXTRA
    )


def test_foreign_origin_is_rejected() -> None:
    assert not origin_allowed(
        "http://evil.test", host="nas:3030", forwarded_host=None, extra_origins=_EXTRA
    )


def test_null_origin_is_rejected() -> None:
    # Sandboxed iframes send the literal string "null".
    assert not origin_allowed("null", host="nas:3030", forwarded_host=None, extra_origins=_EXTRA)


def test_extra_origin_not_in_tuple_is_rejected() -> None:
    # The prod posture: empty tuple, dev origin rejected.
    assert not origin_allowed(
        "http://localhost:5173", host="nas:3030", forwarded_host=None, extra_origins=()
    )


def _guarded_app(extra_origins: tuple[str, ...]) -> Starlette:
    async def ran(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ran")

    app = Starlette(
        routes=[
            Route("/write", ran, methods=["POST", "PUT", "PATCH", "DELETE"]),
            Route("/read", ran, methods=["GET"]),
        ]
    )
    app.add_middleware(OriginGuardMiddleware, extra_origins=extra_origins)
    return app


def test_resolve_dev_mode_allows_the_vite_origin() -> None:
    assert resolve_extra_origins("") == ("http://localhost:5173",)


def test_resolve_prod_mode_allows_no_extra_origins() -> None:
    assert resolve_extra_origins("/app/static") == ()


def test_no_origin_write_executes() -> None:
    # Positive control: the route actually RAN, not merely "wasn't 403".
    r = TestClient(_guarded_app(())).post("/write")
    assert r.status_code == 200
    assert r.text == "ran"


def test_same_origin_write_executes() -> None:
    # TestClient sends Host: testserver.
    r = TestClient(_guarded_app(())).post("/write", headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    assert r.text == "ran"


def test_forwarded_host_write_executes() -> None:
    r = TestClient(_guarded_app(())).post(
        "/write",
        headers={"Origin": "https://music.example", "X-Forwarded-Host": "music.example"},
    )
    assert r.status_code == 200


def test_mismatched_forwarded_host_is_rejected() -> None:
    # Mirrors the spoof arm in test_cover_api.py: a forwarded host that does
    # not match the Origin authority rescues nothing.
    r = TestClient(_guarded_app(())).post(
        "/write",
        headers={"Origin": "http://evil.test", "X-Forwarded-Host": "music.example"},
    )
    assert r.status_code == 403


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_unsafe_method_rejects_a_foreign_origin(method: str) -> None:
    r = TestClient(_guarded_app(())).request(
        method, "/write", headers={"Origin": "http://evil.test"}
    )
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail == "cross-origin request rejected"
    # Wording constraints carried over from the per-route guard's tests.
    assert "upload" not in detail
    assert detail.isascii()


def test_get_with_a_foreign_origin_passes_through() -> None:
    r = TestClient(_guarded_app(())).get("/read", headers={"Origin": "http://evil.test"})
    assert r.status_code == 200
    assert r.text == "ran"


def test_null_origin_write_is_rejected() -> None:
    r = TestClient(_guarded_app(())).post("/write", headers={"Origin": "null"})
    assert r.status_code == 403


def test_dev_origin_allowed_under_the_dev_posture() -> None:
    r = TestClient(_guarded_app(("http://localhost:5173",))).post(
        "/write", headers={"Origin": "http://localhost:5173"}
    )
    assert r.status_code == 200


def test_dev_origin_rejected_under_the_prod_posture() -> None:
    r = TestClient(_guarded_app(())).post("/write", headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 403
