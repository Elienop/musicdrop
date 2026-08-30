"""Wire models for the session-login endpoints."""

from pydantic import BaseModel


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

    ``password_set`` is false both when ``MUSICDROP_PASSWORD_HASH`` is unset and
    when it is set to something unreadable: in either case nothing can
    authenticate, and the operator's fix is the same.
    """

    authenticated: bool
    password_set: bool
