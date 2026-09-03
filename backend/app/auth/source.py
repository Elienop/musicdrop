"""Where the single account's password hash comes from: the env var, or a file.

Two sources, and one rule that decides between them: **a non-empty
``MUSICDROP_PASSWORD_HASH`` wins, even when its value is unreadable.** The file
at ``<beets_dir>/password-hash`` is what the first-run setup form and the
change-password form write, and it is the only source that can change without a
restart (``Settings`` is built once at import, so the env var is a boot-time
value — see ``app/config.py``).

**Why env wins, including when it is garbage.** The env var is the
lockout-recovery lever: an operator who has forgotten the stored password sets
it in compose and regains access without deleting anything. If a typo'd env var
fell through to "no password configured", the app would offer first-run setup on
exactly the incident this rule exists for, the operator would create a SECOND
credential, and the corrected env var would later shadow it. So a set-but-
unreadable env hash refuses every login and offers no setup; the fix is to
correct or unset it. This is the one store in the repo where env beats the file
— ``app/plex/config.py`` and ``app/slskd/config.py`` deliberately do the
opposite, because for those the env var only SEEDS a value the UI then owns.

**Three-way answer, not two.** :func:`effective_password` reports the SOURCE
alongside the hash so callers can tell "nothing is configured" (offer setup)
from "something is configured that cannot be read" (refuse, and say which of
the two things to fix). ``password_is_configured`` collapses those two, which
is why it is not enough on its own.

An entry that exists but cannot be turned into a hash — wrong permissions, a
directory, a dangling symlink, bytes that are not UTF-8, a file past the size
ceiling — reports source ``"file"`` with an empty hash, NOT ``"none"``.
Treating it as absent would let setup overwrite it, and the recovery path for a
forgotten password is deliberately "delete the file and restart" rather than
"silently replace it". This is the opposite of what
``app/auth/session.py::_read_secret`` does with a truncated session secret: a
signing key nobody knows is safe to regenerate, a credential is not.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Final, Literal

from app.config import settings
from app.playlists.atomic import write_atomic_text

logger = logging.getLogger(__name__)

#: Which of the two sources the live hash came from. ``"none"`` is the only
#: value that admits first-run setup; the wire contract says the same thing
#: (``AuthStatus.password_source`` in ``app/models/auth.py``).
PasswordSource = Literal["none", "env", "file"]

PASSWORD_HASH_FILENAME: Final = "password-hash"

#: Owner-only, passed to ``os.open`` UP FRONT by the atomic writer, so the hash
#: is never even briefly world-readable — the treatment the session secret and
#: the Plex admin token already get.
PASSWORD_FILE_MODE: Final = 0o600

#: Above this many bytes the stored file is refused UNREAD. What this module
#: writes is one ``scrypt$...`` line — 88 bytes for the shipped parameters, and
#: under 100 for any of them (six ``$``-joined fields, ``app/auth/passwords.py``)
#: — so the ceiling is roughly forty times the real size and a hand-edited file
#: with stray blank lines still fits. It exists because :func:`_read_hash_file`
#: runs on every gated request: without it, a multi-megabyte file at that path
#: is decoded into memory each time.
MAX_HASH_FILE_BYTES: Final = 4096


def password_hash_path(beets_dir: str) -> Path:
    """Where the stored hash lives: ``<beets_dir>/password-hash``.

    Beside ``config.yaml``, ``library.db`` and ``session-secret`` — the one
    directory the operator already mounts and already backs up, so a restore
    brings the credential with it.
    """
    return Path(beets_dir) / PASSWORD_HASH_FILENAME


def live_password_hash_path() -> Path:
    """The path for the CURRENT settings, resolved at call time.

    Every production read and write goes through this one function rather than
    calling :func:`password_hash_path` with ``settings.beets_dir`` themselves,
    which makes it the single seam the test suite pins at a tmp dir — twice:
    ``backend/conftest.py`` pins it for the whole pytest process (the two
    import-time readers run before any fixture) and ``tests/conftest.py``'s
    autouse ``password_hash_file`` re-pins it per test.
    ``settings.beets_dir`` comes from ``backend/.env`` on a dev box and points
    at the developer's REAL library, so a test that forgot to isolate itself
    would otherwise write a credential into it.
    """
    return password_hash_path(settings.beets_dir)


def password_file_location() -> str:
    """The stored file's path as the boot log and the refusal messages name it.

    A separate function, and a ``str``, so callers OUTSIDE this module get the
    live path at call time. ``from app.auth.source import live_password_hash_path``
    binds that function in the importer's own namespace, where the test suite's
    pin — which patches this module's attribute — would not reach it, and the
    boot log would name a different file from the one being read.
    """
    return str(live_password_hash_path())


def _describe_entry(path: Path) -> os.stat_result | None:
    """What is at ``path``, or ``None`` when NOTHING is.

    An ``lstat`` first, so a SYMLINK is seen as a symlink rather than as its
    target. Links are then followed deliberately: an operator who points
    ``password-hash`` at a secrets mount (a Docker secret, a systemd credential)
    keeps working, and this module already trusts the directory beets' config
    and library sit in. Refusing links would break that for no gain — anyone who
    can create the link can write the file.

    A DANGLING link is the half worth spelling out: the second ``stat`` raises
    ``FileNotFoundError`` where the ``lstat`` succeeded. That error is left to
    propagate rather than turned into ``None``, because the LINK exists — and
    "nothing is there" is the answer that opens first-run setup, whose write
    would replace the link. Only the first ``lstat`` can say absent.
    """
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    if not stat.S_ISLNK(info.st_mode):
        return info
    return path.stat()


def _read_hash_file(path: Path) -> str | None:
    """The stored hash, or ``None`` when nothing exists at that path.

    ``None`` is the answer that admits first-run setup, so it is reserved for
    "the entry is not there" (see :func:`_describe_entry`). Anything else that
    exists and cannot be turned into a hash — a directory, a FIFO, a socket, a
    dangling symlink, a file this process may not open, bytes that are not
    UTF-8, a file past :data:`MAX_HASH_FILE_BYTES` — reports an empty hash from
    source ``"file"``: refuse, do not overwrite.

    The SHAPE is checked before anything is opened. ``read_text`` on a FIFO
    blocks until a writer appears, and this function runs synchronously on every
    gated request, so such an entry would park the event loop rather than raise.
    The size is checked for the same reason: a large file at that path would
    otherwise be decoded on each of those requests.

    ``UnicodeDecodeError`` is caught explicitly because it is a ``ValueError``,
    not an ``OSError``: binary junk in the file would otherwise propagate out
    of a status request as a 500.
    """
    try:
        info = _describe_entry(path)
        if info is None:
            return None
        if not stat.S_ISREG(info.st_mode):
            logger.warning("password hash file at %r is not a regular file", path)
            return ""
        if info.st_size > MAX_HASH_FILE_BYTES:
            logger.warning(
                "password hash file at %r is %d bytes, past the %d-byte ceiling,"
                " so it was not read",
                path,
                info.st_size,
                MAX_HASH_FILE_BYTES,
            )
            return ""
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        # %r on BOTH, never %s: the path is operator-controlled and the
        # exception's message quotes it back, so either one could forge a second
        # log line with a newline or an ANSI escape. repr neutralises both.
        logger.warning("password hash file at %r cannot be read: %r", path, exc)
        return ""
    except UnicodeDecodeError:
        logger.warning("password hash file at %r is not valid UTF-8", path)
        return ""
    return raw.strip()


def stored_password_file_is_present() -> bool:
    """Whether an entry exists at the stored-hash path, readable or not.

    "Present" is the same question :func:`_read_hash_file` answers on its way to
    a hash, and it is asked through that function so the two cannot drift: a
    directory, a dangling symlink and an unreadable file all count as present,
    for the reason that whole module docstring gives.

    Read at CALL time, like everything else here. The boot posture line
    (``app/auth/gate.py``) uses it to say that a stored file exists but is
    shadowed by ``MUSICDROP_PASSWORD_HASH`` — the state an operator lands in
    after removing the compose line and finding a password they did not expect.
    """
    return _read_hash_file(live_password_hash_path()) is not None


def effective_password() -> tuple[str, PasswordSource]:
    """The live hash and where it came from.

    Read through the ``settings`` SINGLETON and off the disk at CALL time,
    never captured: the file is written by ``POST /api/auth/setup`` and
    ``POST /api/auth/password`` while the process runs, so a captured value
    would leave the new password unusable until a restart. The file is ~100
    bytes and this is one ``lstat`` plus one ``read_text`` per CALL — a request
    costs as many as its handlers make (measured on this branch: 0 for
    ``/api/health``, 1 for ``/api/version`` and ``/api/auth/login``, 2 for
    ``/api/auth/status`` and ``/api/auth/password``, where the gate or the route
    asks a second time).

    The env value is returned exactly as configured (not stripped): the session
    signing key is derived from this string, so every reader has to agree on it
    byte for byte.
    """
    env = settings.password_hash
    if env.strip():
        return env, "env"
    stored = _read_hash_file(live_password_hash_path())
    if stored is None:
        return "", "none"
    return stored, "file"


def write_password_hash(stored: str) -> None:
    """Persist ``stored`` as the file source, owner-only and atomically.

    Same recipe as the session secret (``app/auth/session.py``): a tempfile in
    the same directory created with the final mode, fsync, ``os.replace``,
    fsync the parent. Nothing half-written is ever visible at the real path, so
    a crash mid-write cannot leave a credential that parses to nothing.

    ``os.replace`` is last-writer-wins rather than create-or-fail, which is why
    the check-then-write in ``app/api/auth.py`` holds a lock across both halves
    instead of relying on this call to refuse.

    No read-back after the replace, where ``load_or_create_session_secret``
    (same recipe, next door) has one. That re-read exists so two processes
    minting a secret on first run CONVERGE — the loser adopts the winner's key
    rather than signing cookies the other would reject. Here the two callers are
    coroutines in one process holding ``_PASSWORD_WRITE_LOCK`` across the whole
    check-and-write, so there is no second writer to converge with, and a
    read-back could only report a hash the caller's own cookie was not minted
    from.
    """
    write_atomic_text(live_password_hash_path(), stored + "\n", mode=PASSWORD_FILE_MODE)
