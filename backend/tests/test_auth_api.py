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
import time

import anyio
import pytest
from fastapi.testclient import TestClient

from app.auth.session import SESSION_COOKIE_NAME, SESSION_MAX_AGE_SECONDS
from app.config import settings
from app.main import app as real_app
from tests.conftest import TEST_SESSION_SECRET

_PASSWORD = "correct horse battery staple"
_LOGIN = "/api/auth/login"
_LOGOUT = "/api/auth/logout"
_STATUS = "/api/auth/status"
_GATED = "/openapi.json"


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
    # NOT Secure, deliberately: MusicDrop is browsed over plain HTTP by LAN IP,
    # and a Secure cookie would never be sent there — the app could not be
    # signed into at all. A recorded, accepted residual (README, "Authentication").
    assert "Secure" not in header


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
    assert resp.json() == {"detail": "incorrect password"}
    assert "set-cookie" not in resp.headers


def test_an_unset_hash_refuses_every_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail CLOSED with no password configured — the same posture the slskd
    webhook takes when its secret is unset."""
    monkeypatch.setattr("app.config.settings.password_hash", "")
    resp = _anonymous().post(_LOGIN, json={"password": "anything"})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "no password is configured on this server"}
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
    assert resp.json() == {"detail": "the configured password hash is not readable"}


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
    assert resp.json() == {"detail": "the configured password hash is not readable"}


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
    assert resp.json() == {"detail": "the session signing secret is unavailable"}
    assert "set-cookie" not in resp.headers


def test_a_verify_that_cannot_get_a_turn_is_refused_rather_than_queued(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounded wait, and that it really is bounded.

    A derive slower than the wait window stands in for "someone else already
    has the turn". The caller must be told to come back rather than queue
    behind it — an unbounded queue would let a burst turn every login into a
    multi-second hang.
    """
    from app.api import auth as auth_module

    wait = 0.05
    derive = 0.6
    monkeypatch.setattr("app.api.auth._LOCK_WAIT_SECONDS", wait)

    def slow_verify(candidate: str, stored: str) -> bool:
        time.sleep(derive)
        return True

    monkeypatch.setattr(auth_module, "verify_password", slow_verify)

    started = time.monotonic()
    resp = _anonymous().post(_LOGIN, json={"password": _PASSWORD})
    waited = time.monotonic() - started

    assert resp.status_code == 429
    assert resp.json() == {"detail": "another sign-in attempt is in progress"}
    # It WAITED (a bare non-blocking acquire would 429 a double-clicked Sign
    # in)...
    assert waited >= wait
    # ...and it GAVE UP rather than blocking for the whole derive.
    assert waited < derive


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
