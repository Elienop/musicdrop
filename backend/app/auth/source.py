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

A file that exists but cannot be read — wrong permissions, a directory, bytes
that are not UTF-8 — reports source ``"file"`` with an empty hash, NOT
``"none"``. Treating it as absent would let setup overwrite it, and the
recovery path for a forgotten password is deliberately "delete the file and
restart" rather than "silently replace it". This is the opposite of what
``app/auth/session.py::_read_secret`` does with a truncated session secret: a
signing key nobody knows is safe to regenerate, a credential is not.
"""

from __future__ import annotations

import logging
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
    which makes it the single seam the test suite pins at a tmp dir
    (``tests/conftest.py::_pin_the_password_file_away_from_any_real_library``).
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


def _read_hash_file(path: Path) -> str | None:
    """The stored hash, or ``None`` when the file is genuinely NOT THERE.

    ``FileNotFoundError``/``NotADirectoryError`` mean no file exists at that
    path, so setup may proceed. Every other failure means something IS there
    and this process cannot read it, which is reported as an empty hash from
    source ``"file"`` — refuse, do not overwrite.

    ``UnicodeDecodeError`` is caught explicitly because it is a ``ValueError``,
    not an ``OSError``: binary junk in the file would otherwise propagate out
    of a status request as a 500.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
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


def effective_password() -> tuple[str, PasswordSource]:
    """The live hash and where it came from.

    Read through the ``settings`` SINGLETON and off the disk at CALL time,
    never captured: the file is written by ``POST /api/auth/setup`` and
    ``POST /api/auth/password`` while the process runs, so a captured value
    would leave the new password unusable until a restart. The file is ~100
    bytes and this is one ``read_text`` per request.

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
    """
    write_atomic_text(live_password_hash_path(), stored + "\n", mode=PASSWORD_FILE_MODE)
