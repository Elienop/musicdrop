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

import logging
import stat
from pathlib import Path

import anyio
import httpx
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
#: How many browsers race for the account in the write-lock test. Eight is well
#: past the two the failure needs and still costs one scrypt derive, because the
#: seven that lose answer 409 before deriving anything.
_CONCURRENT_SETUPS = 8


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


def test_the_claim_is_logged_and_carries_no_secret(
    password_hash_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The one event an operator has to be able to see, and what it may not say.

    Until the file exists this instance belongs to whoever reaches it first, and
    deleting the file re-opens that on the RUNNING process — no restart, no
    other signal. Without this record the only trace of the claim is uvicorn's
    access line for a 200. WARNING because it is a one-way change of posture.

    The second half is the constraint: no record from ANY logger may carry the
    plaintext or the stored hash. Asserted over every record the request
    produced, not just this one, and over the arguments as well as the rendered
    message — a ``%r`` of the wrong value would otherwise pass.
    """
    with caplog.at_level(logging.DEBUG):
        resp = _anonymous().post(_SETUP, json={"password": _PASSWORD})

    assert resp.status_code == 200
    stored = password_hash_file.read_text(encoding="utf-8").strip()
    claims = [record for record in caplog.records if "first-run setup" in record.getMessage()]
    assert len(claims) == 1, [record.getMessage() for record in caplog.records]
    assert claims[0].levelno == logging.WARNING
    assert str(password_hash_file) in claims[0].getMessage()
    _assert_no_secret_in(caplog.records, secrets=(_PASSWORD, stored))


def _assert_no_secret_in(records: list[logging.LogRecord], *, secrets: tuple[str, ...]) -> None:
    """No record anywhere carries the plaintext or the stored hash.

    Both the rendered message and the raw arguments, because a record is not
    rendered until a handler formats it: a secret passed as an argument and
    never formatted is still a secret sitting in whatever handler the operator
    configured.
    """
    for record in records:
        rendered = f"{record.getMessage()} {record.args!r}"
        for secret in secrets:
            assert secret not in rendered, f"{secret!r} leaked into {record.name}: {rendered}"


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


@pytest.mark.anyio
async def test_only_one_of_many_simultaneous_first_run_setups_wins(
    password_hash_file: Path,
) -> None:
    """First WINS, rather than first-ish: the check and the write are one step.

    The atomic writer publishes with ``os.replace``, which is last-writer-wins
    rather than create-or-fail, so without the lock in ``app/api/auth.py`` every
    caller passes the "no source" check and the last write survives. Measured
    with the lock replaced by a null context: two callers both got 200, and the
    FIRST one was handed a cookie for a password that was no longer stored —
    told it was signed in, and locked out on its next request.

    One event loop for all of them (``httpx.ASGITransport``), because that is
    what an ``asyncio.Lock`` serialises; two TestClients would each run in their
    own loop and contend on nothing.

    Only two of the passwords are verified against the stored hash rather than
    all of them: each verify pays a real ~0.16 s scrypt derive, and the winner
    plus one loser is what the claim needs.
    """
    passwords = [f"first-run passphrase number {index}" for index in range(_CONCURRENT_SETUPS)]
    transport = httpx.ASGITransport(app=real_app)
    answers: dict[str, httpx.Response] = {}

    async def claim(password: str) -> None:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            answers[password] = await client.post(_SETUP, json={"password": password})

    async with anyio.create_task_group() as group:
        for password in passwords:
            group.start_soon(claim, password)

    statuses = sorted(response.status_code for response in answers.values())
    assert statuses == [200] + [409] * (_CONCURRENT_SETUPS - 1), statuses
    with_cookie = [
        password for password, response in answers.items() if "set-cookie" in response.headers
    ]
    assert len(with_cookie) == 1, "more than one caller was told it now holds the account"

    winner = next(password for password, response in answers.items() if response.status_code == 200)
    assert with_cookie == [winner]
    loser = next(password for password in passwords if password != winner)
    # anyio.Path, not Path: a blocking read inside an async test is what
    # ruff's ASYNC240 is about, and the rule is right even here.
    stored = (await anyio.Path(password_hash_file).read_text(encoding="utf-8")).strip()
    assert verify_password(winner, stored) is True
    assert verify_password(loser, stored) is False


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


def test_setup_is_refused_when_the_path_is_a_DANGLING_symlink(
    password_hash_file: Path,
) -> None:
    """A link that resolves to nothing is still an entry the operator put there.

    The shape an operator lands in when the file is a link into a secrets mount
    that has not been mounted yet. Setup publishes with ``os.replace``, which
    drops the link and stores a password where they meant to read one — so the
    answer has to be 409, and the link has to survive it.
    """
    password_hash_file.parent.mkdir(parents=True, exist_ok=True)
    password_hash_file.symlink_to(password_hash_file.parent / "not-mounted-yet")

    resp = _anonymous().post(_SETUP, json={"password": _PASSWORD})

    assert resp.status_code == 409
    assert password_hash_file.is_symlink()
    assert not password_hash_file.exists()  # still dangling: nothing was written


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


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"password": ["a-plaintext-marker"]}, id="wrong-type"),
        pytest.param({"password": {"value": "a-plaintext-marker"}}, id="wrong-type-object"),
    ],
)
def test_a_shape_422_does_not_read_the_password_back(
    body: dict[str, object], password_hash_file: Path
) -> None:
    """A body pydantic refuses must not carry the plaintext into the response.

    The blank-password 422 is the app's own and answers with a sentence, but a
    body of the WRONG SHAPE is refused by pydantic, which puts the rejected
    value in each row by default. Reachable by anyone who can reach the port:
    ``/api/auth/setup`` is gate-exempt. Dropped for every route in
    ``app/wire.py`` rather than for a named list of password fields.
    """
    resp = _anonymous().post(_SETUP, json=body)

    assert resp.status_code == 422
    assert "a-plaintext-marker" not in resp.text
    # The message is still there, which is what the sign-in form renders.
    assert resp.json()["detail"][0]["msg"]
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
