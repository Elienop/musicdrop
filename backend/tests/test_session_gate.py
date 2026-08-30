"""The session gate: what it refuses, what it lets past, and where it sits.

The headline invariant is the first test in this file — before this middleware
existed, anyone who could reach the port could read the whole library through
``GET /api/config``. Everything else here is the fence around that: the four
exempt paths that must stay open, the SPA shell that must stay open (or the
login screen has nowhere to render), the doc surface that must NOT, and the
cheaper guards that must keep winning ahead of it.

Every ``TestClient`` in this suite arrives holding a valid cookie (see
``tests/conftest.py``), so most tests here work by taking it AWAY. That
arrangement is itself load-bearing and has its own positive control at the
bottom of this file: if the conftest stamping ever stopped happening, the gate
would be proven live by ~2,500 red tests rather than silently bypassed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from httpx import Response
from starlette.types import Message, Receive, Scope, Send

from app.auth.gate import (
    AUTH_ROUTE_PATHS,
    DOC_SURFACE_PATHS,
    EXEMPT_PATHS,
    SessionGateMiddleware,
    _route_path,
    path_requires_session,
)
from app.auth.session import SESSION_COOKIE_NAME, mint_session_token
from app.body_limit import BodySizeLimitMiddleware
from app.config import settings
from app.host_guard import HostGuardMiddleware
from app.main import app as real_app
from app.origin_guard import OriginGuardMiddleware
from app.security_headers import SecurityHeadersMiddleware
from app.static_files import mount_static
from tests.conftest import TEST_SESSION_SECRET, session_cookie_value

_GATED_READ = "/api/config"
_GATED_WRITE = "/api/config/validate"
#: A gated route that answers 200 with no beets library behind it, for the
#: assertions that need a real success rather than "anything but 401".
_GATED_SERVEABLE = "/openapi.json"
_UNAUTHENTICATED = {"detail": "authentication required"}


def _anonymous() -> TestClient:
    """A client with the conftest cookie removed — a browser that never signed in."""
    client = TestClient(real_app)
    client.cookies.clear()
    return client


# --------------------------------------------------------------------------
# What the gate closes
# --------------------------------------------------------------------------


def test_unauthenticated_config_read_is_refused() -> None:
    """THE exposure this slice closes: the library config was world-readable.

    ``GET /api/config`` returns the beets configuration — library path, plugin
    list, import behaviour — to anyone who could reach port 3030. It is named
    explicitly rather than folded into a parametrized sweep because it is the
    concrete reason the gate exists.
    """
    resp = _anonymous().get(_GATED_READ)
    assert resp.status_code == 401
    assert resp.json() == _UNAUTHENTICATED


def test_unauthenticated_write_is_refused() -> None:
    resp = _anonymous().post(_GATED_WRITE, json={"yaml_text": "a: 1"})
    assert resp.status_code == 401
    assert resp.json() == _UNAUTHENTICATED


@pytest.mark.parametrize("path", sorted(DOC_SURFACE_PATHS))
def test_the_doc_surface_is_gated(path: str) -> None:
    """``/openapi.json`` is the entire API contract; ``/docs`` executes it.

    Anonymous access to either hands an attacker the complete map of the
    endpoints the gate is protecting.
    """
    assert _anonymous().get(path).status_code == 401


#: A well-formed token, for the malformations below to be derived from.
_GOOD_TOKEN = mint_session_token(TEST_SESSION_SECRET, settings.password_hash)


@pytest.mark.parametrize(
    ("label", "cookie"),
    [
        ("garbage", "not-a-token"),
        ("wrong version", "v2." + _GOOD_TOKEN.split(".", 1)[1]),
        ("truncated", _GOOD_TOKEN.rsplit(".", 1)[0]),
        ("empty", ""),
    ],
)
def test_a_malformed_cookie_is_refused(label: str, cookie: str) -> None:
    client = _anonymous()
    client.cookies.set(SESSION_COOKIE_NAME, cookie)
    assert client.get(_GATED_READ).status_code == 401, label


def test_a_tampered_signature_is_refused() -> None:
    """Flip one character of the SIGNATURE, leave the payload alone.

    This is the mutation-sensitive one: replace the ``compare_digest`` check
    with ``True`` and every other cookie test here still passes, because they
    all send something the parser rejects earlier.
    """
    version, payload, signature = mint_session_token(
        TEST_SESSION_SECRET, settings.password_hash
    ).split(".")
    flipped = ("B" if signature[0] != "B" else "C") + signature[1:]
    client = _anonymous()
    client.cookies.set(SESSION_COOKIE_NAME, f"{version}.{payload}.{flipped}")
    assert client.get(_GATED_READ).status_code == 401


def test_a_tampered_expiry_is_refused() -> None:
    """Extend the expiry and keep the old signature: the HMAC covers it."""
    version, _, signature = mint_session_token(
        TEST_SESSION_SECRET, settings.password_hash, max_age_seconds=-1
    ).split(".")
    far_future = "MjUyNDYwODAwMA"  # base64url of "2524608000" (year 2050)
    client = _anonymous()
    client.cookies.set(SESSION_COOKIE_NAME, f"{version}.{far_future}.{signature}")
    assert client.get(_GATED_READ).status_code == 401


def test_an_expired_token_is_refused() -> None:
    """A properly signed token past its expiry. Delete the expiry check and
    this is the only test that goes red."""
    client = _anonymous()
    client.cookies.set(
        SESSION_COOKIE_NAME,
        mint_session_token(TEST_SESSION_SECRET, settings.password_hash, max_age_seconds=-1),
    )
    assert client.get(_GATED_READ).status_code == 401


def test_a_cookie_signed_by_another_secret_is_refused() -> None:
    client = _anonymous()
    client.cookies.set(SESSION_COOKIE_NAME, mint_session_token(b"a" * 32, settings.password_hash))
    assert client.get(_GATED_READ).status_code == 401


def test_a_valid_cookie_passes() -> None:
    """The positive arm: same client, same gated route, valid cookie -> served.

    ``/openapi.json`` rather than ``/api/config`` because it is a gated route
    that answers 200 without a beets library, so this asserts a REAL success
    instead of "not 401" over a 500.
    """
    client = _anonymous()
    client.cookies.set(SESSION_COOKIE_NAME, session_cookie_value())
    assert client.get(_GATED_SERVEABLE).status_code == 200


def test_the_gate_fails_closed_with_no_secret_on_app_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No secret is "nobody is authenticated", never "everybody is".

    Seeded illegal state: a lifespan-less client whose conftest seed has been
    removed. The client still presents a cookie that WOULD be valid, so this
    fails only on the fail-open reading.
    """
    monkeypatch.delattr(real_app.state, "session_secret")
    client = TestClient(real_app)
    assert client.get(_GATED_READ).status_code == 401


# --------------------------------------------------------------------------
# What the gate must NOT close
# --------------------------------------------------------------------------


def test_health_is_reachable_without_a_cookie() -> None:
    """The image's HEALTHCHECK calls this with bare urllib and no credentials."""
    resp = _anonymous().get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_the_webhook_keeps_its_own_401_and_not_the_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The webhook authenticates itself; the gate must not double-gate it.

    Its module docstring pins "401 is the ONLY non-2xx it ever returns", which
    stays true only while the gate leaves the path alone — the assertion is on
    the DETAIL, because a gate 401 would be indistinguishable by status alone
    and would mean slskd's shared secret had stopped being what decides.
    """
    music = tmp_path / "music"
    music.mkdir()
    (tmp_path / "config.yaml").write_text(
        f"directory: {music}\nlibrary: library.db\nplugins:\n  - musicbrainz\n"
    )
    monkeypatch.setattr("app.config.settings.beets_dir", str(tmp_path))
    resp = _anonymous().post(
        "/api/slskd/webhook",
        json={"type": "DownloadDirectoryComplete", "localDirectoryName": "/downloads/x"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "invalid webhook token"}


def test_auth_status_is_reachable_without_a_cookie() -> None:
    resp = _anonymous().get("/api/auth/status")
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is False


def test_login_is_reachable_without_a_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refused for the LOGIN route's reason, not the gate's — otherwise there
    would be no way in at all."""
    monkeypatch.setattr("app.config.settings.password_hash", "")
    resp = _anonymous().post("/api/auth/login", json={"password": "x"})
    assert resp.status_code == 401
    assert resp.json() != _UNAUTHENTICATED


def test_the_exempt_set_is_exactly_the_four_documented_paths() -> None:
    """A census, so a fifth exemption cannot be added without a decision.

    Each of these is exempt for a caller that cannot hold a cookie; anything
    added here is a hole in the gate and should be visible in a diff.
    """
    assert EXEMPT_PATHS == {
        "/api/health",
        "/api/slskd/webhook",
        "/api/auth/login",
        "/api/auth/status",
    }


@pytest.mark.parametrize(
    "path",
    [
        "/api/healthz",  # a longer name that shares the prefix
        "/api/health/detail",  # a child path
        "/api/HEALTH",  # case
        "/api/health/",  # trailing slash
        "/api/health/../config",  # the traversal shape
    ],
)
def test_exemption_is_an_exact_match_not_a_prefix(path: str) -> None:
    """The anti-traversal invariant, as a predicate test.

    A path that string-equals an exempt path routes to that exempt route and
    nothing else. Relax the compare to ``startswith`` and every lookalike here
    becomes an anonymous door into a DIFFERENT handler.
    """
    assert path_requires_session(path) is True


def test_a_doubled_separator_is_not_gated_and_reaches_no_route() -> None:
    """``//api/config`` fails the prefix test — and that is safe, measured.

    The gate matches ``/api/`` against the same string the router matches, so a
    path the router cannot resolve to an API route is a path the gate does not
    need to cover. Pinned behaviourally rather than argued: if Starlette ever
    started normalising the doubled slash, this test goes red and the prefix
    check has to grow with it. (In production the SPA catch-all answers it with
    index.html, which is likewise not library data.)
    """
    assert path_requires_session("//api/config") is False
    assert _anonymous().get("//api/config").status_code == 404


@pytest.mark.parametrize(
    "path", ["/", "/index.html", "/assets/index-abc123.js", "/favicon.svg", "/manifest.webmanifest"]
)
def test_the_spa_shell_is_not_gated(path: str) -> None:
    assert path_requires_session(path) is False


def test_an_anonymous_browser_can_load_the_built_spa(tmp_path: Path) -> None:
    """End-to-end, not just the predicate: the gate in front of a real mount.

    Slice 2's login screen ships INSIDE this bundle. If the shell 401s, a
    signed-out browser has no way to be shown a password field, so this is the
    property that makes an API-side auth slice deployable at all.
    """
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<!doctype html><div id="root"></div>')
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)")

    app = FastAPI()
    api = APIRouter()

    @api.get("/config")
    def config() -> dict[str, str]:
        return {"library": "secret"}

    app.include_router(api, prefix="/api")
    mount_static(app, str(dist))
    app.add_middleware(SessionGateMiddleware)
    app.state.session_secret = TEST_SESSION_SECRET

    client = TestClient(app)
    client.cookies.clear()
    assert client.get("/").status_code == 200
    assert client.get("/assets/index-abc123.js").status_code == 200
    # ...and the same anonymous client still cannot read the API behind it.
    assert client.get("/api/config").status_code == 401


# --------------------------------------------------------------------------
# Where the gate sits in the stack
# --------------------------------------------------------------------------


def test_the_gate_is_the_innermost_middleware() -> None:
    """Added first, so Starlette wraps every other guard outside it.

    Pinned by position rather than by name-checking one neighbour: the whole
    point of the seat is that EVERY other middleware is outside it.
    """
    registered: list[object] = [m.cls for m in real_app.user_middleware]
    # ``add_middleware`` INSERTS at index 0, so this list is in reverse add
    # order and the first-added — innermost — middleware is last. Compared by
    # class IDENTITY rather than by name, so a same-named replacement cannot
    # satisfy it.
    assert registered[-1] is SessionGateMiddleware
    assert registered == [
        CORSMiddleware,
        SecurityHeadersMiddleware,
        HostGuardMiddleware,
        BodySizeLimitMiddleware,
        OriginGuardMiddleware,
        SessionGateMiddleware,
    ]


def test_disallowed_host_beats_the_session_gate() -> None:
    """400, not 401: the host guard wraps outside and answers first.

    The cheapest, outermost guard wins — a request that should never have
    reached this hostname is refused without spending an HMAC on it.
    """
    resp = _anonymous().get(_GATED_READ, headers={"Host": "evil.test"})
    assert resp.status_code == 400
    assert resp.json() == {"detail": "invalid host"}


def test_a_cross_origin_write_beats_the_session_gate() -> None:
    """403, not 401.

    Both readings are defensible in the abstract; this pins what the stack
    actually does, and the ordering is the right one: a forged cross-origin
    write is refused as CSRF without the server deciding whether the victim's
    browser also happened to be signed in.
    """
    resp = _anonymous().post(
        _GATED_WRITE, json={"yaml_text": "a: 1"}, headers={"Origin": "http://evil.test"}
    )
    assert resp.status_code == 403
    assert resp.json() == {"detail": "cross-origin request rejected"}


def test_an_oversize_body_beats_the_session_gate() -> None:
    from app.config import settings

    oversize = b"x" * (settings.max_body_bytes + 1)
    resp = _anonymous().post(
        _GATED_WRITE, content=oversize, headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 413


def test_unauthenticated_401_carries_the_security_headers() -> None:
    """Proves the stamper really wraps outside the gate.

    The gate writes its 401 straight to the transport without reaching the
    router, so only a middleware outside it can add these. Move the gate
    outside the stamper and this is what goes red.
    """
    resp = _anonymous().get(_GATED_READ)
    assert resp.status_code == 401
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in resp.headers["content-security-policy"]


@pytest.mark.anyio
async def test_a_websocket_scope_passes_through_untouched() -> None:
    """Non-HTTP scopes are forwarded, not judged.

    The app has no WebSocket routes today; this pins that the gate's decision
    is scoped to HTTP rather than silently refusing (or silently admitting)
    a protocol it was never taught about.
    """
    seen: list[Scope] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)

    async def receive() -> Message:  # pragma: no cover - never awaited
        raise AssertionError("the gate must not read from a websocket")

    async def send(message: Message) -> None:  # pragma: no cover - never called
        raise AssertionError("the gate must not answer a websocket")

    gate = SessionGateMiddleware(inner)
    await gate({"type": "websocket", "path": "/api/anything"}, receive, send)
    assert [s["path"] for s in seen] == ["/api/anything"]


# --------------------------------------------------------------------------
# The test seam itself
# --------------------------------------------------------------------------


def test_clearing_the_seam_cookie_makes_the_gate_bite() -> None:
    """The positive control for conftest's TestClient patch.

    Every other test in this suite passes a gated request because
    ``tests/conftest.py`` stamps a real, production-minted cookie onto every
    client. This is the pair that proves the stamping is what does it: same
    client, same route, cookie removed, and the gate answers 401. Without this,
    a patch that silently stopped working (or a gate that silently stopped
    checking) would look identical from inside the suite.
    """
    stamped = TestClient(real_app)
    assert stamped.get(_GATED_SERVEABLE).status_code == 200
    stamped.cookies.clear()
    assert stamped.get(_GATED_SERVEABLE).status_code == 401


def test_the_seam_mints_through_the_production_function() -> None:
    """The cookie the suite carries is one the production gate accepts.

    Not a hand-rolled equivalent: if the token format changed and the gate
    stopped accepting what conftest mints, this is the sentence that says why
    ~2,500 tests went red.
    """
    from app.auth.session import session_token_is_valid

    assert (
        session_token_is_valid(session_cookie_value(), TEST_SESSION_SECRET, settings.password_hash)
        is True
    )


def test_both_testclient_import_paths_are_the_patched_class() -> None:
    """One class-object patch has to cover both spellings.

    ``tests/test_host_guard.py`` imports from starlette, most files from
    fastapi. Patching a MODULE attribute would cover one and miss the other,
    and the miss would show up as a handful of unexplained 401s.
    """
    import fastapi.testclient
    import starlette.testclient

    assert fastapi.testclient.TestClient is starlette.testclient.TestClient
    client: Any = starlette.testclient.TestClient(real_app)
    assert client.cookies.get(SESSION_COOKIE_NAME)


# --------------------------------------------------------------------------
# root_path: the path the ROUTER matches is not always scope["path"]
# --------------------------------------------------------------------------


async def _drive(path: str, *, root_path: str = "", cookie: str | None = None) -> int:
    """Status from driving the real app with a hand-built scope.

    TestClient cannot set ``root_path`` on the scope, so a reverse-proxy mount
    is only reachable this way — which is exactly why the bypass below survived
    a suite of 2,676 tests.
    """
    headers: list[tuple[bytes, bytes]] = [(b"host", b"testserver")]
    if cookie is not None:
        headers.append((b"cookie", f"{SESSION_COOKIE_NAME}={cookie}".encode("ascii")))
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": root_path,
        "headers": headers,
        "client": ("127.0.0.1", 51234),
        "server": ("testserver", 80),
    }
    captured: dict[str, int] = {}

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            captured["status"] = int(message["status"])

    await real_app(scope, receive, send)
    return captured["status"]


@pytest.mark.anyio
async def test_a_gated_path_behind_a_root_path_is_still_gated() -> None:
    """The measured fail-open this closes.

    Under ``uvicorn --root-path /musicdrop`` the scope path is
    ``/musicdrop/openapi.json`` while the router matches ``/openapi.json``. A
    gate reading only the raw path saw no ``/api/`` prefix, passed it through,
    and the router served the whole contract anonymously — 200 with a
    309,375-byte body, reproduced against this app.
    """
    assert await _drive("/musicdrop/openapi.json", root_path="/musicdrop") == 401
    assert await _drive("/musicdrop/api/config", root_path="/musicdrop") == 401


@pytest.mark.anyio
async def test_a_valid_cookie_still_works_behind_a_root_path() -> None:
    """The control: the fix refuses the anonymous case, not the prefix itself."""
    assert (
        await _drive(
            "/musicdrop/openapi.json", root_path="/musicdrop", cookie=session_cookie_value()
        )
        == 200
    )


@pytest.mark.anyio
async def test_an_exempt_path_behind_a_root_path_stays_exempt() -> None:
    """The healthcheck must survive being mounted under a prefix.

    Erring closed is right for a gated path; doing it to ``/api/health`` would
    make the container report unhealthy forever behind a reverse proxy.
    """
    assert await _drive("/musicdrop/api/health", root_path="/musicdrop") == 200


@pytest.mark.anyio
async def test_the_raw_path_variant_is_gated_with_no_root_path() -> None:
    """The unprefixed control, so the two arms cannot both be reading one path."""
    assert await _drive("/openapi.json") == 401
    assert await _drive("/api/config") == 401


@pytest.mark.anyio
async def test_the_raw_path_is_consulted_even_when_stripping_goes_wrong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OR's second arm, with the state that makes it matter SEEDED.

    ``scope_requires_session`` asks about the route path OR the raw path, and
    the second arm is unreachable while ``_route_path`` agrees with Starlette —
    which the test above pins that it does. So nothing observes the backstop in
    normal operation, and deleting it leaves the whole gate suite green
    (measured: 57 passed with the arm removed).

    A backstop that no test can distinguish from its absence is not a backstop,
    so break the precondition on purpose: make the stripping return a path the
    gate would wave through, and the raw path must still refuse. That is the
    exact shape of the future in which the arm earns its keep — Starlette
    changing its ``root_path`` handling under us — and the failure it converts
    from "contract served anonymously" into "asked to log in".
    """
    monkeypatch.setattr("app.auth.gate._route_path", lambda scope: "/harmless")
    assert await _drive("/openapi.json") == 401
    assert await _drive("/api/config") == 401


def test_route_path_matches_starlettes_own_router() -> None:
    """Our stripping and Starlette's must agree, character for character.

    ``_route_path`` is deliberately a local copy rather than an import of
    ``starlette._utils.get_route_path`` (a private symbol a production import
    should not depend on). This is the pin that makes the copy safe: if
    Starlette changes the algorithm, this fails in CI instead of the gate
    quietly guarding a different path from the one the router matches.
    """
    from starlette._utils import get_route_path

    cases = [
        ("/api/config", ""),
        ("/musicdrop/api/config", "/musicdrop"),
        ("/musicdrop/api/health", "/musicdrop"),
        ("/musicdrop", "/musicdrop"),
        ("/musicdropX/api/config", "/musicdrop"),  # not a segment boundary
        ("/api/config", "/other"),  # prefix does not match at all
        ("/", ""),
        ("//api/config", "/musicdrop"),
    ]
    for path, root_path in cases:
        scope: Scope = {"type": "http", "path": path, "root_path": root_path}
        assert _route_path(scope) == get_route_path(scope), (path, root_path)


def test_the_doc_surface_constant_tracks_the_apps_real_openapi_url() -> None:
    """Nothing else pins the ``/openapi.json`` literal against the live app.

    Change ``openapi_url`` on the FastAPI constructor and the contract moves to
    a path the gate does not know about — anonymous again, with no other test
    noticing.
    """
    assert real_app.openapi_url in DOC_SURFACE_PATHS


# --------------------------------------------------------------------------
# Rotating the password evicts every live session
# --------------------------------------------------------------------------


def test_rotating_the_password_hash_invalidates_a_live_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leaked password must be revocable, and the only lever is the hash.

    There is no server-side session store to sweep, so the signing key is
    derived from the password hash instead: change the hash and every
    outstanding cookie stops verifying. Without that binding a stolen cookie
    outlives the credential it came from by up to 30 days.
    """
    client = TestClient(real_app)  # holds a cookie bound to the CURRENT hash
    assert client.get(_GATED_SERVEABLE).status_code == 200

    monkeypatch.setattr("app.config.settings.password_hash", "scrypt$1024$8$1$c2FsdA==$ZGlnZXN0")
    assert client.get(_GATED_SERVEABLE).status_code == 401
    # ...and the exempt status endpoint agrees, rather than still reporting the
    # caller as signed in off a cookie the gate now refuses.
    assert client.get("/api/auth/status").json()["authenticated"] is False


def test_a_cookie_reminted_under_the_new_hash_works_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control for the test above: rotation invalidates, it does not brick.

    Without this, a mint function that returned constant garbage would pass the
    rotation test for entirely the wrong reason.
    """
    rotated = "scrypt$1024$8$1$c2FsdA==$ZGlnZXN0"
    monkeypatch.setattr("app.config.settings.password_hash", rotated)
    client = _anonymous()
    client.cookies.set(SESSION_COOKIE_NAME, mint_session_token(TEST_SESSION_SECRET, rotated))
    assert client.get(_GATED_SERVEABLE).status_code == 200


def test_the_binding_is_deterministic_for_one_hash() -> None:
    """Two tokens minted under the same hash both verify.

    Guards against a derivation that mixed in anything per-call — every cookie
    would then be a one-shot and a second browser tab would sign you out.
    """
    from app.auth.session import session_token_is_valid

    for _ in range(2):
        token = mint_session_token(TEST_SESSION_SECRET, settings.password_hash)
        assert session_token_is_valid(token, TEST_SESSION_SECRET, settings.password_hash)


# --------------------------------------------------------------------------
# Private responses must not be cached
# --------------------------------------------------------------------------


def test_a_gated_response_is_marked_uncacheable() -> None:
    resp = TestClient(real_app).get(_GATED_SERVEABLE)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert "Cookie" in resp.headers["vary"]


def test_a_401_is_marked_uncacheable() -> None:
    """The refusal too — a cached 401 would lock out a browser that has since
    signed in."""
    resp = _anonymous().get(_GATED_READ)
    assert resp.status_code == 401
    assert resp.headers["cache-control"] == "no-store"


#: One probe per member of ``AUTH_ROUTE_PATHS``, since they differ in method
#: and body. Checked for completeness below, so a member added without a probe
#: fails loudly instead of silently going unprobed.
_AUTH_ROUTE_PROBES: dict[str, Callable[[TestClient], object]] = {
    "/api/auth/status": lambda c: c.get("/api/auth/status"),
    "/api/auth/login": lambda c: c.post("/api/auth/login", json={"password": "wrong"}),
}


def test_every_exempt_auth_route_has_a_cache_probe() -> None:
    """The parametrization below is only as good as its coverage of the set."""
    assert set(_AUTH_ROUTE_PROBES) == set(AUTH_ROUTE_PATHS)


@pytest.mark.parametrize("path", sorted(AUTH_ROUTE_PATHS))
def test_the_exempt_auth_routes_are_marked_uncacheable(path: str) -> None:
    """Per-caller even though the gate exempts them.

    ``status`` reports whether THIS browser is signed in — a shared cache that
    stored one browser's "authenticated: true" would hand it to the next — and
    ``login`` is the one response that carries the session token. Parametrized
    over the set rather than probing ``status`` alone, because with only that
    one probe, dropping ``login`` from ``AUTH_ROUTE_PATHS`` left 189 tests green
    and shipped the token-bearing response with no cache directives at all.
    """
    resp = _AUTH_ROUTE_PROBES[path](_anonymous())
    assert isinstance(resp, Response)
    assert resp.headers["cache-control"] == "no-store"
    assert "Cookie" in resp.headers["vary"]


def test_the_spa_shell_is_not_marked_uncacheable() -> None:
    """The blanket must not reach the static bundle — ``/assets/*`` is served
    ``immutable`` with a content hash, and no-store would refetch it forever."""
    from app.auth.gate import scope_is_private

    for path in ("/", "/assets/index-abc123.js", "/favicon.svg"):
        assert scope_is_private({"type": "http", "path": path, "root_path": ""}) is False


def test_an_endpoints_own_cache_control_is_not_overwritten() -> None:
    """Where-absent, so ETag revalidation survives.

    The three image endpoints serve a content-hash ETag with their own
    ``Cache-Control: no-cache``, which is what lets a browser revalidate and get
    a bodiless 304. A blanket ``no-store`` would forbid storing the image at all
    and turn every repaint into a full re-download.
    """
    from app.auth.gate import _mark_uncacheable

    stamped = dict(_mark_uncacheable([(b"cache-control", b"no-cache"), (b"etag", b'"abc"')]))
    assert stamped[b"cache-control"] == b"no-cache"
    assert stamped[b"vary"] == b"Cookie"


def test_an_existing_vary_is_extended_not_replaced() -> None:
    from app.auth.gate import _mark_uncacheable

    stamped = dict(_mark_uncacheable([(b"vary", b"Accept-Encoding")]))
    assert stamped[b"vary"] == b"Accept-Encoding, Cookie"
    # ...and Cookie is not appended twice when it is already named.
    twice = dict(_mark_uncacheable([(b"vary", b"Accept-Encoding, Cookie")]))
    assert twice[b"vary"] == b"Accept-Encoding, Cookie"
