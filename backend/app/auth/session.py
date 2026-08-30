"""The session cookie: an HMAC-signed expiring token, and the signing secret.

The token is ``v1.<expiry-b64>.<signature-b64>``: a base64url expiry (Unix
seconds, ASCII digits) and an HMAC-SHA256 over exactly those payload bytes. It
carries no identity because there is no identity to carry — MusicDrop is a
single-account app — and no server-side session table, so nothing has to be
stored, reconciled or swept.

Two consequences worth being explicit about:

* **Logout is client-side.** ``POST /api/auth/logout`` expires the cookie in the
  browser; the token itself stays cryptographically valid until its embedded
  expiry. Revoking ONE early would need a server-side deny list, which does not
  exist — so a token copied out of a browser before logout keeps working.
  Revoking ALL of them is supported and takes two forms: change
  ``MUSICDROP_PASSWORD_HASH`` (the signing key is derived from it — see
  :func:`_signing_key`), or delete ``<beets_dir>/session-secret``.
* **The version prefix is the upgrade path.** A future token that needs a
  revocation id or an issued-at stamp becomes ``v2.``; ``v1`` tokens then fail
  the version check and their holders re-authenticate.

The signing secret is 32 random bytes persisted at ``<beets_dir>/session-secret``
with mode 0600, so sessions survive a restart (the alternative — a per-process
random key — logs the owner out on every container update).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from pathlib import Path
from typing import Final

from app.playlists.atomic import write_atomic_bytes

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME: Final = "musicdrop_session"

#: 30 days. Long on purpose: this is a LAN app the owner leaves open in a tab,
#: and the cost of a short window is a login prompt interrupting a library scan,
#: not a meaningfully smaller attack surface for a cookie that never leaves the
#: house.
#:
#: Also the cookie's ``Max-Age`` — but sharing this constant is NOT what keeps
#: the two in step, and reading it that way is how the drift gets shipped. The
#: cookie passes ``max_age`` explicitly while the token takes its lifetime from
#: :func:`mint_session_token`'s keyword default, so a login call site passing
#: ``max_age_seconds=60`` leaves the browser holding a "30-day" cookie the
#: server rejects after a minute, with every test green. The actual pin is the
#: assertion in ``test_the_right_password_sets_the_session_cookie``, which
#: decodes the minted token and compares its embedded expiry to the Max-Age the
#: header claims.
SESSION_MAX_AGE_SECONDS: Final = 30 * 24 * 60 * 60

SESSION_SECRET_FILENAME: Final = "session-secret"
_SECRET_BYTES: Final = 32
_SECRET_FILE_MODE: Final = 0o600

_TOKEN_VERSION: Final = "v1"
_TOKEN_SEPARATOR: Final = "."
_TOKEN_FIELD_COUNT: Final = 3

#: Domain separation for the signing-key derivation (see :func:`_signing_key`),
#: versioned independently of the token's own ``v1`` tag.
_KEY_BINDING_PREFIX: Final = b"musicdrop-session-key-v1|"


def _b64encode(raw: bytes) -> str:
    # urlsafe + stripped padding: the result is cookie-value-safe with no
    # quoting, which keeps the wire form readable in a browser's dev tools.
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(field: str) -> bytes | None:
    padded = field + "=" * (-len(field) % 4)
    try:
        # ``validate=True``: without it base64 silently DISCARDS characters
        # outside the alphabet, so ``v1.MTc.si!!gn`` and ``v1.MTc.sign`` would
        # decode identically and one token would have many accepted wire forms.
        # The signature check would reject both, but a credential should have
        # exactly one canonical spelling.
        #
        # ``b64decode(altchars=b"-_")`` rather than ``urlsafe_b64decode``,
        # which takes no ``validate`` argument at all — passing one there is a
        # ``TypeError``, and since it is not a ``ValueError`` the except below
        # would not catch it: every request carrying a cookie would 500.
        # (Measured: 596 tests failed the moment that was tried.) The altchars
        # translation runs BEFORE the validation regex, so this is exactly
        # urlsafe decoding with the junk-character check applied.
        return base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except ValueError:
        # Covers binascii.Error (junk characters, bad padding) and
        # UnicodeEncodeError (non-ASCII in the field) — both are ValueErrors.
        return None


def _signing_key(secret: bytes, password_hash: str) -> bytes:
    """The per-token key: the file secret, bound to the CURRENT password hash.

    Signing with the raw file secret would make a session outlive the password
    that created it — change ``MUSICDROP_PASSWORD_HASH`` because it leaked, and
    every cookie minted under the old one keeps working for up to 30 days.
    There is no session store to sweep, so the binding has to be in the key:
    derive it from the hash, and rotating the hash changes the key, which
    invalidates every outstanding token at once.

    The domain-separation prefix keeps this HMAC from ever colliding with the
    payload signature computed under the same secret, and carries its own
    version so the derivation can change without reusing ``v1``'s meaning.
    An UNSET hash is a legitimate input and derives a stable key of its own —
    it just cannot be authenticated against, because login refuses first.
    """
    return hmac.new(
        secret, _KEY_BINDING_PREFIX + password_hash.encode("utf-8"), hashlib.sha256
    ).digest()


def _sign(secret: bytes, password_hash: str, payload: bytes) -> bytes:
    return hmac.new(_signing_key(secret, password_hash), payload, hashlib.sha256).digest()


def mint_session_token(
    secret: bytes,
    password_hash: str,
    *,
    max_age_seconds: int = SESSION_MAX_AGE_SECONDS,
) -> str:
    """A token for a session expiring ``max_age_seconds`` from now.

    ``password_hash`` is read by the CALLER at call time (never captured here),
    so a rotated ``MUSICDROP_PASSWORD_HASH`` takes effect on the next request
    rather than the next restart.
    """
    payload = str(int(time.time()) + max_age_seconds).encode("ascii")
    return _TOKEN_SEPARATOR.join(
        (
            _TOKEN_VERSION,
            _b64encode(payload),
            _b64encode(_sign(secret, password_hash, payload)),
        )
    )


def session_token_is_valid(token: str | None, secret: bytes, password_hash: str) -> bool:
    """Whether ``token`` was signed for ``password_hash`` and has not expired.

    The signature is checked BEFORE the expiry is read, because the expiry is
    attacker-supplied until the HMAC says otherwise: parsing it first would
    decide "expired vs not" from bytes nothing has authenticated. The compare
    is ``hmac.compare_digest`` so a forged signature cannot be walked byte by
    byte from response timing.
    """
    # ``not secret`` is defence-in-depth, not covered behaviour: no production
    # caller can reach it. The gate only calls this once ``app.state.session_secret``
    # is `bytes`, and the only writer is `load_or_create_session_secret`, which
    # returns either a >=32-byte file read or a fresh 32-byte token — b"" is not
    # among its outputs. `test_no_secret_means_no_valid_token` exercises the arm
    # by calling this function directly, so it is pinned but only at that level;
    # do not read it as evidence that an empty secret is a reachable state.
    if not token or not secret:
        return False
    fields = token.split(_TOKEN_SEPARATOR)
    if len(fields) != _TOKEN_FIELD_COUNT or fields[0] != _TOKEN_VERSION:
        return False
    payload = _b64decode(fields[1])
    signature = _b64decode(fields[2])
    if payload is None or signature is None:
        return False
    if not hmac.compare_digest(_sign(secret, password_hash, payload), signature):
        return False
    return _not_expired(payload)


def _not_expired(payload: bytes) -> bool:
    try:
        expires_at = int(payload.decode("ascii"))
    except ValueError:
        # Signed by us and still unreadable (int() refuses, or the decode
        # raises UnicodeDecodeError — itself a ValueError): a payload shape
        # from another version that reused the ``v1`` tag. Fail closed.
        return False
    return time.time() < expires_at


def session_secret_path(beets_dir: str) -> Path:
    """Where the signing secret lives: ``<beets_dir>/session-secret``.

    Beside ``config.yaml`` and ``library.db`` — the one directory the operator
    already mounts and already backs up, so a restore brings sessions with it.
    """
    return Path(beets_dir) / SESSION_SECRET_FILENAME


def _read_secret(path: Path) -> bytes | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) < _SECRET_BYTES:
        # Not "absent" for the usual reason — someone truncated or `touch`ed
        # the file. Signing with a short (or empty) key would silently weaken
        # every token, so treat it as missing and say why.
        logger.warning(
            "session secret at %s is %d bytes (need %d); generating a new one — "
            "existing sessions will be logged out",
            path,
            len(raw),
            _SECRET_BYTES,
        )
        return None
    return raw


def load_or_create_session_secret(path: Path) -> bytes:
    """The persisted signing secret, generating and storing one on first run.

    Written through the repo's crash-safe atomic recipe with the final mode
    passed UP FRONT, so the secret is never even briefly world-readable (the
    same treatment ``app/plex/config.py`` gives the Plex admin token).

    ``write_atomic_bytes`` publishes with ``os.replace``, which is last-writer-
    wins rather than create-or-fail, so two processes reaching first-run
    together could each mint a key. Re-reading after the write is what makes
    them converge: the loser adopts the winner's secret instead of signing
    cookies the other half of the deployment would reject. It narrows the
    window rather than closing it (both could re-read before the other's
    replace); the image runs uvicorn with a single worker by design, so there
    is no second process to race in the deployment this ships for.
    """
    existing = _read_secret(path)
    if existing is not None:
        return existing
    candidate = secrets.token_bytes(_SECRET_BYTES)
    write_atomic_bytes(path, candidate, mode=_SECRET_FILE_MODE)
    settled = _read_secret(path)
    return candidate if settled is None else settled
