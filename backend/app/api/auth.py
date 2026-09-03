"""Sign in, sign out, set up, change the password, and "am I signed in?".

``login``, ``status`` and ``setup`` are exempt from the session gate
(``app/auth/gate.py``) — they are how a cookie-less browser gets one, what the
login screen asks before it renders, and how a server with no credential at all
gets its first. ``logout`` and ``password`` are NOT exempt: they are gated
routes like any other, so the gate stays the single place a session is checked.

Both writing routes go through :func:`app.auth.source.write_password_hash`, and
both then re-mint THE CALLER'S cookie: the signing key is derived from the
password hash, so a rotated hash invalidates every other live session on its
next request (``app/auth/session.py::_signing_key``). That is the intended
behaviour of a password change and the copy says so; it is also why setup, which
runs before any session exists, has to issue a cookie rather than bounce the
operator to the sign-in form it just replaced.

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

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

import anyio
import anyio.to_thread
from fastapi import APIRouter, HTTPException, Request, Response

from app.auth.cookies import request_is_https
from app.auth.gate import scope_has_valid_session
from app.auth.passwords import hash_password, password_is_configured, verify_password
from app.auth.session import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    mint_session_token,
)
from app.auth.source import (
    PasswordSource,
    effective_password,
    password_file_location,
    write_password_hash,
)
from app.models.auth import (
    AuthStatus,
    ChangePasswordRequest,
    LoginRequest,
    SetupRequest,
)
from app.models.errors import ErrorDetail, validation_or_detail_422

logger = logging.getLogger(__name__)

# The two lines an OPERATOR is told to look for in `docker logs`, and the reason
# they do not use `logger` above: uvicorn's LOGGING_CONFIG configures only its
# own loggers and leaves root at WARNING with no handler, so under the shipped
# CMD (`Dockerfile:61`, no --log-config) an INFO record from `app.api.auth` is
# discarded and a WARNING one reaches stderr through `logging.lastResort` —
# printed bare, with no level to grep for. Same reasoning, and the same choice,
# as the boot posture line; the long version is in the comment above it
# (`app/main.py`). Pinned by
# test_host_guard.py::test_both_password_lines_reach_real_uvicorns_output, which
# reads a real child process's output rather than caplog: caplog attaches to the
# ROOT logger, so it cannot tell these two spellings apart.
operator_logger = logging.getLogger("uvicorn.error")

router = APIRouter(tags=["auth"])

# These are UI COPY, which is why they are full sentences.
#
# Every other ``ErrorDetail`` in the app is read by a developer — in a log, in
# `curl` output, through a generated client — but the login form renders
# whichever of these came back straight into the page, verbatim and unwrapped
# (frontend/src/pages/LoginPage.tsx; the contract is documented on both sides).
# The operator IS the only user here, and they see this text in a form next to
# sentence-case labels, so the register is a copy decision rather than a house
# convention.
#
# The divergence is narrower than it looks. Capitalisation is already the house
# majority, comfortably and under every way of counting it ("Album not found",
# "An import is already running") — so only the terminating period is unusual,
# and it is here because a fragment reads as a label while a sentence reads as
# an answer. ``app/models/errors.py`` calls the shape "one ASCII sentence";
# these five are the ones that take it literally.
#
# That majority is stated as a PREDICATE on purpose, where this comment used to
# quote an exact fraction. The fraction is not reproducible without also
# writing down where you draw the line, and the line has several defensible
# places: count only the string literals passed as ``detail=`` and you get one
# denominator; add the ``HTTPException`` calls that pass detail POSITIONALLY
# and you get another; fold in the strings nested inside ``detail={...}``
# payloads and you get a third. Re-derived by AST over ``backend/app`` on
# 2026-08-30, those readings spanned 53-84 distinct literals at 77-90%
# capitalised. The direction is robust under all of them; the exact pair of
# numbers was not, and nothing in CI re-runs it — so a corrected count would
# only have been a fresh expiry date.
#
# The gate's own detail (``app/auth/gate.py``: "authentication required") is
# deliberately NOT in this set. It is a bounce trigger the transport turns into
# a redirect and no human ever reads, so it keeps the house register.
_NO_PASSWORD_DETAIL: Final = "No password is configured on this server."
# Three sentences, and the last two are both recoveries, because they lead
# different places. Fixing the value keeps the override; unsetting it hands the
# server back to whatever is stored — which may be a password the operator set
# on the sign-in screen, or nothing at all, and the sentence says so rather than
# promising either. A hash pasted into docker-compose with single dollars is
# usually interpolated to something shorter (compose swallows a letter-led
# field; a digit-led one survives), which is the measured way this state is
# reached. The file twin below names its own recovery, which is deleting it
# (never overwriting it — see app/auth/source.py).
_UNREADABLE_ENV_HASH_DETAIL: Final = (
    "The password hash in MUSICDROP_PASSWORD_HASH is not readable. "
    "In docker-compose, every $ in the hash must be doubled to $$. "
    "Or unset it and restart MusicDrop, and the password stored on this server, "
    "if there is one, applies again."
)
_UNREADABLE_FILE_HASH_DETAIL: Final = (
    "The stored password hash on this server is not readable. "
    "Delete the password-hash file in the beets directory and restart MusicDrop "
    "to set a new password."
)
_WRONG_PASSWORD_DETAIL: Final = "Incorrect password."
_WRONG_CURRENT_PASSWORD_DETAIL: Final = "The current password is incorrect."
_BUSY_DETAIL: Final = "Another sign-in is already in progress. Try again in a moment."
_NO_SECRET_DETAIL: Final = "The session signing secret is unavailable."
_BLANK_PASSWORD_DETAIL: Final = "A password cannot be empty or only whitespace."
_ENV_ALREADY_CONFIGURED_DETAIL: Final = (
    "A password is already configured on this server from "
    "MUSICDROP_PASSWORD_HASH. Unset that variable and restart MusicDrop to let "
    "the app manage the password instead."
)
_FILE_ALREADY_CONFIGURED_DETAIL: Final = (
    "A password is already configured on this server and stored in the beets "
    "directory. Change it from Settings, or delete the password-hash file there "
    "and restart MusicDrop to set a new one."
)
_ENV_OVERRIDE_DETAIL: Final = (
    "The password comes from MUSICDROP_PASSWORD_HASH, which overrides the stored "
    "one, so it cannot be changed here. Unset that variable and restart MusicDrop "
    "to let the app manage the password instead."
)
_NOTHING_TO_CHANGE_DETAIL: Final = (
    "There is no password to change on this server yet. Set one on the sign-in screen first."
)
_WRITE_FAILED_DETAIL: Final = (
    "The new password could not be saved to the beets directory, so nothing was "
    "changed. Check that the directory is writable, then try again."
)

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

_BUSY_429_DESCRIPTION: Final = (
    "Another password derive was still running and this request waited its turn "
    "without getting one. The scrypt work is deliberately serialised and slow; "
    "retry."
)
_NO_SECRET_503_DESCRIPTION: Final = (
    "The server has no session signing secret, so no cookie can be issued. Only "
    "reachable if startup did not complete."
)

#: Serialises CHECK-THEN-WRITE on the stored hash across the setup and
#: change-password routes. One lock for both, because they contend on the same
#: file: setup must not overwrite a hash a change wrote a moment earlier, and
#: two setups racing on first run must not both pass the "no source" check
#: (``os.replace`` is last-writer-wins — see app/auth/source.py).
#:
#: An ``asyncio.Lock`` rather than a ``threading.Lock``: the routes are async
#: and a thread lock held across an await would block the event loop. It binds
#: no loop at construction time on Python 3.10+, so declaring it at import is
#: safe.
_PASSWORD_WRITE_LOCK: Final = asyncio.Lock()

_LOGIN_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    401: {
        "model": ErrorDetail,
        "description": (
            "The password did not match, or the configured password hash is not "
            "usable — either nothing is configured, or the value in "
            "MUSICDROP_PASSWORD_HASH cannot be read, or the stored hash file "
            "cannot be read."
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
    503: {"model": ErrorDetail, "description": _NO_SECRET_503_DESCRIPTION},
}

_SETUP_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    409: {
        "model": ErrorDetail,
        "description": (
            "A password source already exists, so first-run setup is closed: "
            "either MUSICDROP_PASSWORD_HASH is set (it wins even when its value "
            "cannot be read), or the hash file under the beets directory is "
            'already there. Available only while password_source is "none".'
        ),
    },
    422: validation_or_detail_422(
        "The password was empty or only whitespace. There is deliberately no "
        "other policy — no minimum length, no character classes."
    ),
    429: {"model": ErrorDetail, "description": _BUSY_429_DESCRIPTION},
    503: {
        "model": ErrorDetail,
        "description": (
            "The server has no session signing secret (startup did not "
            "complete), or the hash could not be written to the beets directory. "
            "Nothing is half-written in either case."
        ),
    },
}

_CHANGE_PASSWORD_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    403: {
        "model": ErrorDetail,
        "description": (
            "The current password did not match. Deliberately NOT a 401: the "
            "session is still valid, and a 401 on a gated path is what the "
            'client treats as "you have been signed out".'
        ),
    },
    409: {
        "model": ErrorDetail,
        "description": (
            "The password cannot be changed in this server's state: "
            "MUSICDROP_PASSWORD_HASH is set and overrides the stored hash, or "
            "there is no readable stored hash to verify against (nothing "
            "configured yet, or a hash file this process cannot read)."
        ),
    },
    422: validation_or_detail_422(
        "The new password was empty or only whitespace. There is deliberately no "
        "other policy — no minimum length, no character classes."
    ),
    429: {"model": ErrorDetail, "description": _BUSY_429_DESCRIPTION},
    503: {
        "model": ErrorDetail,
        "description": (
            "The new hash could not be written to the beets directory; the old "
            "password still works. The route also answers 503 if the server has "
            "no session signing secret, but on this GATED path the gate refuses "
            "that state with a 401 first (measured), so that arm is defence in "
            "depth rather than a reachable answer."
        ),
    },
}


def _refusal_detail(source: PasswordSource) -> str:
    """Why an unusable password hash refused this login.

    Three sentences rather than one because the operator IS the only user here:
    "nothing is configured", "the env var cannot be read" and "the stored file
    cannot be read" need three different fixes, and the login screen shows the
    sentence verbatim. None of them leaks anything an unauthenticated caller
    could not learn from ``password_set`` and ``password_source`` on
    ``/api/auth/status``, which report the same three states by design.
    """
    if source == "env":
        return _UNREADABLE_ENV_HASH_DETAIL
    if source == "file":
        return _UNREADABLE_FILE_HASH_DETAIL
    return _NO_PASSWORD_DETAIL


def _already_configured_detail(source: PasswordSource) -> str:
    """Why first-run setup is closed, and how to re-open it.

    Two different fixes: unset the env var and restart, or change the password
    from Settings (deleting the file and restarting is the forgotten-password
    path). ``"none"`` never reaches here — the caller checks the source first —
    and falls back to the file sentence, which is the safe thing to say about a
    server that does have a stored hash.
    """
    return _ENV_ALREADY_CONFIGURED_DETAIL if source == "env" else _FILE_ALREADY_CONFIGURED_DETAIL


def _change_refusal_detail(source: PasswordSource) -> str:
    """Why a change-password request cannot be verified at all.

    ``"file"`` means the stored hash exists and this process cannot read it, so
    the recovery is deleting it; ``"none"`` means there is nothing to change and
    the sign-in screen's setup form is the way in.
    """
    return _UNREADABLE_FILE_HASH_DETAIL if source == "file" else _NOTHING_TO_CHANGE_DETAIL


def _reject_a_blank_password(candidate: str) -> None:
    """The one password policy either writing route has, mirroring the CLI.

    ``app/auth/hash_password.py`` refuses a password that is empty or only
    whitespace, and this route applies the same rule — a whitespace-only one is
    an empty password with a stray keystroke. Nothing else is enforced: see
    :class:`app.models.auth.LoginRequest` for why a length ceiling would not
    help, and the 422 descriptions for the deliberate absence of a minimum.
    """
    if not candidate.strip():
        raise HTTPException(status_code=422, detail=_BLANK_PASSWORD_DETAIL)


def _store_password_hash(stored: str) -> None:
    """Persist a new hash, or 503 with nothing half-written.

    The atomic writer publishes with ``os.replace``, so a failure here leaves
    the previous file (or no file) exactly as it was — which is what lets the
    detail promise that nothing changed. ``OSError`` covers the reachable
    cases: a read-only mount, a full disk, a data directory the container user
    cannot write.
    """
    try:
        write_password_hash(stored)
    except OSError as exc:
        # %r on both: the path is operator-controlled and the exception quotes
        # it back, so a newline in either could forge a second log line.
        logger.warning("could not write the password hash: %r", exc)
        raise HTTPException(status_code=503, detail=_WRITE_FAILED_DETAIL) from exc


def _issue_session_cookie(response: Response, request: Request, secret: bytes, stored: str) -> None:
    """The ONE Set-Cookie block, shared by login, setup and change-password.

    Shared rather than repeated because the three have to agree: the cookie is
    bound to the hash it was minted under (rotating the hash invalidates every
    other outstanding cookie), and its ``Secure`` flag has to match the one
    ``logout`` will send or sign-out becomes a silent no-op over plain HTTP —
    see the block above ``delete_cookie`` below.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        # Bound to the hash this request just verified against or wrote, so
        # rotating the password invalidates every OTHER outstanding cookie.
        value=mint_session_token(secret, stored),
        max_age=SESSION_MAX_AGE_SECONDS,
        path="/",
        httponly=True,
        samesite="lax",
        # Secure only when THIS request arrived over HTTPS — a per-request
        # decision, not a property of the build. Over plain HTTP (the by-IP
        # LAN deployment) the flag has to be absent or the browser would never
        # send the cookie back and the app could not be signed into at all;
        # behind a TLS proxy it costs nothing and keeps the session off any
        # plain-HTTP hop. app/auth/cookies.py carries the spoof analysis, and
        # why X-Forwarded-Proto is trusted here while the host guard refuses
        # to trust X-Forwarded-Host.
        secure=request_is_https(request),
    )


def _session_secret(request: Request) -> bytes:
    secret = getattr(request.app.state, "session_secret", None)
    if not isinstance(secret, bytes):
        # Fail closed, and honestly: the password may well have been right, but
        # no cookie can be signed. Never mint a per-request secret to paper
        # over it — every later request would be checked against a key this one
        # alone knew.
        raise HTTPException(status_code=503, detail=_NO_SECRET_DETAIL)
    return secret


@asynccontextmanager
async def _one_derive_at_a_time() -> AsyncIterator[None]:
    """Hold the single derive slot for the body, or refuse with a 429.

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

    The whole cancel scope lives BEFORE the yield, so no scope crosses the
    caller's body — an anyio scope that did would be exited in a different task
    context than it was entered in.
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
        yield
    finally:
        _VERIFY_LIMITER.release()


async def _verify_serialised(candidate: str, stored: str) -> bool:
    """One scrypt VERIFY, under the shared derive slot.

    ``abandon_on_cancel=False`` and no ``limiter=`` kwarg on the derive itself:
    admission is already held, and passing the same limiter again would
    self-deadlock. The derive therefore draws one token from anyio's DEFAULT
    thread limiter (40) — bounded at one by the admission above, so 39 remain
    for every other blocking call in the app.
    """
    async with _one_derive_at_a_time():
        return await anyio.to_thread.run_sync(verify_password, candidate, stored)


async def _hash_serialised(password: str) -> str:
    """One scrypt DERIVE for a new password, under the same slot as a verify.

    Hashing costs exactly what verifying costs (``app/auth/passwords.py`` uses
    the same n=2**17), so running it outside the limiter would re-open the
    memory ceiling the limiter exists to enforce — a change-password request
    spends two of these turns, one after the other, never nested.
    """
    async with _one_derive_at_a_time():
        return await anyio.to_thread.run_sync(hash_password, password)


@router.post("/auth/login", responses=_LOGIN_RESPONSES)
async def login(body: LoginRequest, request: Request, response: Response) -> AuthStatus:
    """Exchange the password for a session cookie."""
    # Resolved at REQUEST time, never captured at import: the env var is read
    # through the settings SINGLETON (attribute-patching ``app.config.settings``
    # is how the suite overrides configuration) and the file is read off the
    # disk, where the setup and change-password routes rewrite it while the
    # process runs.
    stored, source = effective_password()
    if not password_is_configured(stored):
        raise HTTPException(status_code=401, detail=_refusal_detail(source))
    secret = _session_secret(request)
    if not await _verify_serialised(body.password, stored):
        raise HTTPException(status_code=401, detail=_WRONG_PASSWORD_DETAIL)
    _issue_session_cookie(response, request, secret, stored)
    return AuthStatus(authenticated=True, password_set=True, password_source=source)


@router.post("/auth/setup", responses=_SETUP_RESPONSES)
async def setup_password(body: SetupRequest, request: Request, response: Response) -> AuthStatus:
    """Set the FIRST password, on a server that has none, and sign the caller in.

    Gate-exempt by necessity — there is no credential to hold a session with
    yet — and therefore refused with a 409 the moment any source exists. The
    two halves of that decision (read the source, write the file) run under one
    lock, because the atomic writer publishes with ``os.replace``, which is
    last-writer-wins rather than create-or-fail: without the lock two
    simultaneous first-run POSTs would each pass the check and the second would
    overwrite the first, leaving the operator holding a cookie for a password
    that is no longer stored. The image runs a single uvicorn worker by design,
    so an in-process lock is what "first wins" means here; a multi-worker
    deployment would need the check in the filesystem instead.

    The 503 arm runs BEFORE anything is written: a cookie that cannot be signed
    would leave a password stored and nobody able to use it until a restart.
    """
    _reject_a_blank_password(body.password)
    secret = _session_secret(request)
    async with _PASSWORD_WRITE_LOCK:
        _, source = effective_password()
        if source != "none":
            raise HTTPException(status_code=409, detail=_already_configured_detail(source))
        stored = await _hash_serialised(body.password)
        _store_password_hash(stored)
    # WARNING, not info: this is the moment the instance stopped being claimable
    # by whoever reached it first, and deleting the file re-opens that on the
    # running process with no restart and no other signal. An operator reading
    # the log has to be able to see both the claim and where the credential now
    # lives. Neither the password nor the hash is logged, here or anywhere.
    operator_logger.warning("first-run setup stored a password at %r", password_file_location())
    _issue_session_cookie(response, request, secret, stored)
    return AuthStatus(authenticated=True, password_set=True, password_source="file")


@router.post("/auth/password", responses=_CHANGE_PASSWORD_RESPONSES)
async def change_password(
    body: ChangePasswordRequest, request: Request, response: Response
) -> AuthStatus:
    """Replace the stored password, and re-mint THIS caller's cookie.

    Gated, and it still asks for the current password: the session cookie is a
    30-day bearer token with no server-side record, so possession of one must
    not be enough to replace the credential it was minted from.

    Every OTHER live session is signed out by this, and that is not a side
    effect to design away — the signing key is derived from the password hash
    (``app/auth/session.py::_signing_key``), which is the only revocation this
    stateless session design has. The caller's own cookie is re-minted under
    the new hash so the browser that made the change stays signed in.

    A wrong current password is a 403, deliberately NOT a 401: on a gated path
    the client reads any 401 as "your session ended" and drops the user at the
    sign-in screen, which would turn a typo into a sign-out.
    """
    _reject_a_blank_password(body.new_password)
    secret = _session_secret(request)
    async with _PASSWORD_WRITE_LOCK:
        stored, source = effective_password()
        if source == "env":
            raise HTTPException(status_code=409, detail=_ENV_OVERRIDE_DETAIL)
        if not password_is_configured(stored):
            # Nothing readable to verify the current password against. Refusing
            # is the honest answer: accepting would let a caller REPLACE an
            # unreadable stored hash without proving they knew the old one.
            raise HTTPException(status_code=409, detail=_change_refusal_detail(source))
        if not await _verify_serialised(body.current_password, stored):
            raise HTTPException(status_code=403, detail=_WRONG_CURRENT_PASSWORD_DETAIL)
        new_stored = await _hash_serialised(body.new_password)
        _store_password_hash(new_stored)
    # INFO rather than WARNING: an expected administrative action, where setup is
    # a one-way change of the instance's posture. Same rule about what is in it.
    operator_logger.info("the stored password was changed at %r", password_file_location())
    _issue_session_cookie(response, request, secret, new_stored)
    return AuthStatus(authenticated=True, password_set=True, password_source="file")


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
    # original in place and "logout" would do nothing. Name and path are the
    # only MATCHING keys, but do not read that as "the rest are cosmetic":
    # Secure decides whether this Set-Cookie is stored at all, which is the
    # next paragraph.
    #
    # Secure is RECOMPUTED from this request rather than remembered, which is
    # the only way it could be: a logout is a different request from the login,
    # and nothing server-side records what the login decided.
    #
    # Keeping it symmetric with the login call site is LOAD-BEARING, not
    # tidiness. A Set-Cookie carrying Secure that arrives over a non-secure
    # connection is dropped before it is ever stored, so it can expire nothing
    # — draft-ietf-httpbis-rfc6265bis-20 section 5.7 ("Storage Model") step 13:
    # "If the request-uri does not denote a 'secure' connection (as defined by
    # the user agent), and the cookie's secure-only-flag is true, then abort
    # these steps and ignore the cookie entirely." That step is new in bis; the
    # published RFC 6265 (2011) has no equivalent, but browsers do implement it
    # (MDN, Set-Cookie: "Insecure sites (http:) cannot set cookies with the
    # Secure attribute"). So hardcoding secure=True here would make sign-out
    # over plain HTTP a SILENT no-op: 204 returned, session cookie still live.
    # Only the opposite mismatch — no Secure, over TLS — is harmless, and that
    # is the one direction this comment used to describe.
    #
    # No test can pin the browser rule, so do not add one that appears to:
    # http.cookiejar runs no secure check when STORING (DefaultCookiePolicy
    # defines set_ok_{version,verifiability,name,path,domain,port} and no
    # set_ok_secure), so httpx and therefore TestClient will happily keep a
    # cookie a browser would drop. test_logout_matches_logins_secure_posture
    # pins the part that IS real and checkable: both call sites derive the flag
    # from the same property of the request in front of them.
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
    stored, source = effective_password()
    return AuthStatus(
        authenticated=scope_has_valid_session(request.scope),
        password_set=password_is_configured(stored),
        password_source=source,
    )
