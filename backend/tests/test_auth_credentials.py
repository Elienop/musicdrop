"""The pieces underneath the endpoints: the hash format, the token, the secret file.

Split from ``test_auth_api.py`` because these are unit-level and one of them —
``test_a_generated_hash_uses_the_owasp_parameters`` — deliberately pays the real
~0.16 s derive that every other auth test shortcuts around.
"""

from __future__ import annotations

import base64
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from app.auth.gate import auth_posture, boot_auth_posture
from app.auth.hash_password import main
from app.auth.passwords import (
    PasswordHashError,
    hash_password,
    password_is_configured,
    verify_password,
)
from app.auth.session import (
    SESSION_MAX_AGE_SECONDS,
    load_or_create_session_secret,
    mint_session_token,
    session_secret_path,
    session_token_is_valid,
)
from app.auth.source import password_file_location
from app.playlists.atomic import write_atomic_bytes
from tests.conftest import low_cost_stored_hash

_SECRET = b"0123456789abcdef0123456789abcdef"
#: The hash a token is bound to. Spelled at every call site below rather
#: than defaulted, because WHICH hash a token was minted under is now part
#: of what these tests are about.
_HASH = "scrypt$1024$8$1$c2FsdA==$ZGlnZXN0"


# --------------------------------------------------------------------------
# the password hash
# --------------------------------------------------------------------------


def test_a_generated_hash_round_trips() -> None:
    stored = hash_password("hunter2")
    assert verify_password("hunter2", stored) is True
    assert verify_password("hunter3", stored) is False


def test_a_generated_hash_uses_the_owasp_parameters() -> None:
    """The one test that pins the REAL work factors.

    OWASP's Password Storage Cheat Sheet, "scrypt": N=2^17 (128 MiB), r=8
    (1024 bytes), p=1. Every other auth test builds its hash at n=1024 for
    speed, so without this the production cost could be dropped to nothing and
    the suite would stay green.
    """
    algorithm, n, r, p, salt, digest = hash_password("x").split("$")
    assert algorithm == "scrypt"
    assert (int(n), int(r), int(p)) == (2**17, 8, 1)
    # NIST SP 800-132 section 5.1: at least 128 bits of random salt.
    assert len(base64.b64decode(salt)) >= 16
    assert len(base64.b64decode(digest)) == 32


def test_two_hashes_of_one_password_differ() -> None:
    """A random salt per hash — otherwise the value is a rainbow-table lookup."""
    first = hash_password("same")
    second = hash_password("same")
    assert first != second


def test_a_hash_is_verified_with_its_OWN_parameters_not_the_current_ones() -> None:
    """What makes the format future-proof: raising the cost keeps old hashes valid.

    A verify that used this module's constants instead of the stored ones would
    log the owner out the day the work factors were raised.
    """
    import hashlib

    n, r, p = 1024, 8, 1
    salt = b"sixteen-byte-slt"
    digest = hashlib.scrypt(
        b"old", salt=salt, n=n, r=r, p=p, maxmem=128 * r * (n + p + 2) + 1024, dklen=32
    )
    legacy = (
        f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"
    )
    assert verify_password("old", legacy) is True


@pytest.mark.parametrize(
    "broken",
    [
        "",
        "   ",
        "scrypt",
        "scrypt$1024$8$1$c2FsdA==",
        "bcrypt$1024$8$1$c2FsdA==$ZGlnZXN0",
        "scrypt$0$8$1$c2FsdA==$ZGlnZXN0",
        "scrypt$1024$0$1$c2FsdA==$ZGlnZXN0",
        "scrypt$1024$8$1$c2FsdA==$",
    ],
)
def test_verify_raises_rather_than_returning_false_on_a_broken_hash(broken: str) -> None:
    """A refusal the caller must SEE, not one that looks like a wrong password.

    ``verify_password`` returning False for an unreadable hash would let the
    route answer "Incorrect password." for a server misconfiguration, and the
    operator would spend the afternoon retyping a password that was right.
    """
    with pytest.raises(PasswordHashError):
        verify_password("anything", broken)


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("", False),
        ("    ", False),
        ("scrypt$nonsense", False),
        ("scrypt$1024$8$1$c2FsdA==$ZGlnZXN0", True),
    ],
)
def test_password_is_configured(stored: str, expected: bool) -> None:
    assert password_is_configured(stored) is expected


# --------------------------------------------------------------------------
# the session token
# --------------------------------------------------------------------------


def test_a_minted_token_verifies() -> None:
    assert session_token_is_valid(mint_session_token(_SECRET, _HASH), _SECRET, _HASH) is True


def test_a_token_does_not_verify_under_another_secret() -> None:
    assert session_token_is_valid(mint_session_token(_SECRET, _HASH), b"b" * 32, _HASH) is False


def test_an_expired_token_does_not_verify() -> None:
    token = mint_session_token(_SECRET, _HASH, max_age_seconds=-1)
    assert session_token_is_valid(token, _SECRET, _HASH) is False


def test_a_token_expiring_in_a_second_still_verifies() -> None:
    """The near side of the boundary, so "expired" cannot be read as "always"."""
    token = mint_session_token(_SECRET, _HASH, max_age_seconds=5)
    assert session_token_is_valid(token, _SECRET, _HASH) is True


def test_the_default_lifetime_is_thirty_days() -> None:
    """The cookie's Max-Age and the token's embedded expiry come from ONE
    constant, so a session cannot outlive its cookie or vice versa."""
    assert SESSION_MAX_AGE_SECONDS == 30 * 24 * 60 * 60
    _, payload, _ = mint_session_token(_SECRET, _HASH).split(".")
    padded = payload + "=" * (-len(payload) % 4)
    expires_at = int(base64.urlsafe_b64decode(padded))
    assert abs(expires_at - (time.time() + SESSION_MAX_AGE_SECONDS)) < 5


@pytest.mark.parametrize(
    "token",
    [None, "", "garbage", "v1.only-two", "v1.a.b.c", "v9.MTAwMDAwMDAwMDA.sig"],
)
def test_a_malformed_token_does_not_verify(token: str | None) -> None:
    assert session_token_is_valid(token, _SECRET, _HASH) is False


def test_a_token_has_exactly_one_accepted_spelling() -> None:
    """Junk characters inside the base64 fields do not decode to a valid token.

    Python's base64 SILENTLY DISCARDS characters outside the alphabet unless
    ``validate=True``, so without it ``sig`` and ``si!!g`` decode to identical
    bytes and one credential has an unbounded family of accepted wire forms.
    The signature check would still reject a forgery either way — this is about
    a credential having one canonical spelling, which is what makes "did this
    exact cookie change?" answerable.

    Two junk characters, not one: inserting a single character leaves the field
    at a length lenient base64 rejects for padding anyway, so a one-character
    version would pass with or without the fix and prove nothing.
    """
    version, payload, signature = mint_session_token(_SECRET, _HASH).split(".")
    junked = f"{version}.{payload}.{signature[:5]}!!{signature[5:]}"

    # The premise: lenient decoding really would accept this as the same bytes.
    padded = junked.split(".")[2] + "=" * (-len(junked.split(".")[2]) % 4)
    assert base64.b64decode(padded, altchars=b"-_") == base64.urlsafe_b64decode(
        signature + "=" * (-len(signature) % 4)
    )

    assert session_token_is_valid(junked, _SECRET, _HASH) is False


def test_no_secret_means_no_valid_token() -> None:
    """Fail closed at the primitive too, not only in the middleware."""
    assert session_token_is_valid(mint_session_token(_SECRET, _HASH), b"", _HASH) is False


# --------------------------------------------------------------------------
# the signing secret on disk
# --------------------------------------------------------------------------


def test_the_secret_is_created_owner_only(tmp_path: Path) -> None:
    """0600 from the moment it exists.

    ``write_atomic_bytes`` passes the final mode to ``os.open``, so there is no
    create-then-chmod window in which the key is world-readable — the same
    treatment the Plex admin token gets.
    """
    path = session_secret_path(str(tmp_path))
    secret = load_or_create_session_secret(path)

    assert len(secret) == 32
    assert path.read_bytes() == secret
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_same_secret_is_returned_on_a_second_load(tmp_path: Path) -> None:
    """Sessions survive a restart — the whole reason it is persisted.

    A per-process key would sign the owner out on every container update.
    """
    path = session_secret_path(str(tmp_path))
    first = load_or_create_session_secret(path)
    assert load_or_create_session_secret(path) == first


def test_a_truncated_secret_file_is_replaced(tmp_path: Path, caplog: Any) -> None:
    """Signing with a short key would silently weaken every token.

    ``write_atomic_bytes`` cannot produce this, but a stray ``touch`` or a
    restore from an interrupted copy can, so it is treated as absent and said
    out loud rather than used.
    """
    path = session_secret_path(str(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"short")
    with caplog.at_level("WARNING"):
        secret = load_or_create_session_secret(path)
    assert len(secret) == 32
    assert secret != b"short"
    assert "session secret" in caplog.text


def test_the_secret_lands_beside_the_beets_config(tmp_path: Path) -> None:
    assert session_secret_path(str(tmp_path)) == tmp_path / "session-secret"


def test_a_racing_writer_wins_and_the_loser_adopts_its_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two processes reaching first-run together must converge on ONE key.

    Simulated by replacing the file between our write and our read-back, which
    is exactly what the losing side of the race observes. Drop the re-read and
    this returns the key nobody else has, so half the deployment would reject
    the other half's cookies.
    """
    path = session_secret_path(str(tmp_path))
    rival = b"r" * 32

    def write_then_lose(target: Path, data: bytes, *, mode: int = 0o644) -> None:
        write_atomic_bytes(target, data, mode=mode)
        write_atomic_bytes(target, rival, mode=mode)  # the other process replaces ours

    monkeypatch.setattr("app.auth.session.write_atomic_bytes", write_then_lose)
    assert load_or_create_session_secret(path) == rival


# --------------------------------------------------------------------------
# the generator CLI
# --------------------------------------------------------------------------


def _run_cli(stdin: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the generator CLI with ``stdin`` piped in.

    ``start_new_session=True`` is NOT optional and NOT tidiness. ``getpass``
    tries ``/dev/tty`` before stdin, and a subprocess inherits the controlling
    terminal — so run from a real shell (rather than a CI runner with no tty)
    these three tests read the OWNER'S KEYBOARD and hang until the per-test
    timeout, ~6 minutes of a suite that otherwise finishes in 30 seconds.
    ``setsid`` detaches the child, ``/dev/tty`` then fails to open, and getpass
    falls back to the pipe. Verified both ways under ``script(1)``: without it
    the child reports a tty, with it the child has none.
    """
    return subprocess.run(
        [sys.executable, "-m", "app.auth.hash_password"],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=cwd,
        env={**os.environ, "PYTHONWARNINGS": "ignore"},
        timeout=120,
        check=False,
        start_new_session=True,
    )


def test_the_cli_prints_a_hash_that_verifies() -> None:
    """Generate, then verify — the round trip an operator actually performs.

    Scriptable only because ``_run_cli`` detaches the child from the terminal;
    see its docstring for why piping alone is not enough.
    """
    backend = Path(__file__).resolve().parents[1]
    out = _run_cli("hunter2\nhunter2\n", backend)
    assert out.returncode == 0, out.stderr
    stored = out.stdout.strip()
    assert stored.startswith("scrypt$")
    assert verify_password("hunter2", stored) is True
    # The value alone on stdout, so `MUSICDROP_PASSWORD_HASH=$(...)` works;
    # the guidance goes to stderr.
    assert stored.count("\n") == 0
    assert "MUSICDROP_PASSWORD_HASH" in out.stderr
    # The compose form is on STDERR, beside the guidance and never on stdout:
    # a `scrypt$...` value pasted raw into docker-compose.yml usually arrives
    # unparseable (compose swallows letter-led fields; digit-led ones survive),
    # which is the measured way the set-but-UNREADABLE state is reached.
    assert stored.replace("$", "$$") in out.stderr
    assert "$$" in out.stderr


def test_the_cli_refuses_a_mismatched_confirmation() -> None:
    backend = Path(__file__).resolve().parents[1]
    out = _run_cli("one\ntwo\n", backend)
    assert out.returncode == 2
    assert out.stdout.strip() == ""
    assert "do not match" in out.stderr


def test_the_cli_refuses_an_empty_password() -> None:
    backend = Path(__file__).resolve().parents[1]
    out = _run_cli("\n\n", backend)
    assert out.returncode == 2
    assert out.stdout.strip() == ""
    assert "empty or only whitespace" in out.stderr


@pytest.mark.parametrize("blank", ["", "   ", "\t "])
def test_the_cli_refuses_a_blank_password_the_same_way_the_routes_do(
    blank: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Whitespace-only too, which is the half that used to be hashed.

    Measured before this: three spaces produced a working ``scrypt$...`` line and
    rc 0, while ``POST /api/auth/setup`` answered 422 for the same input — so
    ``app/models/auth.py`` (which ships into the OpenAPI contract) and README
    both described a rule the CLI did not apply. A credential nobody can retype
    on purpose is the thing that rule exists to prevent.

    Driven through the module's ``getpass`` seam rather than a subprocess: this
    is about the refusal, not about the terminal handling the three tests above
    cover.
    """
    monkeypatch.setattr("app.auth.hash_password.getpass", lambda _prompt: blank)

    assert main() == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "empty or only whitespace" in captured.err


# --------------------------------------------------------------------------
# the startup posture clause
# --------------------------------------------------------------------------


_WORKING_HASH = "scrypt$1024$8$1$c2FsdA==$ZGlnZXN0"


def test_posture_reports_a_working_hash_and_which_source_it_came_from() -> None:
    """The source is part of the clause now, because there are two of them.

    An operator who removed the compose line and cannot sign in has to be told
    whether the live hash is the env var or the stored file; with one string for
    both, the boot log could not answer that.
    """
    from_env = auth_posture(_WORKING_HASH, "env", stored_file_present=False)
    assert "password configured" in from_env
    assert "from the environment" in from_env

    from_file = auth_posture(_WORKING_HASH, "file", stored_file_present=True)
    assert "password configured" in from_file
    assert password_file_location() in from_file
    assert from_file != from_env


def test_posture_reports_an_unset_hash_and_names_the_env_var() -> None:
    line = auth_posture("", "none", stored_file_present=False)
    assert "NO password configured" in line
    assert "MUSICDROP_PASSWORD_HASH" in line
    # The fix moved: setup happens on the sign-in screen, and the env var is
    # now the override rather than the only way in.
    assert "setup form" in line


def test_posture_distinguishes_an_unreadable_hash_from_an_unset_one() -> None:
    """Four states, not two: each unreadable source needs its own fix.

    Collapse these arms and a fresh deploy with a mangled env var reports
    "NO password configured", so the operator sets it again — to the same
    mangled value.
    """
    unreadable = auth_posture("scrypt$oops", "env", stored_file_present=False)
    assert "UNREADABLE" in unreadable
    assert unreadable != auth_posture("", "none", stored_file_present=False)
    # The measured way a hash usually arrives mangled: pasted into
    # docker-compose with single dollars, where a $ before a letter or an
    # underscore starts a variable interpolation (a $ before a digit or a
    # symbol is left alone).
    assert "$$" in unreadable


def test_posture_tells_an_unreadable_stored_file_from_an_unreadable_env_var() -> None:
    """Different fix: delete the file and restart, versus correct the env var.

    The file arm must not tell the operator to double dollar signs in compose —
    they never typed the stored hash — and must name the path, because deleting
    it is the forgotten-password recovery.
    """
    line = auth_posture("scrypt$oops", "file", stored_file_present=True)
    assert "UNREADABLE" in line
    assert password_file_location() in line
    assert "deleted" in line
    assert line != auth_posture("scrypt$oops", "env", stored_file_present=False)


@pytest.mark.parametrize("env_hash", [_WORKING_HASH, "scrypt$oops"])
def test_posture_says_when_the_env_var_is_shadowing_a_stored_file(env_hash: str) -> None:
    """Both env arms, because the operator's next move is the same either way.

    Removing the compose line hands the password back to a file they may not
    know is there — or may be counting on. Naming it is what turns "my password
    stopped working" into one look at the boot log. The unreadable arm needs it
    too: that operator is already reaching for the compose file.
    """
    shadowing = auth_posture(env_hash, "env", stored_file_present=True)
    alone = auth_posture(env_hash, "env", stored_file_present=False)

    assert password_file_location() in shadowing
    assert "ignored" in shadowing
    assert password_file_location() not in alone
    assert alone in shadowing, "the shadowed line adds to the plain one rather than replacing it"


def test_the_boot_clause_reads_the_live_state(password_hash_file: Path) -> None:
    """What ``app/main.py`` logs at import: composed here, not at the call site.

    The call site used to unpack the resolver itself, and replacing that with two
    literals left every test green while the boot line said the opposite of the
    truth. One no-argument function is what makes the arguments testable.
    """
    assert "NO password configured" in boot_auth_posture()

    password_hash_file.parent.mkdir(parents=True, exist_ok=True)
    password_hash_file.write_text(low_cost_stored_hash("a stored password"), encoding="utf-8")

    configured = boot_auth_posture()
    assert "password configured from" in configured
    assert password_file_location() in configured


def test_the_boot_clause_names_a_file_the_env_var_shadows(
    password_hash_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state the two halves of ``effective_password`` cannot report.

    Under the env var the file is never consulted, so the pair says only
    ``("<hash>", "env")``. The presence has to be resolved separately or the
    clause silently drops the one fact the operator needs.
    """
    password_hash_file.parent.mkdir(parents=True, exist_ok=True)
    password_hash_file.write_text(low_cost_stored_hash("the stored one"), encoding="utf-8")
    monkeypatch.setattr("app.config.settings.password_hash", _WORKING_HASH)

    clause = boot_auth_posture()

    assert "from the environment" in clause
    assert password_file_location() in clause
