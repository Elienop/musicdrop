"""The two password sources and the rule that picks between them.

``app/auth/source.py`` answers one question — "what hash is live, and where did
it come from?" — and every reader in the app asks it: the gate on every request,
the login route, the status endpoint, the boot log, and the suite's own cookie
minting (``tests/conftest.py::session_cookie_value``).

The rule under test is the owner's, and it is the opposite of every other
file-vs-env store in this repo (``app/plex/config.py`` lets the file win): a
non-empty ``MUSICDROP_PASSWORD_HASH`` wins, **including when its value cannot be
read**. That last clause is the one worth pinning hardest, because the naive
reading — "nothing usable is configured, so offer setup" — opens first-run setup
on exactly the incident the feature exists for, and the operator ends up with a
second credential the corrected env var later shadows.

Nothing here touches a real library: ``tests/conftest.py``'s autouse
``password_hash_file`` fixture pins the file at a per-test tmp path, and
``tests/test_password_file_isolation.py`` is the decoy pin proving that floor is
not vacuous.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
from pathlib import Path

import pytest

from app.auth.source import (
    MAX_HASH_FILE_BYTES,
    PASSWORD_HASH_FILENAME,
    PasswordSource,
    effective_password,
    password_file_location,
    password_hash_path,
    stored_password_file_is_present,
    write_password_hash,
)

_ENV_HASH = "scrypt$1024$8$1$c2FsdA==$ZmFrZS1lbnY="
_FILE_HASH = "scrypt$1024$8$1$c2FsdA==$ZmFrZS1maWxl"

#: How long the FIFO test waits before calling the read blocked. Generous
#: enough that a loaded machine cannot fail it by being slow, and short enough
#: that the failure arrives while someone is still watching.
_READ_DEADLINE_SECONDS = 5.0


def _store(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# where the file lives
# --------------------------------------------------------------------------


def test_the_hash_file_lands_beside_the_beets_config(tmp_path: Path) -> None:
    """The one directory the operator already mounts and already backs up.

    Same reasoning as ``session-secret`` next door, and the same shape, so a
    restore of the beets dir brings the credential with it.
    """
    assert password_hash_path(str(tmp_path)) == tmp_path / "password-hash"
    assert PASSWORD_HASH_FILENAME == "password-hash"


def test_the_reported_location_is_the_file_actually_read(password_hash_file: Path) -> None:
    """The boot log and the refusal messages must name the file being read.

    ``password_file_location`` exists because a caller that imported
    ``live_password_hash_path`` by name would hold a stale binding and could
    name a different file from the one this module reads.
    """
    assert password_file_location() == str(password_hash_file)


# --------------------------------------------------------------------------
# precedence
# --------------------------------------------------------------------------


def test_no_source_at_all_reports_none(password_hash_file: Path) -> None:
    """The only state that admits first-run setup."""
    assert not password_hash_file.exists()
    assert effective_password() == ("", "none")


def test_a_stored_file_is_the_source_when_the_env_var_is_unset(
    password_hash_file: Path,
) -> None:
    _store(password_hash_file, _FILE_HASH + "\n")
    assert effective_password() == (_FILE_HASH, "file")


def test_the_env_var_wins_over_a_stored_file(
    password_hash_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both present: the env var decides, and the file is shadowed untouched.

    Untouched matters — an operator who removes the compose line gets the
    stored password back rather than a server with none.
    """
    _store(password_hash_file, _FILE_HASH)
    monkeypatch.setattr("app.config.settings.password_hash", _ENV_HASH)

    assert effective_password() == (_ENV_HASH, "env")
    assert password_hash_file.read_text(encoding="utf-8") == _FILE_HASH


def test_an_unreadable_env_var_still_wins_over_a_readable_file(
    password_hash_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's ruling, and the arm the naive implementation gets wrong.

    A hash pasted into docker-compose with single dollars usually arrives
    mangled. If
    that fell through to the file — or to ``"none"`` — the server would either
    authenticate against a password the operator thought they had replaced, or
    offer to set a brand new one. It reports ``"env"`` with an unusable hash
    instead: refuse, and say which of the two things to fix.
    """
    _store(password_hash_file, _FILE_HASH)
    monkeypatch.setattr("app.config.settings.password_hash", "scrypt")

    assert effective_password() == ("scrypt", "env")


def test_a_whitespace_only_env_var_is_not_a_source(
    password_hash_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``MUSICDROP_PASSWORD_HASH=""`` is indistinguishable from unset.

    pydantic-settings defaults the field to ``""``, so "set" can only mean
    non-empty after stripping — otherwise a compose file with the line present
    and blank would lock the app out of its own setup form forever.
    """
    _store(password_hash_file, _FILE_HASH)
    monkeypatch.setattr("app.config.settings.password_hash", "   \n\t ")

    assert effective_password() == (_FILE_HASH, "file")


def test_the_file_is_read_on_EVERY_call(password_hash_file: Path) -> None:
    """No caching: the setup and change routes rewrite this file while the
    process runs, and a captured value would leave the new password unusable
    until a restart."""
    assert effective_password() == ("", "none")
    _store(password_hash_file, _FILE_HASH)
    assert effective_password() == (_FILE_HASH, "file")
    _store(password_hash_file, _ENV_HASH)
    assert effective_password() == (_ENV_HASH, "file")


# --------------------------------------------------------------------------
# a file that exists but cannot be read is NOT "absent"
# --------------------------------------------------------------------------


def test_an_empty_file_reports_the_file_source_with_no_hash(
    password_hash_file: Path,
) -> None:
    """Refuse, do not re-open setup.

    Treating a blank file as absent would let setup overwrite it, and the
    documented recovery for a forgotten password is deliberately "delete the
    file and restart" — so a file that is there always means "configured".
    """
    _store(password_hash_file, "")
    assert effective_password() == ("", "file")


def test_a_directory_where_the_file_belongs_reports_the_file_source(
    password_hash_file: Path,
) -> None:
    """An unreadable file, tested without a chmod.

    ``chmod 000`` proves nothing when the tests run as root (the container's
    ``docker exec`` shell is root, and so is CI in the shipped image), whereas
    reading a DIRECTORY raises ``IsADirectoryError`` for every uid. It is an
    ``OSError``, so this also pins that the catch-all arm reports "file"
    rather than crashing a status request with a 500.
    """
    password_hash_file.mkdir(parents=True)
    assert effective_password() == ("", "file")


def test_bytes_that_are_not_utf8_report_the_file_source(
    password_hash_file: Path,
) -> None:
    """``UnicodeDecodeError`` is a ``ValueError``, NOT an ``OSError``.

    Caught explicitly for that reason: an ``except OSError`` alone would let a
    binary file (a truncated copy, a restored blob) escape as a 500 out of
    ``GET /api/auth/status``, which is a gate-exempt endpoint anyone who can
    reach the port can call.
    """
    password_hash_file.parent.mkdir(parents=True, exist_ok=True)
    password_hash_file.write_bytes(b"scrypt$1024$8$1$\xff\xfe$x")
    assert effective_password() == ("", "file")


def test_a_missing_parent_directory_is_absent_not_unreadable(
    password_hash_file: Path,
) -> None:
    """First run in a fresh data dir: nothing exists yet, so setup is available."""
    assert not password_hash_file.parent.exists()
    assert effective_password() == ("", "none")


# --------------------------------------------------------------------------
# what is at the path is INSPECTED before it is opened
# --------------------------------------------------------------------------


def test_a_fifo_at_the_path_answers_instead_of_blocking(
    password_hash_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A FIFO is refused on its SHAPE, without opening it.

    ``read_text`` on a FIFO with no writer blocks in ``open`` — not an error,
    not a timeout, just a thread that never comes back. This resolver runs
    synchronously inside the session gate on every gated request, so one such
    entry in the beets directory would park the event loop rather than answer
    401. Measured on this branch before the shape check existed: the call
    returned only when a signal interrupted the open.

    Run on a thread with a deadline: a regression here HANGS rather than
    failing, and a hang inside the test would take the whole suite with it. The
    thread is a daemon so a still-blocked one cannot hold up interpreter exit.
    """
    password_hash_file.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(password_hash_file)
    answered: list[tuple[str, PasswordSource]] = []

    def read_it() -> None:
        answered.append(effective_password())

    worker = threading.Thread(target=read_it, daemon=True)
    with caplog.at_level(logging.WARNING, logger="app.auth.source"):
        worker.start()
        worker.join(timeout=_READ_DEADLINE_SECONDS)

    assert not worker.is_alive(), (
        f"the resolver was still inside the FIFO after {_READ_DEADLINE_SECONDS}s"
    )
    assert answered == [("", "file")]
    # %r of the path, never %s: the beets directory is operator-controlled, and
    # a newline or an ANSI escape in a name could otherwise forge a log line.
    assert repr(password_hash_file) in caplog.text


def test_a_symlink_to_a_device_reports_the_file_source_and_says_why(
    password_hash_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-regular entry that CAN be opened is still refused.

    ``/dev/null`` reads as an empty string, so the answer alone would look the
    same with no shape check at all — the warning is what makes this test
    measure the check rather than the coincidence.
    """
    password_hash_file.parent.mkdir(parents=True, exist_ok=True)
    password_hash_file.symlink_to("/dev/null")

    with caplog.at_level(logging.WARNING, logger="app.auth.source"):
        assert effective_password() == ("", "file")

    assert "not a regular file" in caplog.text
    assert repr(password_hash_file) in caplog.text


def test_a_dangling_symlink_is_present_not_absent(
    password_hash_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The link EXISTS, so this is "configured and unreadable", not "no password".

    Reporting ``"none"`` would offer first-run setup, and setup publishes with
    ``os.replace``, which drops the link and stores a password where the
    operator had pointed at a secrets mount.
    """
    password_hash_file.parent.mkdir(parents=True, exist_ok=True)
    password_hash_file.symlink_to(password_hash_file.parent / "not-mounted-yet")

    with caplog.at_level(logging.WARNING, logger="app.auth.source"):
        assert effective_password() == ("", "file")

    assert stored_password_file_is_present() is True
    assert repr(password_hash_file) in caplog.text


def test_a_symlink_to_a_readable_file_is_read_through(
    password_hash_file: Path, tmp_path: Path
) -> None:
    """Links are followed on purpose: a secrets mount keeps working.

    Anyone who can create the link can write the file, so refusing links would
    cost an operator the Docker-secret / systemd-credential layout for no gain.
    """
    mounted = tmp_path / "secrets-mount" / "musicdrop-password"
    mounted.parent.mkdir(parents=True, exist_ok=True)
    mounted.write_text(_FILE_HASH + "\n", encoding="utf-8")
    password_hash_file.parent.mkdir(parents=True, exist_ok=True)
    password_hash_file.symlink_to(mounted)

    assert effective_password() == (_FILE_HASH, "file")


def test_a_file_past_the_ceiling_is_refused_unread(
    password_hash_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Size is checked from the stat, so the bytes never reach memory.

    The content here is a VALID hash followed by padding: without the ceiling
    the resolver would return it, so the empty answer is what says the file was
    not read rather than read and rejected.
    """
    padded = _FILE_HASH + "\n" + "x" * MAX_HASH_FILE_BYTES
    _store(password_hash_file, padded)

    with caplog.at_level(logging.WARNING, logger="app.auth.source"):
        assert effective_password() == ("", "file")

    assert str(len(padded)) in caplog.text
    assert str(MAX_HASH_FILE_BYTES) in caplog.text


def test_a_file_at_the_ceiling_is_still_read(password_hash_file: Path) -> None:
    """The bound is a ceiling on the file, not a trap for an ordinary one.

    The literal bounds are the other half: below ~128 bytes a real
    ``scrypt$...`` line would not fit, and past 64 KiB the ceiling stops
    bounding what a gated request decodes.
    """
    assert 128 <= MAX_HASH_FILE_BYTES <= 64 * 1024
    padding = " " * (MAX_HASH_FILE_BYTES - len(_FILE_HASH))
    _store(password_hash_file, _FILE_HASH + padding)

    assert password_hash_file.stat().st_size == MAX_HASH_FILE_BYTES
    assert effective_password() == (_FILE_HASH, "file")


def test_presence_counts_an_entry_that_cannot_be_read(password_hash_file: Path) -> None:
    """ "Present" is the same question the resolver answers on its way to a hash.

    The boot posture line asks it to say a stored file is shadowed by the env
    var; a directory is the shape that proves "present" is not "readable".
    """
    assert stored_password_file_is_present() is False
    password_hash_file.mkdir(parents=True)
    assert stored_password_file_is_present() is True


def test_surrounding_whitespace_in_the_file_is_stripped(
    password_hash_file: Path,
) -> None:
    """A hand-edited file (or one written with a trailing newline) must resolve
    to the same string every reader agrees on — the session signing key is
    derived from it, so a stray newline would sign out every live session."""
    _store(password_hash_file, f"  {_FILE_HASH}  \n\n")
    assert effective_password() == (_FILE_HASH, "file")


# --------------------------------------------------------------------------
# writing it
# --------------------------------------------------------------------------


def test_the_hash_is_written_owner_only(password_hash_file: Path) -> None:
    """0600 from the moment it exists.

    ``write_atomic_bytes`` passes the final mode to ``os.open``, so there is no
    create-then-chmod window in which the credential is world-readable — the
    same treatment the session secret and the Plex admin token get.
    """
    write_password_hash(_FILE_HASH)

    assert password_hash_file.is_file()
    assert stat.S_IMODE(password_hash_file.stat().st_mode) == 0o600


def test_writing_creates_the_data_directory(password_hash_file: Path) -> None:
    """First run can happen before anything else has written to the beets dir."""
    assert not password_hash_file.parent.exists()
    write_password_hash(_FILE_HASH)
    assert effective_password() == (_FILE_HASH, "file")


def test_a_password_change_replaces_the_link_rather_than_writing_through_it(
    password_hash_file: Path, tmp_path: Path
) -> None:
    """A secrets mount survives reads, not the first password change.

    ``_describe_entry`` follows links, so an operator can point
    ``password-hash`` at a Docker secret and sign in through it. This is the
    other half of that sentence, and the reason the docstring there says
    "read-side": the writer publishes with ``os.replace``, which swaps the LINK
    for a regular file and never opens the target, so the mounted secret is left
    byte-identical and the new password lives in the beets directory instead.

    Pinned rather than left implicit because the two halves read as one promise:
    an operator told "links keep working" would expect the change to write
    through to the mount, and the orphaned secret then shadows the new password
    on the next deploy that recreates the link. Refusing to write through a
    planted link is the property being kept.
    """
    mounted = tmp_path / "secrets-mount" / "musicdrop-password"
    mounted.parent.mkdir(parents=True, exist_ok=True)
    mounted.write_text(_FILE_HASH + "\n", encoding="utf-8")
    before = mounted.read_bytes()
    password_hash_file.parent.mkdir(parents=True, exist_ok=True)
    password_hash_file.symlink_to(mounted)
    assert effective_password() == (_FILE_HASH, "file")

    write_password_hash(_ENV_HASH)

    assert password_hash_file.is_symlink() is False
    assert stat.S_ISREG(password_hash_file.lstat().st_mode)
    assert effective_password() == (_ENV_HASH, "file")
    assert mounted.read_bytes() == before


def test_writing_twice_replaces_the_stored_hash(password_hash_file: Path) -> None:
    write_password_hash(_FILE_HASH)
    write_password_hash(_ENV_HASH)

    assert effective_password() == (_ENV_HASH, "file")
    # No tempfile left behind: the atomic writer unlinks its own in a finally.
    assert [child.name for child in password_hash_file.parent.iterdir()] == [PASSWORD_HASH_FILENAME]
