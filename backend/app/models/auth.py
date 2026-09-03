"""Wire models for the session-login, first-run-setup and change-password endpoints."""

from pydantic import BaseModel

from app.auth.source import PasswordSource


class LoginRequest(BaseModel):
    """The one field ``POST /api/auth/login`` takes.

    No length ceiling, but not for the reason it would be easy to assume.
    scrypt runs its input through PBKDF2-HMAC-SHA256 first, and that step is
    LINEAR in input length — measured here, a 24 MiB password takes 0.182 s
    against 0.155 s for a short one, so "long inputs are free" is simply false.
    The ceiling is unnecessary anyway: the request body is already bounded by
    the body-size guard (``MUSICDROP_MAX_BODY_BYTES``, 25 MiB by default), and
    across that whole range the marginal cost is a fraction of one derive —
    which the login route serialises to one at a time regardless.
    """

    password: str


class AuthStatus(BaseModel):
    """Whether this caller is signed in, and whether signing in is possible.

    Returned by ``GET /api/auth/status`` AND by a successful
    ``POST /api/auth/login`` — one model, so the client can seed its status
    cache straight from the login response instead of round-tripping again.

    ``password_set`` says the EFFECTIVE hash parses, so it is false both when
    no password is configured at all and when the configured one is unreadable:
    in either case nothing can authenticate. ``password_source`` is what tells
    those two apart, and what decides whether first-run setup is offered:

    * ``"none"`` — no source exists. ``POST /api/auth/setup`` is available, and
      this is the ONLY value for which it is (it answers 409 otherwise);
    * ``"env"`` — ``MUSICDROP_PASSWORD_HASH`` is non-empty. It wins over the
      file whether or not its value can be read, so ``"env"`` with
      ``password_set`` false means "fix or unset the env var" (in
      docker-compose, every $ in the hash must be doubled to $$);
    * ``"file"`` — the hash file under the beets directory exists. ``"file"``
      with ``password_set`` false means "delete that file and restart", which
      is also the forgotten-password recovery.
    """

    authenticated: bool
    password_set: bool
    password_source: PasswordSource


class SetupRequest(BaseModel):
    """The one field ``POST /api/auth/setup`` takes, on first run only.

    No confirmation field: the confirm-and-compare belongs to the form, which
    can tell the operator about a typo without spending a ~0.16 s scrypt derive
    on it. No length or composition policy either — see :class:`LoginRequest`
    for why a ceiling would not help, and ``app/auth/hash_password.py`` for the
    one rule this mirrors: an empty (or whitespace-only) password is refused.
    """

    password: str


class ChangePasswordRequest(BaseModel):
    """Both fields ``POST /api/auth/password`` takes.

    The current password is required even though the caller already holds a
    valid session cookie: the cookie is a 30-day bearer token with no
    server-side record, so possession of it must not be enough to replace the
    credential it was minted from.
    """

    current_password: str
    new_password: str
