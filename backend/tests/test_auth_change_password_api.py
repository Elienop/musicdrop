"""``POST /api/auth/password`` — change the stored password from Settings.

Gated, and it still asks for the current password: the session cookie is a
30-day bearer token with no server-side record, so possession of one must not be
enough to replace the credential it was minted from.

Two properties get most of the attention here:

* a wrong current password is a **403, not a 401**. The client treats any 401 on
  a gated path as "your session ended" and drops the user at the sign-in screen,
  so a 401 would turn a typo into a sign-out;
* changing the password signs every OTHER session out, because the session
  signing key is derived from the password hash
  (``app/auth/session.py::_signing_key``) — the only revocation this stateless
  design has. The caller's own cookie is re-minted so the browser that made the
  change stays in.

The stored file is pinned at a tmp path for every test by
``tests/conftest.py::password_hash_file``; ``tests/test_password_file_isolation.py``
proves that floor is not vacuous.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth.passwords import verify_password
from app.auth.session import SESSION_COOKIE_NAME
from app.main import app as real_app
from tests.conftest import low_cost_stored_hash

_CHANGE = "/api/auth/password"
_LOGIN = "/api/auth/login"
_GATED = "/openapi.json"
_OLD = "correct horse battery staple"
_NEW = "a different long passphrase"


def _stored(password_hash_file: Path, password: str) -> None:
    """Seed the file source, the way ``POST /api/auth/setup`` leaves it."""
    password_hash_file.parent.mkdir(parents=True, exist_ok=True)
    password_hash_file.write_text(low_cost_stored_hash(password) + "\n", encoding="utf-8")


def _signed_in() -> TestClient:
    """A client whose cookie is minted under the CURRENT effective hash.

    ``tests/conftest.py`` stamps the cookie at construction time, so the stored
    hash has to be seeded first — the same rule a real browser lives under.
    """
    return TestClient(real_app)


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


def test_the_password_changes_and_the_caller_stays_signed_in(
    password_hash_file: Path,
) -> None:
    _stored(password_hash_file, _OLD)
    client = _signed_in()

    resp = client.post(_CHANGE, json={"current_password": _OLD, "new_password": _NEW})

    assert resp.status_code == 200
    assert resp.json() == {
        "authenticated": True,
        "password_set": True,
        "password_source": "file",
    }
    stored = password_hash_file.read_text(encoding="utf-8").strip()
    assert verify_password(_NEW, stored) is True
    assert verify_password(_OLD, stored) is False
    # The re-minted cookie is bound to the NEW hash, so the same client keeps
    # working: without the re-mint the browser that made the change would be
    # signed out by its own success.
    assert client.get(_GATED).status_code == 200


def test_the_change_is_logged_and_carries_no_secret(
    password_hash_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A record an operator can find, at INFO — this is an expected action.

    Setup logs at WARNING because it is a one-way change of the instance's
    posture; a change is routine, and its value is in the timeline (every other
    browser was signed out at that moment). Neither carries the plaintext or the
    hash, and that is asserted over every record the request produced.
    """
    _stored(password_hash_file, _OLD)
    client = _signed_in()

    with caplog.at_level(logging.DEBUG):
        resp = client.post(_CHANGE, json={"current_password": _OLD, "new_password": _NEW})

    assert resp.status_code == 200
    stored = password_hash_file.read_text(encoding="utf-8").strip()
    changes = [
        record
        for record in caplog.records
        if "the stored password was changed" in record.getMessage()
    ]
    assert len(changes) == 1, [record.getMessage() for record in caplog.records]
    assert changes[0].levelno == logging.INFO
    assert str(password_hash_file) in changes[0].getMessage()
    for record in caplog.records:
        rendered = f"{record.getMessage()} {record.args!r}"
        for secret in (_OLD, _NEW, stored):
            assert secret not in rendered, f"{secret!r} leaked into {record.name}: {rendered}"


def test_only_the_new_password_signs_in_afterwards(password_hash_file: Path) -> None:
    _stored(password_hash_file, _OLD)
    client = _signed_in()
    assert (
        client.post(_CHANGE, json={"current_password": _OLD, "new_password": _NEW}).status_code
        == 200
    )

    anonymous = _signed_in()
    anonymous.cookies.clear()
    assert anonymous.post(_LOGIN, json={"password": _OLD}).status_code == 401
    assert anonymous.post(_LOGIN, json={"password": _NEW}).status_code == 200


def test_every_other_live_session_is_signed_out(password_hash_file: Path) -> None:
    """The blunt revocation a stateless session design has, pinned as intended.

    The other browser's cookie was signed under the old hash, so the gate stops
    accepting it on its very next request. The UI says so; this is what makes
    that copy true.
    """
    _stored(password_hash_file, _OLD)
    other = _signed_in()
    assert other.get(_GATED).status_code == 200

    changer = _signed_in()
    assert (
        changer.post(_CHANGE, json={"current_password": _OLD, "new_password": _NEW}).status_code
        == 200
    )

    assert other.get(_GATED).status_code == 401


def test_the_response_sets_a_new_cookie(password_hash_file: Path) -> None:
    """A different token, with the same attributes login sends.

    Read off the raw header rather than the jar: the jar holds the conftest's
    stamped cookie beside the one this response set, and httpx refuses an
    ambiguous lookup across two domains.
    """
    _stored(password_hash_file, _OLD)
    client = _signed_in()
    before = client.cookies.get(SESSION_COOKIE_NAME, domain="")

    resp = client.post(_CHANGE, json={"current_password": _OLD, "new_password": _NEW})

    header = resp.headers["set-cookie"]
    assert header.startswith(f"{SESSION_COOKIE_NAME}=")
    assert "HttpOnly" in header
    assert "SameSite=lax" in header
    minted = header.split(";")[0].split("=", 1)[1]
    assert minted != before


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


def test_a_wrong_current_password_is_a_403_and_NOT_a_401(
    password_hash_file: Path,
) -> None:
    """A typo must not read as "you have been signed out".

    ``frontend/src/api/client.ts`` flips the auth store on any 401 that is not
    on the gate-exempt list, so a 401 here would clear the session and bounce
    the operator to the sign-in screen mid-form. The stored hash is untouched
    and the caller's session survives.
    """
    _stored(password_hash_file, _OLD)
    client = _signed_in()
    before = password_hash_file.read_text(encoding="utf-8")

    resp = client.post(_CHANGE, json={"current_password": "not it", "new_password": _NEW})

    assert resp.status_code == 403
    assert "current password" in resp.json()["detail"]
    assert password_hash_file.read_text(encoding="utf-8") == before
    assert client.get(_GATED).status_code == 200


def test_the_route_is_gated(password_hash_file: Path) -> None:
    """Not on the exempt list: a cookie-less caller never reaches the handler,
    so the current-password check is a second factor rather than the only one."""
    _stored(password_hash_file, _OLD)
    client = _signed_in()
    client.cookies.clear()

    resp = client.post(_CHANGE, json={"current_password": _OLD, "new_password": _NEW})

    assert resp.status_code == 401
    assert password_hash_file.read_text(encoding="utf-8").strip() != ""


def test_the_env_override_refuses_the_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """409, even though the UI hides the form: the API is the contract.

    Writing the file here would store a password the env var shadows — the
    operator would set a new one, be told it worked, and still have to use the
    old one.
    """
    monkeypatch.setattr("app.config.settings.password_hash", low_cost_stored_hash(_OLD))
    client = _signed_in()

    resp = client.post(_CHANGE, json={"current_password": _OLD, "new_password": _NEW})

    assert resp.status_code == 409
    assert "MUSICDROP_PASSWORD_HASH" in resp.json()["detail"]


def test_a_server_with_no_password_has_nothing_to_change() -> None:
    """409 and a sentence pointing at setup, not a 403 blaming the operator's
    typing: there is nothing to verify the current password against."""
    client = _signed_in()

    resp = client.post(_CHANGE, json={"current_password": _OLD, "new_password": _NEW})

    assert resp.status_code == 409
    assert "no password to change" in resp.json()["detail"]


def test_an_unreadable_stored_hash_refuses_the_change(password_hash_file: Path) -> None:
    """Refusing is the honest answer.

    Accepting would let a caller REPLACE a hash they could not have proved they
    knew — the session cookie alone would be enough, which is the property the
    current-password field exists to deny.
    """
    password_hash_file.parent.mkdir(parents=True, exist_ok=True)
    password_hash_file.write_text("scrypt$oops", encoding="utf-8")
    client = _signed_in()

    resp = client.post(_CHANGE, json={"current_password": _OLD, "new_password": _NEW})

    assert resp.status_code == 409
    # The whole sentence, not just the status. Collapsing this arm onto the
    # "nothing to change" one left the suite green while the panel told an
    # operator with a corrupt hash file to go and set a password on the sign-in
    # screen, which answers 409 as well. The recovery here is deleting the file,
    # and it is the same sentence a login refusal gives — one state, one fix.
    assert resp.json() == {
        "detail": (
            "The stored password hash on this server is not readable. "
            "Delete the password-hash file in the beets directory and restart "
            "MusicDrop to set a new password."
        )
    }
    assert password_hash_file.read_text(encoding="utf-8") == "scrypt$oops"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"current_password": "a-plaintext-marker"}, id="new-password-missing"),
        pytest.param(
            {"current_password": ["a-plaintext-marker"], "new_password": _NEW},
            id="current-password-wrong-type",
        ),
    ],
)
def test_a_shape_422_does_not_read_the_password_back(
    body: dict[str, object], password_hash_file: Path
) -> None:
    """A missing or mistyped field must not echo the password that WAS sent.

    The missing-field row is the one worth spelling out: pydantic reports it
    against the whole body, so the value of every OTHER field — here the current
    password — travelled back to the caller in the 422. Dropped app-wide in
    ``app/wire.py``; see the twin in ``test_auth_setup_api.py``.
    """
    _stored(password_hash_file, _OLD)
    client = _signed_in()

    resp = client.post(_CHANGE, json=body)

    assert resp.status_code == 422
    assert "a-plaintext-marker" not in resp.text
    assert resp.json()["detail"][0]["msg"]


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_a_blank_new_password_is_refused(blank: str, password_hash_file: Path) -> None:
    """The same rule setup and the CLI keep, and it is checked BEFORE the
    current password: a blank new password is a form error, not a credential
    one, and answering 403 would send the operator hunting the wrong field."""
    _stored(password_hash_file, _OLD)
    client = _signed_in()
    before = password_hash_file.read_text(encoding="utf-8")

    resp = client.post(_CHANGE, json={"current_password": _OLD, "new_password": blank})

    assert resp.status_code == 422
    assert password_hash_file.read_text(encoding="utf-8") == before


def test_a_server_with_no_signing_secret_changes_nothing(
    password_hash_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused, and the OLD password still works.

    MEASURED, not assumed: the answer is the GATE's 401, not the route's 503,
    because the gate fails closed on a missing ``app.state.session_secret`` and
    runs first. So the route's own illegal-state arm is defence in depth on this
    path (it is what ``POST /api/auth/setup``, which is gate-exempt, really
    answers — see ``tests/test_auth_setup_api.py``). What matters either way is
    that a server which cannot sign a cookie does not rewrite the credential.
    """
    _stored(password_hash_file, _OLD)
    client = _signed_in()
    before = password_hash_file.read_text(encoding="utf-8")
    monkeypatch.delattr(real_app.state, "session_secret")

    resp = client.post(_CHANGE, json={"current_password": _OLD, "new_password": _NEW})

    assert resp.status_code == 401
    assert password_hash_file.read_text(encoding="utf-8") == before


def test_a_write_failure_leaves_the_old_password_working(
    password_hash_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """503 with a full sentence, and nothing half-written."""
    _stored(password_hash_file, _OLD)
    client = _signed_in()
    before = password_hash_file.read_text(encoding="utf-8")

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("app.auth.source.write_atomic_text", refuse)

    resp = client.post(_CHANGE, json={"current_password": _OLD, "new_password": _NEW})

    assert resp.status_code == 503
    assert "could not be saved" in resp.json()["detail"]
    assert password_hash_file.read_text(encoding="utf-8") == before
    assert client.get(_GATED).status_code == 200
