"""``POST /api/auth/setup`` — the first password, on a server that has none.

The route is gate-EXEMPT by necessity (there is no credential to hold a session
with yet) and therefore has to refuse itself the moment any password source
exists. Both halves are pinned here: the way in on first run, and the 409 on
every other state, including the one where the configured hash cannot be read.

The stored file lives under ``settings.beets_dir``, which points at the
developer's real library on a dev box — ``tests/conftest.py``'s autouse
``password_hash_file`` fixture pins it at a tmp path for every test in the suite,
and ``tests/test_password_file_isolation.py`` proves that floor is not vacuous.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth.passwords import verify_password
from app.auth.session import SESSION_COOKIE_NAME, SESSION_MAX_AGE_SECONDS
from app.main import app as real_app
from tests.conftest import low_cost_stored_hash

_SETUP = "/api/auth/setup"
_LOGIN = "/api/auth/login"
_STATUS = "/api/auth/status"
_GATED = "/openapi.json"
_PASSWORD = "correct horse battery staple"


def _anonymous() -> TestClient:
    """A client with no session cookie — what a first-run browser is."""
    client = TestClient(real_app)
    client.cookies.clear()
    return client


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


def test_setup_stores_the_hash_and_signs_the_caller_in(password_hash_file: Path) -> None:
    """One request: the password is stored, and the browser holds a session.

    Returning a cookie rather than bouncing to the sign-in form matters — the
    operator has just typed the password into a form that is REPLACING the
    sign-in screen, and sending them back to it with no explanation reads as a
    failure.
    """
    resp = _anonymous().post(_SETUP, json={"password": _PASSWORD})

    assert resp.status_code == 200
    assert resp.json() == {
        "authenticated": True,
        "password_set": True,
        "password_source": "file",
    }
    stored = password_hash_file.read_text(encoding="utf-8").strip()
    assert verify_password(_PASSWORD, stored) is True


def test_the_stored_hash_is_owner_only(password_hash_file: Path) -> None:
    """0600 from the moment it exists — the credential is never world-readable."""
    assert _anonymous().post(_SETUP, json={"password": _PASSWORD}).status_code == 200
    assert stat.S_IMODE(password_hash_file.stat().st_mode) == 0o600


def test_the_cookie_setup_issues_is_the_one_login_issues() -> None:
    """Same Set-Cookie block, so the session has the same properties.

    Read off the raw header rather than the jar, which drops the attributes
    after parsing them — the flags are the security properties here.
    """
    resp = _anonymous().post(_SETUP, json={"password": _PASSWORD})

    header = resp.headers["set-cookie"]
    assert header.startswith(f"{SESSION_COOKIE_NAME}=")
    assert "HttpOnly" in header
    assert "SameSite=lax" in header
    assert "Path=/" in header
    assert f"Max-Age={SESSION_MAX_AGE_SECONDS}" in header
    # No Secure, because THIS request came over http (app/auth/cookies.py).
    assert "Secure" not in header


def test_the_session_setup_issues_opens_a_gated_route() -> None:
    """The cookie is real, not decorative: it is signed under the hash just
    written, which is what the gate now resolves."""
    client = _anonymous()
    assert client.post(_SETUP, json={"password": _PASSWORD}).status_code == 200
    assert client.get(_GATED).status_code == 200


def test_the_new_password_signs_in_afterwards() -> None:
    """The end-to-end proof that the file is a real credential source: log in
    against it through the ordinary login route."""
    assert _anonymous().post(_SETUP, json={"password": _PASSWORD}).status_code == 200

    resp = _anonymous().post(_LOGIN, json={"password": _PASSWORD})
    assert resp.status_code == 200
    assert resp.json()["password_source"] == "file"


def test_status_stops_offering_setup_once_it_has_run() -> None:
    """What the login screen branches on, before and after."""
    assert _anonymous().get(_STATUS).json()["password_source"] == "none"
    assert _anonymous().post(_SETUP, json={"password": _PASSWORD}).status_code == 200
    assert _anonymous().get(_STATUS).json() == {
        "authenticated": False,
        "password_set": True,
        "password_source": "file",
    }


def test_setup_is_reachable_without_a_session() -> None:
    """It is on the gate's exempt list, so a cookie-less browser reaches the
    ROUTE — the 409 below is the route's own answer, not the gate's 401."""
    resp = _anonymous().post(_SETUP, json={"password": ""})
    assert resp.status_code != 401


# --------------------------------------------------------------------------
# 409: a source already exists
# --------------------------------------------------------------------------


def test_setup_is_refused_once_a_hash_is_stored(password_hash_file: Path) -> None:
    """First wins. The second caller is told, and the stored hash is untouched.

    409 rather than 403: this is a state conflict, and the client shows the
    sentence verbatim.
    """
    first = _anonymous().post(_SETUP, json={"password": _PASSWORD})
    assert first.status_code == 200
    stored = password_hash_file.read_text(encoding="utf-8")

    second = _anonymous().post(_SETUP, json={"password": "a different password"})

    assert second.status_code == 409
    assert "already configured" in second.json()["detail"]
    assert password_hash_file.read_text(encoding="utf-8") == stored
    assert "set-cookie" not in second.headers


def test_setup_is_refused_while_the_env_var_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """The override is active, so the app does not own the password."""
    monkeypatch.setattr("app.config.settings.password_hash", low_cost_stored_hash(_PASSWORD))

    resp = _anonymous().post(_SETUP, json={"password": "something else"})

    assert resp.status_code == 409
    assert "MUSICDROP_PASSWORD_HASH" in resp.json()["detail"]


def test_setup_is_refused_when_the_env_var_is_set_but_UNREADABLE(
    password_hash_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's ruling, and the arm the obvious implementation gets wrong.

    ``password_set`` is false here — nothing can authenticate — so a route that
    keyed setup off "is a password usable?" would happily create a second
    credential on exactly the deployment whose env var was mangled by compose
    interpolation. Setup is keyed off the SOURCE instead.
    """
    monkeypatch.setattr("app.config.settings.password_hash", "scrypt$oops")
    assert _anonymous().get(_STATUS).json()["password_set"] is False

    resp = _anonymous().post(_SETUP, json={"password": _PASSWORD})

    assert resp.status_code == 409
    assert not password_hash_file.exists()


def test_setup_is_refused_when_the_stored_file_is_UNREADABLE(
    password_hash_file: Path,
) -> None:
    """A file that exists and cannot be read is "configured", never "absent".

    Otherwise the recovery for a corrupted hash file would be an overwrite by
    whoever reached the setup form first, rather than the deliberate "delete it
    and restart".
    """
    password_hash_file.mkdir(parents=True)  # unreadable for every uid, no chmod needed

    resp = _anonymous().post(_SETUP, json={"password": _PASSWORD})

    assert resp.status_code == 409
    assert password_hash_file.is_dir()


# --------------------------------------------------------------------------
# 422 / 503
# --------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", " ", "\t\n  "])
def test_a_blank_password_is_refused(blank: str, password_hash_file: Path) -> None:
    """The CLI's rule, and the only password policy either writing route has.

    ``hash_password`` refuses an empty password; whitespace-only is the same
    mistake with a stray keystroke, and it would be accepted by every later
    login (the login route does not strip).
    """
    resp = _anonymous().post(_SETUP, json={"password": blank})

    assert resp.status_code == 422
    assert not password_hash_file.exists()


def test_setup_refuses_when_the_server_has_no_signing_secret(
    password_hash_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """503, and NOTHING written.

    Seeded illegal state (startup never completed). Storing the password before
    discovering the cookie cannot be signed would leave a server whose password
    is set and whose setup form is closed, with nobody signed in.
    """
    monkeypatch.delattr(real_app.state, "session_secret")

    resp = _anonymous().post(_SETUP, json={"password": _PASSWORD})

    assert resp.status_code == 503
    assert "set-cookie" not in resp.headers
    assert not password_hash_file.exists()


def test_setup_reports_a_write_failure_rather_than_a_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read-only or full data directory is an operator problem, not a crash.

    Patched at the sink rather than by chmod-ing a directory, because the suite
    can run as root (the shipped image's shell is), where a mode change is not
    an obstacle at all.
    """

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("app.auth.source.write_atomic_text", refuse)

    resp = _anonymous().post(_SETUP, json={"password": _PASSWORD})

    assert resp.status_code == 503
    assert "could not be saved" in resp.json()["detail"]
    assert "set-cookie" not in resp.headers
