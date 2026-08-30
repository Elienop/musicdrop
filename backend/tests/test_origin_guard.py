"""The app-wide origin guard: policy, middleware, and real-app wiring.

Spec: docs/superpowers/specs/2026-08-23-origin-guard-design.md. The policy is
a browser-CSRF guard, not auth: a missing Origin (curl, the container
healthcheck, the slskd webhook) is allowed by design.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.api.csrf import origin_allowed
from app.main import app as real_app
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


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil-nas:3030",
        "http://nas:3030.evil.test",
        "http://nas:30300",
        "https://nas:3030.evil.test",
    ],
)
def test_authority_compare_is_exact_not_a_suffix_or_substring(origin: str) -> None:
    # The canonical origin-validation bug is a suffix/substring match. Each origin
    # here is a near-miss on the host, so `==` degraded to `endswith`/`in` lets one
    # of them through. Both the host arm and the forwarded-host arm are covered.
    # The other negative fixtures share no substring with the host, so they are
    # blind to this: only near-misses can see it.
    assert not origin_allowed(origin, host="nas:3030", forwarded_host=None, extra_origins=())
    assert not origin_allowed(
        origin, host="127.0.0.1:3030", forwarded_host="nas:3030", extra_origins=()
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


def test_real_app_bodyless_post_rejects_a_foreign_origin() -> None:
    # /api/reorganize/dismiss was one of the 19 open CORS-simple routes.
    r = TestClient(real_app).post("/api/reorganize/dismiss", headers={"Origin": "http://evil.test"})
    assert r.status_code == 403
    assert r.json()["detail"] == "cross-origin request rejected"


def test_real_app_json_post_rejects_a_foreign_origin() -> None:
    # Defense-in-depth: JSON routes no longer rely on the preflight assumption.
    r = TestClient(real_app).post(
        "/api/config/validate",
        json={"yaml_text": "a: 1"},
        headers={"Origin": "http://evil.test"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "cross-origin request rejected"


def test_real_app_same_origin_post_still_works() -> None:
    r = TestClient(real_app).post(
        "/api/config/validate",
        json={"yaml_text": "a: 1"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200


def test_real_app_no_origin_post_still_works() -> None:
    # curl / LAN tooling / webhooks: unchanged by this slice.
    r = TestClient(real_app).post("/api/config/validate", json={"yaml_text": "a: 1"})
    assert r.status_code == 200


def test_real_app_dev_origin_post_works_in_dev_mode() -> None:
    # The test env has no MUSICDROP_STATIC_DIR, so the real app is in dev mode.
    r = TestClient(real_app).post(
        "/api/config/validate",
        json={"yaml_text": "a: 1"},
        headers={"Origin": "http://localhost:5173"},
    )
    assert r.status_code == 200


def test_real_app_get_with_a_foreign_origin_passes() -> None:
    r = TestClient(real_app).get("/api/health", headers={"Origin": "http://evil.test"})
    assert r.status_code == 200


def test_prod_posture_rejects_the_dev_origin_write(tmp_path: Path) -> None:
    """The prod half of the dev/prod posture, on the REAL app wiring.

    The suite itself runs in dev posture (no ``MUSICDROP_STATIC_DIR``), where a
    correctly-resolved tuple and a hardcoded ``("http://localhost:5173",)`` are
    identical — so no in-process test can tell them apart. This builds
    ``app.main`` in a subprocess with ``MUSICDROP_STATIC_DIR`` set, so
    import-time settings resolve to PRODUCTION, and pins main.py's
    ``extra_origins = resolve_extra_origins(settings.static_dir)`` linkage:
    hardcode the dev tuple there and the guard accepts :5173 in prod, so this
    fails.

    Status codes alone only see the GUARD. The child therefore also prints both
    resolved consumers, because hardcoding CORS's ``allow_origins`` leaves the
    guard's 403/200 untouched while silently granting a foreign page credentialed
    cross-origin READ access to the whole API in production.
    ``test_both_middlewares_read_one_resolved_tuple`` cannot see it either: it
    compares the consumers to EACH OTHER, and the suite runs in dev posture
    where the correct tuple and a hardcoded dev
    tuple are identical.
    """
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<!doctype html><div id="root"></div>')
    # The child runs without conftest, so it seeds the session secret and mints
    # its own cookie: `/api/config/validate` is gated, and an un-authenticated
    # child would report 401 for BOTH requests — which would pass the "dev
    # origin is rejected" half for entirely the wrong reason. The auth wiring
    # here is scaffolding; the assertions below still pin ORIGIN posture only.
    code = (
        "from starlette.testclient import TestClient\n"
        "from app.main import app\n"
        "from app.auth.session import SESSION_COOKIE_NAME, mint_session_token\n"
        "from app.config import settings\n"
        "secret = b'0123456789abcdef0123456789abcdef'\n"
        "app.state.session_secret = secret\n"
        "c = TestClient(app, cookies={SESSION_COOKIE_NAME:"
        " mint_session_token(secret, settings.password_hash)})\n"
        "dev = c.post('/api/config/validate', json={'yaml_text': 'a: 1'},"
        " headers={'Origin': 'http://localhost:5173'})\n"
        "same = c.post('/api/config/validate', json={'yaml_text': 'a: 1'},"
        " headers={'Origin': 'http://testserver'})\n"
        "g = next(m.kwargs['extra_origins'] for m in app.user_middleware"
        " if m.cls.__name__ == 'OriginGuardMiddleware')\n"
        "cors = next(m.kwargs['allow_origins'] for m in app.user_middleware"
        " if m.cls.__name__ == 'CORSMiddleware')\n"
        "print(dev.status_code, same.status_code)\n"
        "print(tuple(g), list(cors))\n"
    )
    # The child runs in PROD posture, where the host guard (which wraps outside
    # the origin guard) rejects the TestClient's `Host: testserver` before the
    # origin guard runs; this test is about ORIGIN posture, so allowlist the
    # test host explicitly.
    # Prod host-guard behavior has its own subprocess test in test_host_guard.py.
    env = {
        **os.environ,
        "MUSICDROP_STATIC_DIR": str(dist),
        "MUSICDROP_ALLOWED_HOSTS": "testserver",
        # Hermetic binding: the session token is signed with a key derived
        # from MUSICDROP_PASSWORD_HASH, and an env var beats backend/.env —
        # so an owner who sets a real hash locally cannot change what this
        # child mints under.
        "MUSICDROP_PASSWORD_HASH": "",
    }
    backend = Path(__file__).resolve().parents[1]
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=backend,
        timeout=180,
        check=False,
    )
    # Prod posture: the dev origin is a foreign origin (403); same-origin still
    # 200 (so a child that 403s everything cannot pass this for the wrong reason).
    assert out.returncode == 0, f"child failed: stderr={out.stderr!r}"
    lines = out.stdout.strip().splitlines()
    assert len(lines) == 2, f"stdout={out.stdout!r} stderr={out.stderr!r}"
    statuses, wiring = lines
    assert statuses == "403 200", f"stdout={out.stdout!r} stderr={out.stderr!r}"
    # ...and EVERY consumer resolved to the production tuple, not the dev one:
    # guard `()`, CORS `[]`. (The body limit was a third consumer until 8eda506
    # (#181) moved CORS outermost and its hand-rolled 413 echo became redundant;
    # it no longer reads the tuple at all, so there is nothing left to hardcode
    # there.)
    assert wiring == "() []", f"stdout={out.stdout!r} stderr={out.stderr!r}"


def _middleware_kwargs(cls: object) -> dict[str, object]:
    for m in real_app.user_middleware:
        if m.cls is cls:
            return dict(m.kwargs)
    raise AssertionError(f"middleware not registered: {cls!r}")


def test_both_middlewares_read_one_resolved_tuple() -> None:
    # The spec's consolidation invariant: the guard and the CORS allowlist read
    # the SAME resolved origin tuple. The body limit was a third consumer until
    # 8eda506, #181 (see the docstring on the prod-posture test above).
    guard = _middleware_kwargs(OriginGuardMiddleware)["extra_origins"]
    assert isinstance(guard, tuple)
    assert _middleware_kwargs(CORSMiddleware)["allow_origins"] == list(guard)

    cors = _middleware_kwargs(CORSMiddleware)
    # X-Forwarded-Host is guard-trusted, so any origin CORS approves could steer
    # the guard's authority compare. Keep CORS from ever exceeding the tuple.
    assert cors.get("allow_origin_regex") is None
    allow_origins = cors["allow_origins"]
    assert isinstance(allow_origins, list)
    assert "*" not in allow_origins


def test_oversize_body_beats_the_origin_guard() -> None:
    # Pins the middleware nesting: BodySizeLimit wraps outside the origin
    # guard, so an
    # oversize foreign-origin write is refused 413 before the origin guard
    # sees it. Reordering the add_middleware calls in app.main breaks this.
    r = TestClient(real_app).post(
        "/api/config/validate",
        content=b"x" * (26 * 1024 * 1024),
        headers={"Origin": "http://evil.test", "Content-Type": "application/json"},
    )
    assert r.status_code == 413
