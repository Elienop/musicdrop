"""The ``MUSICDROP_PASSWORD_HASH`` format, and the scrypt verify behind it.

The env var holds a SELF-DESCRIBING hash string, never a plaintext password::

    scrypt$<n>$<r>$<p>$<salt-b64>$<hash-b64>

Every parameter needed to reproduce the derivation travels with the digest, so
raising the work factors later re-verifies hashes generated before the change,
and the leading algorithm tag is the version field: an ``argon2id$...`` string
would be refused by name here rather than mis-parsed as this format. Generate a
value with ``uv run python -m app.auth.hash_password``.

Work factors — ``n=2**17``, ``r=8``, ``p=1`` — are OWASP's first-listed scrypt
setting (Password Storage Cheat Sheet, "scrypt": "N=2^17 (128 MiB), r=8 (1024
bytes), p=1"); RFC 7914 section 2 separately notes that "r=8 and p=1 appears to
yield good results". The 16-byte salt is NIST SP 800-132 section 5.1 ("The
length of the randomly-generated portion of the salt shall be at least 128
bits"). One derive measured ~0.16 s on the dev box, which is also the
brute-force brake the login route serialises on.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Final

_ALGORITHM: Final = "scrypt"
_FIELD_SEPARATOR: Final = "$"
_FIELD_COUNT: Final = 6

_SCRYPT_N: Final = 2**17
_SCRYPT_R: Final = 8
_SCRYPT_P: Final = 1
_SCRYPT_DKLEN: Final = 32
_SALT_BYTES: Final = 16

# A stored hash's parameters are honoured as written (that is what makes the
# format future-proof), so they also decide how much memory a login spends.
# The value comes from the operator's own environment rather than from a
# request, but it is read on EVERY login attempt, so a fat-fingered exponent
# must fail as a configuration error and not as an allocation. 1 GiB is eight
# times OWASP's 128 MiB setting — room to raise the cost several times over,
# far short of anything that would evict the beets library from page cache.
_MAX_WORKING_SET: Final = 1024 * 1024 * 1024


class PasswordHashError(ValueError):
    """``MUSICDROP_PASSWORD_HASH`` is set but is not a hash this module wrote."""


@dataclass(frozen=True)
class _ScryptHash:
    n: int
    r: int
    p: int
    salt: bytes
    digest: bytes


def _working_set(n: int, r: int, p: int) -> int:
    """The bytes OpenSSL requires ``maxmem`` to admit for these parameters.

    ``hashlib.scrypt`` hands ``maxmem`` straight to OpenSSL, whose default is
    far below what n=2**17 needs — leave it unset and the derive raises instead
    of running. OpenSSL admits a derivation when ``128 * r * (N + p + 2)`` fits:
    scrypt's ROMix working set of N blocks of ``128 * r`` octets (RFC 7914
    sections 4-5), plus the p input blocks and two scratch blocks. Measured
    against this venv's OpenSSL, the smallest accepted value for the constants
    above is exactly that expression (134,220,800 bytes). Derived rather than
    hardcoded so raising the work factors cannot silently leave maxmem behind.
    """
    return 128 * r * (n + p + 2)


def _maxmem(n: int, r: int, p: int) -> int:
    # +1 MiB of headroom for an OpenSSL whose accounting differs by a block.
    return _working_set(n, r, p) + 1024 * 1024


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64decode(field: str) -> bytes:
    # ``validate=True``: without it base64 silently DISCARDS characters outside
    # the alphabet, so a corrupted salt would decode to a shorter-but-plausible
    # one and every login would fail with no explanation.
    try:
        return base64.b64decode(field, validate=True)
    except ValueError as exc:  # binascii.Error is a ValueError
        raise PasswordHashError("hash field is not valid base64") from exc


def _cost(n: str, r: str, p: str) -> tuple[int, int, int]:
    try:
        return int(n), int(r), int(p)
    except ValueError as exc:
        raise PasswordHashError("hash cost parameters are not integers") from exc


def _validate(parsed: _ScryptHash) -> None:
    """Reject a hash scrypt itself would reject, as a CONFIG error not a crash."""
    # RFC 7914 section 2: N "must be larger than 1, a power of 2".
    if parsed.n < 2 or parsed.n & (parsed.n - 1):
        raise PasswordHashError("hash cost parameter n is not a power of two above 1")
    if parsed.r < 1 or parsed.p < 1:
        raise PasswordHashError("hash cost parameters r and p must be positive")
    if not parsed.salt or not parsed.digest:
        raise PasswordHashError("hash carries an empty salt or digest")
    if _working_set(parsed.n, parsed.r, parsed.p) > _MAX_WORKING_SET:
        raise PasswordHashError("hash cost parameters demand too much memory")


def _parse(stored: str) -> _ScryptHash:
    fields = stored.strip().split(_FIELD_SEPARATOR)
    if len(fields) != _FIELD_COUNT:
        raise PasswordHashError("hash is not <algorithm>$<n>$<r>$<p>$<salt>$<digest>")
    algorithm, n, r, p, salt, digest = fields
    if algorithm != _ALGORITHM:
        raise PasswordHashError(f"unsupported password hash algorithm {algorithm!r}")
    parsed = _ScryptHash(*_cost(n, r, p), salt=_b64decode(salt), digest=_b64decode(digest))
    _validate(parsed)
    return parsed


def hash_password(password: str) -> str:
    """A fresh ``scrypt$...`` string for ``password``, with a random salt."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_maxmem(_SCRYPT_N, _SCRYPT_R, _SCRYPT_P),
        dklen=_SCRYPT_DKLEN,
    )
    return _FIELD_SEPARATOR.join(
        (
            _ALGORITHM,
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            _b64encode(salt),
            _b64encode(digest),
        )
    )


def verify_password(candidate: str, stored: str) -> bool:
    """Whether ``candidate`` derives to the digest ``stored`` carries.

    Raises :class:`PasswordHashError` when ``stored`` is not a hash this module
    can read — the caller must treat that as a REFUSAL (and say so), never as a
    reason to let the request through.

    The parameters come from the stored hash rather than from this module's
    constants, which is what lets the work factors be raised without
    invalidating older hashes. The compare is ``hmac.compare_digest`` over the
    derived KEYS, not over the hash strings: a plain ``==`` on bytes
    short-circuits at the first differing byte and leaks, through timing, how
    much of a guess was right.
    """
    parsed = _parse(stored)
    derived = hashlib.scrypt(
        candidate.encode("utf-8"),
        salt=parsed.salt,
        n=parsed.n,
        r=parsed.r,
        p=parsed.p,
        maxmem=_maxmem(parsed.n, parsed.r, parsed.p),
        dklen=len(parsed.digest),
    )
    return hmac.compare_digest(derived, parsed.digest)


def password_is_configured(stored: str) -> bool:
    """Whether ``stored`` is a USABLE hash — set, and readable by this module.

    Backs ``password_set`` on ``GET /api/auth/status``. A malformed value is
    reported as "not configured" for the same reason the login route refuses
    it: nothing can ever authenticate against it.
    """
    if not stored.strip():
        return False
    try:
        _parse(stored)
    except PasswordHashError:
        return False
    return True
