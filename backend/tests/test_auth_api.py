"""``/api/auth/login``, ``/api/auth/logout`` and ``/api/auth/status``.

Most tests here build their stored hash with :func:`_stored_hash`, which writes
the ``scrypt$...`` wire format out by hand at a deliberately tiny work factor.
Two reasons, and the first is not speed:

* it is a LITERAL reconstruction of the format, so a change to the separator,
  the field order or the algorithm tag breaks these tests instead of moving
  silently with the production code;
* n=1024 makes a verify ~1 ms instead of ~160 ms, which is the difference
  between a fast file and twenty tests spending three seconds in a KDF.

``test_auth_credentials.py`` pins what ``hash_password()`` itself writes, so the
real OWASP parameters are not left untested by the shortcut.
"""

from __future__ import annotations

import base64
import hashlib
import threading
import time

import anyio
import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth.session import SESSION_COOKIE_NAME, SESSION_MAX_AGE_SECONDS
from app.config import settings
from app.main import app as real_app
from tests.conftest import TEST_SESSION_SECRET, session_cookie_value

_PASSWORD = "correct horse battery staple"
_LOGIN = "/api/auth/login"
_LOGOUT = "/api/auth/logout"
_STATUS = "/api/auth/status"
_GATED = "/openapi.json"
#: The cookie attribute this file spends most of its assertions on, and the
#: header that decides it over plain HTTP.
_SECURE_FLAG = "Secure"
_PROTO_HEADER = "X-Forwarded-Proto"


def _stored_hash(password: str, *, n: int = 1024, r: int = 8, p: int = 1) -> str:
    salt = b"sixteen-byte-slt"
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=128 * r * (n + p + 2) + 1024 * 1024,
        dklen=32,
    )
    return "$".join(
        (
            "scrypt",
            str(n),
            str(r),
            str(p),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
    )


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MUSICDROP_PASSWORD_HASH`` set to a hash of :data:`_PASSWORD`.

    Patched on the settings SINGLETON, not through the environment: that is how
    the rest of the suite overrides configuration, and it is also what proves
    the login route reads ``settings.password_hash`` at REQUEST time rather
    than capturing it at import.
    """
    monkeypatch.setattr("app.config.settings.password_hash", _stored_hash(_PASSWORD))


def _anonymous() -> TestClient:
    client = TestClient(real_app)
    client.cookies.clear()
    return client


# --------------------------------------------------------------------------
# login
# --------------------------------------------------------------------------


def test_the_right_password_sets_the_session_cookie(configured: None) -> None:
    """All five cookie attributes, asserted on the raw Set-Cookie header.

    Read off the header rather than the cookie jar because the jar drops the
    attributes after parsing them — the flags are the security properties here,
    so they have to be checked as SENT.
    """
    resp = _anonymous().post(_LOGIN, json={"password": _PASSWORD})
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": True, "password_set": True}

    header = resp.headers["set-cookie"]
    assert header.startswith(f"{SESSION_COOKIE_NAME}=")
    assert "HttpOnly" in header
    assert "SameSite=lax" in header
    assert "Path=/" in header
    assert f"Max-Age={SESSION_MAX_AGE_SECONDS}" in header
    # No Secure, because THIS request came over http — the flag is conditional
    # on the request's scheme, not a constant (app/auth/cookies.py). Marking it
    # here would make the by-IP LAN deployment impossible to sign into, since a
    # Secure cookie is never sent back over plain HTTP. The https arms are
    # pinned below.
    assert _SECURE_FLAG not in header

    # The TOKEN's own expiry must match the Max-Age the header just claimed.
    # These are two independent values: the cookie passes max_age explicitly
    # while the token takes its lifetime from mint_session_token's keyword
    # DEFAULT, so a call site passing max_age_seconds=60 would leave a browser
    # holding a "30-day" cookie that the server rejects after a minute — a
    # 43,200x drift, and fully green before this assertion existed.
    max_age = int(header.split("Max-Age=")[1].split(";")[0])
    token = resp.cookies[SESSION_COOKIE_NAME]
    payload = token.split(".")[1]
    embedded_expiry = int(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert abs(embedded_expiry - (time.time() + max_age)) < 5

    # The one response that carries the session token may never be stored.
    # Asserted HERE, in the same response as the Set-Cookie, because the two
    # properties are what make each other matter.
    assert resp.headers["cache-control"] == "no-store"


def test_a_login_over_real_tls_gets_a_secure_cookie(configured: None) -> None:
    """The direct-HTTPS arm, with no forwarding header in play at all.

    ``base_url="https://..."`` genuinely flips ``request.url.scheme`` (verified:
    the ASGI scope's ``scheme`` is ``"https"``), and TestClient sends no
    ``X-Forwarded-Proto`` of its own — so this arm exercises the scheme branch
    alone, the way a container terminating TLS itself, or one behind a proxy
    uvicorn already trusts, would arrive.
    """
    client = TestClient(real_app, base_url="https://testserver")
    client.cookies.clear()
    resp = client.post(_LOGIN, json={"password": _PASSWORD})
    assert resp.status_code == 200
    assert _SECURE_FLAG in resp.headers["set-cookie"]


@pytest.mark.parametrize(
    ("forwarded", "expect_secure"),
    [
        ("https", True),
        # Case-insensitive: the header is a token, and proxies differ.
        ("HTTPS", True),
        # By convention the LEFTMOST element is the browser's own hop, the way
        # X-Forwarded-For reads — whether a given proxy appends, overwrites or
        # passes a client value through is its own config (app/auth/cookies.py).
        ("https, http", True),
        ("  https ,http", True),
        ("http", False),
        # The mutation that matters: a substring test reads this as https,
        # and mints a Secure cookie for a browser that arrived over plain HTTP
        # and can therefore never send it back.
        ("http, https", False),
        ("", False),
    ],
)
def test_the_forwarded_proto_header_decides_the_secure_flag(
    forwarded: str, expect_secure: bool, configured: None
) -> None:
    """Over plain HTTP, ``X-Forwarded-Proto``'s first element decides it.

    Trusted rather than validated, deliberately: a client that gets this header
    wrong only affects its OWN cookie — either locking itself out with a
    ``Secure`` cookie it cannot send back, or silently downgrading its own
    session to a plain one. See ``app/auth/cookies.py`` for both modes, and for
    why this differs from the host guard's treatment of ``X-Forwarded-Host``.
    """
    resp = _anonymous().post(
        _LOGIN, json={"password": _PASSWORD}, headers={_PROTO_HEADER: forwarded}
    )
    assert resp.status_code == 200
    assert (_SECURE_FLAG in resp.headers["set-cookie"]) is expect_secure


@pytest.mark.parametrize(
    ("first", "second", "expect_secure"),
    [
        ("https", "http", True),
        # The silent self-downgrade: a TLS-fronted session whose cookie comes
        # back without Secure, because the value the browser's own proxy set is
        # not the one read.
        ("http", "https", False),
    ],
)
def test_duplicate_forwarded_proto_headers_are_decided_by_the_FIRST_one(
    first: str, second: str, expect_secure: bool, configured: None
) -> None:
    """Two separate headers, not one comma chain — first-header-wins.

    A proxy that APPENDS its own ``X-Forwarded-Proto`` rather than rewriting a
    client-supplied one sends the value twice, and Starlette's ``Headers.get``
    returns the FIRST occurrence. So the second arm here is a genuinely
    TLS-fronted request that mints a non-Secure cookie — the self-downgrade
    ``app/auth/cookies.py`` names, pinned so the two arms cannot quietly swap.
    """
    resp = _anonymous().post(
        _LOGIN,
        json={"password": _PASSWORD},
        headers=[(_PROTO_HEADER, first), (_PROTO_HEADER, second)],
    )
    assert resp.status_code == 200
    assert (_SECURE_FLAG in resp.headers["set-cookie"]) is expect_secure


def test_the_issued_cookie_actually_opens_a_gated_route(configured: None) -> None:
    """The round trip, not just the header: log in, then read something gated.

    A cookie with the right flags and a signature the gate rejects would pass
    the test above and fail here.
    """
    client = _anonymous()
    assert client.get(_GATED).status_code == 401
    assert client.post(_LOGIN, json={"password": _PASSWORD}).status_code == 200
    # The jar kept what the response set; no cookie is passed by hand.
    assert client.get(_GATED).status_code == 200


def test_the_wrong_password_is_refused(configured: None) -> None:
    resp = _anonymous().post(_LOGIN, json={"password": _PASSWORD + "!"})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Incorrect password."}
    assert "set-cookie" not in resp.headers


def test_an_unset_hash_refuses_every_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail CLOSED with no password configured — the same posture the slskd
    webhook takes when its secret is unset."""
    monkeypatch.setattr("app.config.settings.password_hash", "")
    resp = _anonymous().post(_LOGIN, json={"password": "anything"})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "No password is configured on this server."}
    assert "set-cookie" not in resp.headers


def test_an_unreadable_hash_says_so_rather_than_blaming_the_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A distinct sentence, because the fix is different.

    "Not configured" and "configured but unreadable" both refuse every
    password, but one is "set MUSICDROP_PASSWORD_HASH" and the other is
    "replace the one you set". The operator is the only user here and sees this
    sentence verbatim in the login form.
    """
    monkeypatch.setattr("app.config.settings.password_hash", "argon2id$v=19$whatever")
    resp = _anonymous().post(_LOGIN, json={"password": _PASSWORD})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "The configured password hash is not readable."}


@pytest.mark.parametrize(
    "broken",
    [
        "scrypt$16384$8",  # too few fields
        "scrypt$notanumber$8$1$c2FsdA==$ZGlnZXN0",
        "scrypt$16385$8$1$c2FsdA==$ZGlnZXN0",  # n is not a power of two
        "scrypt$16384$8$1$!!!!$ZGlnZXN0",  # salt is not base64
        "scrypt$16384$8$1$$ZGlnZXN0",  # empty salt
        "scrypt$1073741824$8$1$c2FsdA==$ZGlnZXN0",  # n demands ~1 TiB
        "plaintextpassword",
    ],
)
def test_every_shape_of_broken_hash_refuses(broken: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """None of these may 500, hang, or allocate — each is a config error.

    The oversized ``n`` matters most: honouring a stored parameter is what
    makes the format future-proof, and it is also what would let a typo'd
    exponent turn every login attempt into a gigabyte allocation.
    """
    monkeypatch.setattr("app.config.settings.password_hash", broken)
    resp = _anonymous().post(_LOGIN, json={"password": _PASSWORD})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "The configured password hash is not readable."}


def test_login_refuses_when_the_server_has_no_signing_secret(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """503, not a cookie signed with something invented on the spot.

    Seeded illegal state (startup never completed). The password below is
    CORRECT, so this fails on the honest reading and only on that: a 200 here
    would mean the route minted a key no later request could verify against.
    """
    monkeypatch.delattr(real_app.state, "session_secret")
    resp = _anonymous().post(_LOGIN, json={"password": _PASSWORD})
    assert resp.status_code == 503
    assert resp.json() == {"detail": "The session signing secret is unavailable."}
    assert "set-cookie" not in resp.headers


@pytest.mark.anyio
async def test_a_verify_that_cannot_get_a_turn_is_refused_rather_than_queued(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounded wait, over HTTP, with the turn genuinely held by someone else.

    The contention is created by taking the limiter's one token on behalf of a
    sentinel borrower — the honest simulation of "another sign-in is mid-derive".

    It used to be created by making THIS request's own derive slower than the
    window, which passed for the wrong reason: the window cancelled the caller's
    own in-flight derive. That is precisely the behaviour that unbounded the
    memory ceiling (see ``_verify_serialised``), so the old shape of this test
    would now report a 200 — the window bounds the WAIT for a turn, and a caller
    that gets one runs to completion however long that takes.
    """
    from app.api import auth as auth_module

    wait = 0.05
    derive = 0.4
    monkeypatch.setattr("app.api.auth._LOCK_WAIT_SECONDS", wait)

    def slow_verify(candidate: str, stored: str) -> bool:
        time.sleep(derive)
        return True

    monkeypatch.setattr(auth_module, "verify_password", slow_verify)

    # One event loop for both callers. Two TestClients would each run in their
    # OWN loop, and a CapacityLimiter signals its waiters with asyncio.Events
    # bound to the loop that created them — releasing from one loop while
    # another waits is not sound, so the contention has to be built in-loop.
    transport = httpx.ASGITransport(app=real_app)
    results: dict[str, httpx.Response] = {}
    elapsed: dict[str, float] = {}

    async def attempt(tag: str, delay: float) -> None:
        await anyio.sleep(delay)
        began = time.monotonic()
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            results[tag] = await client.post(_LOGIN, json={"password": _PASSWORD})
        elapsed[tag] = time.monotonic() - began

    async with anyio.create_task_group() as group:
        group.start_soon(attempt, "first", 0.0)
        group.start_soon(attempt, "second", wait / 2)

    # The one that got the turn ran to completion, however long that took.
    assert results["first"].status_code == 200
    assert elapsed["first"] >= derive

    # The one that did not was told to come back rather than queued behind it.
    assert results["second"].status_code == 429
    assert results["second"].json() == {
        "detail": "Another sign-in is already in progress. Try again in a moment."
    }
    # It WAITED (a bare non-blocking acquire would 429 a double-clicked Sign in)...
    assert elapsed["second"] >= wait
    # ...and it gave up rather than blocking for the whole derive.
    assert elapsed["second"] < derive
    assert auth_module._VERIFY_LIMITER.borrowed_tokens == 0


@pytest.mark.anyio
async def test_only_one_password_derive_runs_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The serialization invariant, asserted directly on concurrent callers.

    This is what the limiter is FOR: each scrypt derive holds ~128 MiB, so four
    overlapping ones would allocate half a gigabyte. Raise the limiter's
    capacity and ``max(concurrent)`` goes above 1 and this fails; the 429 test
    above would not notice.
    """
    from app.api import auth as auth_module

    concurrent = 0
    high_water: list[int] = []

    def recording_verify(candidate: str, stored: str) -> bool:
        nonlocal concurrent
        concurrent += 1
        high_water.append(concurrent)
        time.sleep(0.02)
        concurrent -= 1
        return True

    monkeypatch.setattr(auth_module, "verify_password", recording_verify)
    async with anyio.create_task_group() as group:
        for _ in range(4):
            group.start_soon(auth_module._verify_serialised, "pw", "stored")

    assert len(high_water) == 4, "every call ran"
    assert max(high_water) == 1, f"derives overlapped: {high_water}"
    assert auth_module._VERIFY_LIMITER.borrowed_tokens == 0


@pytest.mark.anyio
async def test_a_derive_that_outlives_the_window_still_blocks_the_next_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The serialization invariant when callers GIVE UP — where it used to fail.

    The test above uses a derive far shorter than the window, so nothing ever
    times out and the abandonment path is never taken. That is exactly the case
    that was broken: anyio holds the limiter as ``async with limiter:`` around
    the ``await future``, so abandoning a slow derive released the token while
    the worker thread kept running, and the next caller was admitted on top of
    it. Steady-state concurrency was ``derive / window``, not 1 — measured at
    peak 3 with a 0.6 s derive and a 0.2 s window, i.e. ~384 MiB of scrypt.

    Callers arrive STAGGERED, one just after the previous one gives up, because
    a simultaneous burst cannot show this: the losers are cancelled while still
    waiting for admission, so they never start a derive and the peak is 1 even
    with the bug present.
    """
    from app.api import auth as auth_module

    window = 0.1
    derive = 0.35
    monkeypatch.setattr("app.api.auth._LOCK_WAIT_SECONDS", window)

    lock = threading.Lock()
    live = 0
    peak = 0
    started = 0

    def slow_verify(candidate: str, stored: str) -> bool:
        nonlocal live, peak, started
        with lock:
            live += 1
            started += 1
            peak = max(peak, live)
        time.sleep(derive)
        with lock:
            live -= 1
        return True

    monkeypatch.setattr(auth_module, "verify_password", slow_verify)

    refusals: list[float] = []

    async def caller(delay: float) -> None:
        await anyio.sleep(delay)
        began = time.monotonic()
        try:
            await auth_module._verify_serialised("pw", "stored")
        except HTTPException as exc:
            assert exc.status_code == 429
            refusals.append(time.monotonic() - began)

    async with anyio.create_task_group() as group:
        for i in range(5):
            group.start_soon(caller, i * (window + 0.02))

    assert peak == 1, f"derives overlapped: peak {peak} of {started} started"
    # ...and the callers that could not get in were REFUSED promptly, rather
    # than the invariant being held by nobody ever getting a turn.
    assert refusals, "no caller was refused, so nothing contended"
    assert max(refusals) < derive, f"a refusal waited for the derive: {refusals}"
    assert auth_module._VERIFY_LIMITER.borrowed_tokens == 0


@pytest.mark.anyio
async def test_a_disconnecting_caller_cannot_abandon_its_derive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other way a caller gives up: it goes away mid-derive.

    ``move_on_after`` only wraps the ADMISSION, so the wait timeout can no
    longer abandon a running derive — but a client disconnect cancels the whole
    request task, and that WOULD unwind the derive's await if it were not
    shielded. The token would be released by the ``finally`` while the
    un-interruptible worker thread carried on, and the next caller would be
    admitted on top of it: the same unbounded ceiling as F1, reached by hanging
    up instead of by waiting.

    ``run_sync``'s default ``abandon_on_cancel=False`` is what shields it.
    Passing ``True`` leaves every other test in this file green (measured), so
    this is the only thing standing between that default and a silent revert.
    """
    from app.api import auth as auth_module

    derive = 0.3
    live = 0
    lock = threading.Lock()

    def slow_verify(candidate: str, stored: str) -> bool:
        nonlocal live
        with lock:
            live += 1
        time.sleep(derive)
        with lock:
            live -= 1
        return True

    monkeypatch.setattr(auth_module, "verify_password", slow_verify)

    began = time.monotonic()
    with anyio.move_on_after(0.05):
        await auth_module._verify_serialised("pw", "stored")
    waited = time.monotonic() - began

    # The cancellation did not take effect until the derive had finished...
    assert waited >= derive, f"the derive was abandoned after {waited:.3f}s"
    # ...so no worker is still holding ~128 MiB behind a freed token.
    with lock:
        assert live == 0
    assert auth_module._VERIFY_LIMITER.borrowed_tokens == 0


def test_a_refused_login_releases_its_turn(configured: None) -> None:
    """A refusal must not keep the turn — every later login would 429.

    Pins the ``finally``-equivalent: the limiter is released whichever way the
    verify ends. Hold it open and the second login below cannot get a turn.
    """
    from app.api import auth as auth_module

    client = _anonymous()
    assert client.post(_LOGIN, json={"password": "wrong"}).status_code == 401
    assert auth_module._VERIFY_LIMITER.borrowed_tokens == 0
    assert client.post(_LOGIN, json={"password": _PASSWORD}).status_code == 200


def test_login_rejects_a_body_without_a_password(configured: None) -> None:
    """A Pydantic model at the boundary, never a raw dict."""
    assert _anonymous().post(_LOGIN, json={}).status_code == 422
    assert _anonymous().post(_LOGIN, json={"password": 7}).status_code == 422


# --------------------------------------------------------------------------
# logout
# --------------------------------------------------------------------------


def test_logout_expires_the_cookie_and_the_client_is_locked_out(
    configured: None,
) -> None:
    """The full arc: signed out client -> in -> out -> refused again."""
    client = _anonymous()
    assert client.post(_LOGIN, json={"password": _PASSWORD}).status_code == 200
    assert client.get(_GATED).status_code == 200

    resp = client.post(_LOGOUT)
    assert resp.status_code == 204
    header = resp.headers["set-cookie"]
    assert header.startswith(f"{SESSION_COOKIE_NAME}=")
    assert "Max-Age=0" in header
    assert "Path=/" in header
    # The jar honoured the expiry, so the next request carries nothing.
    assert client.cookies.get(SESSION_COOKIE_NAME) is None
    assert client.get(_GATED).status_code == 401


def test_logout_matches_logins_secure_posture(configured: None) -> None:
    """The expiring cookie carries the same conditional Secure flag.

    Both arms, because only the pair proves the flag is being computed rather
    than hardcoded: the plain-HTTP request must not grow one and the forwarded
    HTTPS request must.

    This symmetry is load-bearing, and the plain-HTTP arm is the one that
    matters. A Set-Cookie carrying Secure that arrives over a non-secure
    connection is ignored entirely rather than stored, so it expires nothing
    (draft-ietf-httpbis-rfc6265bis-20 section 5.7, "Storage Model", step 13) —
    a logout hardcoded to secure=True would return 204 over plain HTTP while
    leaving the session cookie live.

    What is asserted here is still only the SYMMETRY, not that browser rule.
    http.cookiejar applies no secure check when storing a cookie
    (DefaultCookiePolicy has set_ok_path and set_ok_domain but no
    set_ok_secure), so this client would accept a cookie a browser drops, and
    any test claiming to pin the browser behaviour would be pinning nothing.
    See the block above ``delete_cookie`` in ``app/api/auth.py``.
    """
    plain = _anonymous()
    assert plain.post(_LOGIN, json={"password": _PASSWORD}).status_code == 200
    assert _SECURE_FLAG not in plain.post(_LOGOUT).headers["set-cookie"]

    # The full arc over real TLS: the jar sends the Secure cookie back, so this
    # logout is a genuinely authenticated one rather than a 401.
    over_tls = TestClient(real_app, base_url="https://testserver")
    over_tls.cookies.clear()
    assert over_tls.post(_LOGIN, json={"password": _PASSWORD}).status_code == 200
    assert _SECURE_FLAG in over_tls.post(_LOGOUT).headers["set-cookie"]

    # And the forwarded arm, which is the production shape: browser -> TLS
    # proxy -> plain internal hop. The cookie is seeded onto the jar by hand
    # rather than taken from a login response, because a jar that took one
    # would refuse to send it back over http — that refusal IS the Secure flag
    # working, and it would turn this logout into a 401 about nothing.
    forwarded = _anonymous()
    forwarded.cookies.set(SESSION_COOKIE_NAME, session_cookie_value())
    resp = forwarded.post(_LOGOUT, headers={_PROTO_HEADER: "https"})
    assert resp.status_code == 204
    assert _SECURE_FLAG in resp.headers["set-cookie"]


def test_logout_is_itself_gated() -> None:
    """Not on the exempt list: it goes through the same gate as everything else,
    so there is exactly one place a session is checked."""
    assert _anonymous().post(_LOGOUT).status_code == 401


def test_a_token_copied_before_logout_still_works(configured: None) -> None:
    """The honest limit of a stateless session, pinned rather than implied.

    Logout expires the cookie in the BROWSER; the token stays signed and unexpired
    because there is no server-side session record to revoke. This test exists so
    the limitation is a documented, deliberate property instead of a surprise
    during a future security review.
    """
    client = _anonymous()
    client.post(_LOGIN, json={"password": _PASSWORD})
    stolen = client.cookies[SESSION_COOKIE_NAME]
    client.post(_LOGOUT)

    attacker = _anonymous()
    attacker.cookies.set(SESSION_COOKIE_NAME, stolen)
    assert attacker.get(_GATED).status_code == 200


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def test_status_reports_signed_out_with_a_password_available(configured: None) -> None:
    assert _anonymous().get(_STATUS).json() == {"authenticated": False, "password_set": True}


def test_status_reports_signed_in(configured: None) -> None:
    client = _anonymous()
    client.post(_LOGIN, json={"password": _PASSWORD})
    assert client.get(_STATUS).json() == {"authenticated": True, "password_set": True}


def test_status_reports_no_password_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both fields false — what slice 2's login screen renders "set a password" from."""
    monkeypatch.setattr("app.config.settings.password_hash", "")
    assert _anonymous().get(_STATUS).json() == {"authenticated": False, "password_set": False}


def test_status_reports_an_unreadable_hash_as_no_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.config.settings.password_hash", "scrypt$oops")
    assert _anonymous().get(_STATUS).json()["password_set"] is False


def test_status_agrees_with_the_gate_about_an_expired_cookie(
    configured: None,
) -> None:
    """Status must never claim "signed in" about a cookie the gate would refuse.

    It shares the gate's own predicate rather than re-deriving the answer; a
    second copy of the check is exactly how the two would drift.
    """
    from app.auth.session import mint_session_token

    client = _anonymous()
    client.cookies.set(
        SESSION_COOKIE_NAME,
        mint_session_token(TEST_SESSION_SECRET, settings.password_hash, max_age_seconds=-1),
    )
    assert client.get(_STATUS).json()["authenticated"] is False
    assert client.get(_GATED).status_code == 401
