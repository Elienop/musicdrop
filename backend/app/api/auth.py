"""Sign in, sign out, and "am I signed in?" for the single-account session.

``login`` and ``status`` are exempt from the session gate (``app/auth/gate.py``)
— they are how a cookie-less browser gets one, and what the login screen asks
before it renders. ``logout`` is NOT exempt: it is a gated route like any
other, so the gate stays the single place a session is checked.

``login`` is ``async`` and hands the ~0.16 s memory-hard derive to a worker
thread itself, rather than being a plain ``def`` and letting FastAPI do it. The
difference is where a request WAITS. A sync route holding a
``threading.Lock`` parks a worker from the framework's shared pool
(``CapacityLimiter(40)`` by default) for the whole wait, and that pool backs
every other blocking call in the app — measured, 80 concurrent anonymous logins
pushed an unrelated request to ~2.1 s. Waiting for an ``anyio.CapacityLimiter``
instead suspends a coroutine, which costs no thread at all.
"""

from __future__ import annotations

from typing import Final

import anyio
import anyio.to_thread
from fastapi import APIRouter, HTTPException, Request, Response

from app.auth.cookies import request_is_https
from app.auth.gate import scope_has_valid_session
from app.auth.passwords import password_is_configured, verify_password
from app.auth.session import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    mint_session_token,
)
from app.config import settings
from app.models.auth import AuthStatus, LoginRequest
from app.models.errors import ErrorDetail

router = APIRouter(tags=["auth"])

_NO_PASSWORD_DETAIL: Final = "no password is configured on this server"
_UNREADABLE_HASH_DETAIL: Final = "the configured password hash is not readable"
_WRONG_PASSWORD_DETAIL: Final = "incorrect password"
_BUSY_DETAIL: Final = "another sign-in attempt is in progress"
_NO_SECRET_DETAIL: Final = "the session signing secret is unavailable"

# ONE verify at a time, process-wide. Each scrypt derive holds ~128 MiB
# (app/auth/passwords.py), so N parallel logins would allocate N times that and
# the container would be OOM-killed by a handful of requests. Serialising also
# caps guessing at roughly one attempt per derive (~6/s), which is this login's
# only brute-force brake — there is no lockout and no attempt counter.
#
# A CapacityLimiter rather than a threading.Lock: a task waiting on this is
# SUSPENDED, holding no worker. A lock held across the derive in a sync route
# parks one of the framework's shared pool of 40 for the duration, so a burst of
# logins starves every other blocking call in the app (album reads, imports,
# artwork writes) — measured at ~2.1 s for an unrelated request under 80
# concurrent logins.
#
# Acquired and released BY HAND in _verify_serialised, never via run_sync's
# `limiter=` kwarg — that spelling looks equivalent and is not. Read that
# function's docstring before changing this.
_VERIFY_LIMITER: Final = anyio.CapacityLimiter(1)

# How long a request will WAIT for a turn before giving up with a 429. The wait
# no longer costs a thread, but it still costs the CALLER, and an unbounded
# queue would let a burst turn every login into a multi-second hang. ~2 s admits
# a double-clicked Sign in (one derive ahead of it) and a dozen more.
#
# This bounds the WAIT, not the derive. A caller that gives up here never
# started one; a caller that was admitted runs to completion however long that
# takes, holding the token throughout — see _verify_serialised for why the
# reverse (abandoning an in-flight derive) silently unbounds the memory ceiling.
_LOCK_WAIT_SECONDS: Final = 2.0

_LOGIN_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    401: {
        "model": ErrorDetail,
        "description": (
            "The password did not match, or no usable password hash is "
            "configured on the server (MUSICDROP_PASSWORD_HASH)."
        ),
    },
    429: {
        "model": ErrorDetail,
        "description": (
            "Another sign-in attempt was still being verified and this one "
            "waited its turn without getting one. The password check is "
            "deliberately serialised and slow; retry."
        ),
    },
    503: {
        "model": ErrorDetail,
        "description": (
            "The server has no session signing secret, so no cookie can be "
            "issued. Only reachable if startup did not complete."
        ),
    },
}


def _refusal_detail(stored: str) -> str:
    """Why an unusable ``MUSICDROP_PASSWORD_HASH`` refused this login.

    Two sentences rather than one because the operator IS the only user here:
    "not configured" and "configured but unreadable" need different fixes, and
    the login screen shows the sentence verbatim. Neither leaks anything an
    unauthenticated caller could not learn from ``password_set`` on
    ``/api/auth/status``, which is deliberately false in both states.
    """
    return _NO_PASSWORD_DETAIL if not stored.strip() else _UNREADABLE_HASH_DETAIL


def _session_secret(request: Request) -> bytes:
    secret = getattr(request.app.state, "session_secret", None)
    if not isinstance(secret, bytes):
        # Fail closed, and honestly: the password may well have been right, but
        # no cookie can be signed. Never mint a per-request secret to paper
        # over it — every later request would be checked against a key this one
        # alone knew.
        raise HTTPException(status_code=503, detail=_NO_SECRET_DETAIL)
    return secret


async def _verify_serialised(candidate: str, stored: str) -> bool:
    """Run one derive at a time, or refuse with a 429 rather than queue.

    ADMISSION IS TAKEN EXPLICITLY, and this is the whole point of the shape.
    The obvious spelling — ``run_sync(..., limiter=_VERIFY_LIMITER,
    abandon_on_cancel=True)`` inside ``move_on_after`` — does NOT bound
    concurrency, because anyio holds the limiter as ``async with limiter:``
    *around* the ``await future`` (see ``run_sync_in_worker_thread`` in
    ``anyio/_backends/_asyncio.py``). Abandoning unwinds through that
    ``async with`` and RELEASES the token while the un-interruptible worker
    thread keeps deriving, so the next caller is admitted immediately and
    steady-state concurrency is ``derive_time / window``, not 1. Measured with
    a 0.6 s derive and a 0.2 s window: **peak 3 concurrent derives, every
    caller admitted**. At production numbers that is ~384 MiB of scrypt at
    under 0.5 requests per second — the exact ceiling the limiter exists to
    enforce, gone.

    So: wait for the token under the timeout, and hold it in a ``finally``
    until the derive COMPLETES rather than until a caller gives up.

    ``admitted`` rather than ``scope.cancel_called`` decides the refusal,
    because those two can disagree: the token can be handed over just as the
    deadline fires, and only ``admitted`` says whether there is something to
    release. A cancelled ``acquire`` never takes a token (anyio pops the waiter
    and re-notifies the next one), so the two failure directions are covered.

    ``abandon_on_cancel=False`` and no ``limiter=`` kwarg on the derive itself:
    admission is already held, and passing the same limiter again would
    self-deadlock. The derive therefore draws one token from anyio's DEFAULT
    thread limiter (40) — bounded at one by the admission above, so 39 remain
    for every other blocking call in the app.
    """
    admitted = False
    with anyio.move_on_after(_LOCK_WAIT_SECONDS):
        await _VERIFY_LIMITER.acquire()
        # No await between the acquire returning and this line, so the flag
        # cannot disagree with whether the token is held.
        admitted = True
    if not admitted:
        raise HTTPException(status_code=429, detail=_BUSY_DETAIL)
    try:
        return await anyio.to_thread.run_sync(verify_password, candidate, stored)
    finally:
        _VERIFY_LIMITER.release()


@router.post("/auth/login", responses=_LOGIN_RESPONSES)
async def login(body: LoginRequest, request: Request, response: Response) -> AuthStatus:
    """Exchange the password for a session cookie."""
    # Read through the settings SINGLETON at request time, never captured at
    # import: attribute-patching ``app.config.settings`` is how the suite
    # overrides configuration, and a captured value would be invisible to it.
    stored = settings.password_hash
    if not password_is_configured(stored):
        raise HTTPException(status_code=401, detail=_refusal_detail(stored))
    secret = _session_secret(request)
    if not await _verify_serialised(body.password, stored):
        raise HTTPException(status_code=401, detail=_WRONG_PASSWORD_DETAIL)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        # Bound to the hash this login just verified against, so rotating
        # MUSICDROP_PASSWORD_HASH invalidates this cookie along with every
        # other outstanding one.
        value=mint_session_token(secret, stored),
        max_age=SESSION_MAX_AGE_SECONDS,
        path="/",
        httponly=True,
        samesite="lax",
        # Secure only when THIS login arrived over HTTPS — a per-request
        # decision, not a property of the build. Over plain HTTP (the by-IP
        # LAN deployment) the flag has to be absent or the browser would never
        # send the cookie back and the app could not be signed into at all;
        # behind a TLS proxy it costs nothing and keeps the session off any
        # plain-HTTP hop. app/auth/cookies.py carries the spoof analysis, and
        # why X-Forwarded-Proto is trusted here while the host guard refuses
        # to trust X-Forwarded-Host.
        secure=request_is_https(request),
    )
    return AuthStatus(authenticated=True, password_set=True)


@router.post("/auth/logout", status_code=204)
def logout(request: Request) -> Response:
    """Expire the session cookie in the browser.

    Client-side only: the token stays cryptographically valid until the expiry
    baked into it, because there is no server-side session record to revoke
    (see ``app/auth/session.py``). A token copied out of a browser before
    logout therefore keeps working; deleting ``<beets_dir>/session-secret``
    invalidates every session at once, which is the blunt instrument available.

    Takes the ``Request`` only to read the scheme, so the expiring cookie
    carries the same ``Secure`` posture the login one did.
    """
    response = Response(status_code=204)
    # Same attributes as the Set-Cookie that created it — a browser matches the
    # cookie to expire by name AND path, so a mismatched path would leave the
    # original in place and "logout" would do nothing.
    #
    # Secure is RECOMPUTED from this request rather than remembered, which is
    # the only way it could be: a logout is a different request from the login,
    # and nothing server-side records what the login decided. Name and path are
    # what browsers actually match on, so a Secure mismatch would not break the
    # deletion — the symmetry is kept anyway, because someone reading the two
    # call sites should find one policy rather than two that happen to agree.
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=request_is_https(request),
    )
    return response


@router.get("/auth/status")
def auth_status(request: Request) -> AuthStatus:
    """Whether this browser is signed in, and whether signing in is possible.

    Exempt from the gate, so it answers for a cookie-less caller too — that is
    the point. ``authenticated`` is decided by the gate's OWN predicate rather
    than a second copy of the check, so this endpoint cannot say "signed in"
    about a cookie the gate would reject.
    """
    return AuthStatus(
        authenticated=scope_has_valid_session(request.scope),
        password_set=password_is_configured(settings.password_hash),
    )
