import asyncio
import logging
import os
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import library_busy
from app.api.acquisition import router as acquisition_router
from app.api.albums import router as albums_router
from app.api.artists import router as artists_router
from app.api.auth import router as auth_router
from app.api.bank import get_bank_dir
from app.api.bank import router as bank_router
from app.api.browse import router as browse_router
from app.api.config_ import router as config_router
from app.api.disk_sync import router as disk_sync_router
from app.api.duplicates import router as duplicates_router
from app.api.events import router as events_router
from app.api.health import router as health_router
from app.api.import_ import router as import_router
from app.api.lyrics import router as lyrics_router
from app.api.playlists import router as playlists_router
from app.api.plex import router as plex_router
from app.api.reorganize import router as reorganize_router
from app.api.search import router as search_router
from app.api.slskd import router as slskd_router
from app.api.stats import router as stats_router
from app.api.trash import router as trash_router
from app.artwork.cache import ArtistImageCache
from app.artwork.cover_thumbs import CoverThumbCache
from app.artwork.factory import ArtistImageSources, build_artist_image_sources
from app.artwork.filler import ArtistImageFiller
from app.artwork.rate_limit import TokenBucketLimiter
from app.artwork.service import ArtistImageService
from app.artwork.toggle import ArtistArtWriteToggle, ArtistImageToggle
from app.auth.gate import SessionGateMiddleware, boot_auth_posture
from app.auth.session import load_or_create_session_secret, session_secret_path
from app.bank.store import reconcile_interrupted
from app.beets.library import LibraryHandle, close_library
from app.beets.setup import setup_beets
from app.beets.store_layout import StoreLayoutError, checked_store_dirs
from app.body_limit import BodySizeLimitMiddleware
from app.config import resolve_artist_image_cache_dir, resolve_cover_thumb_cache_dir, settings
from app.events.emit import emit_art_changed
from app.host_guard import HostGuardMiddleware, resolve_allowed_hosts
from app.openapi_overlay import overlay_middleware_responses
from app.origin_guard import OriginGuardMiddleware, resolve_extra_origins
from app.playlists.store import get_playlists_dir
from app.security_headers import SecurityHeadersMiddleware
from app.static_files import mount_static
from app.wire import SurrogateSafeJSONResponse, install_wire_safety

# Shutdown grace: after the inbox drain is stopped, poll the import slot for up
# to TICKS * INTERVAL seconds (~5s) so an import already in flight gets a
# best-effort moment to release the slot (commit its DB row) before we close the
# library's SQLite connection. The beets worker runs on its own daemon thread we
# cannot join, so this only narrows — never eliminates — the shutdown race.
_SHUTDOWN_IMPORT_DRAIN_TICKS = 50
_SHUTDOWN_IMPORT_DRAIN_INTERVAL = 0.1


def _resolve_library() -> LibraryHandle:
    """Run beets' startup and return the opened library handle.

    Delegates to setup_beets, which mirrors beets' own _setup: ensure BEETSDIR
    exists, copy the starter config.yaml on first run, force-resolve confuse,
    load the plugins listed in the user's config, then open the library with
    path formats + replacements and fire library_opened. Always returns a
    handle (the dir/file are created if missing). Sync helper: runs once at
    startup (cold path).
    """
    return setup_beets(
        settings.beets_dir,
        # static_dir doubles as the "running from the image" marker.
        container_music_default=bool(settings.static_dir),
    )


#: What beets' own startup raises for a value it cannot open, measured on this
#: tree by calling ``setup_beets`` directly: ``sqlite3.OperationalError`` for a
#: ``library:`` that is a symlink loop, ``ValueError`` for one holding a NUL, and
#: ``RuntimeError`` from ``Path.resolve`` for a ``MUSICDROP_BEETS_DIR`` that is a
#: symlink loop. Each already failed CLOSED; what was missing was the one
#: level-tagged line naming a setting that the layout gate below prints.
_BEETS_STARTUP_FAILED = (OSError, RuntimeError, ValueError, sqlite3.Error)


def _boot_log() -> logging.Logger:
    """The logger a refusal to start goes to.

    ``uvicorn.error`` and not this module's own: under the Dockerfile CMD an
    app-namespace record never reaches the container's output at all, and the
    operator grepping for why the process died has only ``docker logs``.
    """
    return logging.getLogger("uvicorn.error")


def _leftovers_note(handle: LibraryHandle) -> str:
    """Where to look for what this start wrote before the layout gate refused it.

    beets' startup has to run first: the gate compares the ``directory:`` and
    ``library:`` beets LOADS, and neither is known until confuse has resolved the
    config — which means the config must exist, which on a first run means
    writing the starter. So the earliest honest point is here, after the fact.

    It names the two directories rather than a file list because only some of
    what lands is new. Measured: a refused ``MUSICDROP_BEETS_DIR=<music>`` left 13
    files inside the music library (config.yaml, library.db and 11 of beets'
    migration backups) and a refused ``library: trash/library.db`` left 12 under
    the Trash dir. They are litter, not Trash entries — ``list_trashed_albums``
    returned ``[]`` with all 12 present — so nothing removes them on its own.
    """
    db_dir = Path(os.fsdecode(handle.lib.path)).parent
    places = {str(handle.beets_dir), str(db_dir)}
    return (
        " beets' own startup ran first, because it is what supplies the"
        " `directory:` and `library:` this check compares, and it creates the"
        " beets data directory, config.yaml and the database as it goes:"
        f" look in {', '.join(sorted(places))} for files this start left behind."
    )


def _build_artist_image_service(
    cache: ArtistImageCache,
    is_enabled: Callable[[], bool],
    sources: ArtistImageSources,
) -> ArtistImageService:
    limiter = TokenBucketLimiter(
        rate_per_sec=settings.artist_image_rate_per_sec,
        max_concurrency=settings.artist_image_max_concurrency,
    )
    return ArtistImageService(
        # The chain wraps the registry's instances: the manual per-source fetch
        # addresses the SAME objects, so credentials and Spotify's token cache
        # exist exactly once. Taking the registry as a parameter (rather than
        # building one here) is what makes a second construction impossible.
        source=sources.chain(),
        cache=cache,
        limiter=limiter,
        is_enabled=is_enabled,
        negative_ttl_seconds=settings.artist_image_negative_ttl_seconds,
        transient_ttl_seconds=settings.artist_image_transient_ttl_seconds,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Open the beets library once at startup (a SQLite connection we keep for
    # the process lifetime) and close it on shutdown. setup_beets always returns
    # a handle — missing BEETSDIR / config.yaml are created from the starter —
    # so there is no "library disabled" branch in production.
    #
    # The ``except`` is here because this runs BEFORE the layout gate below and
    # can fail on the same class of operator input: a value beets cannot open
    # used to leave a bare traceback with no line saying which setting to look
    # at. It re-raises — the process still does not come up — and only adds the
    # line.
    try:
        handle = _resolve_library()
    except _BEETS_STARTUP_FAILED as exc:
        _boot_log().error(
            "refusing to start: beets could not open the library under %s=%r. %s: %s."
            " Check `library:` and `directory:` in that directory's config.yaml.",
            "MUSICDROP_BEETS_DIR",
            settings.beets_dir,
            type(exc).__name__,
            exc,
        )
        raise

    # Before ANYTHING is attached or started: refuse to come up when Trash or
    # the Trash origin store sits somewhere using it would destroy data (see
    # app/beets/store_layout.py for the table and the reasoning). The check is
    # here rather than inside a request path because all four inputs are settled
    # exactly once — three come from the environment, and the fourth
    # (``directory:``) only moves through Save/Apply, which run the same check.
    # Logged through ``uvicorn.error`` (see ``_boot_log``). ERROR, not WARNING —
    # the process does not come up, and the operator grepping for the reason
    # after "Application startup failed" must find a line whose level says so.
    #
    # The PAIR it returns is what the import registry is handed below, so the
    # boot gate and the values the process actually runs on come from one call.
    # Taking them from the bare resolvers afterwards, as this did, left a window
    # where a Trash swapped between the two reached ``attach_library`` unchecked.
    try:
        boot_trash_dir, boot_origins_dir = checked_store_dirs(settings, handle)
    except StoreLayoutError as exc:
        _boot_log().error("refusing to start: %s%s", exc, _leftovers_note(handle))
        # Give back the SQLite connection ``_resolve_library`` just opened. The
        # raise below skips the ``finally`` teardown further down (it has not
        # been entered yet), so this is the only place that can.
        close_library(handle.lib)
        raise

    app.state.beets_library = handle

    # Settings + the swap lock back the Apply endpoint (Task 8). The lock
    # serialises the in-process beets-globals teardown + rebuild so two
    # concurrent Applies cannot trample each other's ``app.state.beets_library``
    # swap; ``settings`` is exposed so the handler reads ``beets_dir`` without
    # re-importing the module (and so tests can monkeypatch it on a single
    # surface).
    app.state.settings = settings
    # The session gate reads its signing secret off ``app.state`` on every
    # request, because the middleware stack is declared at import time while
    # the secret lives under ``settings.beets_dir`` — a value that is not
    # settled until now. Seeded ONLY IF ABSENT: the test suite pre-seeds a
    # fixed secret on this same surface (tests/conftest.py), mirroring how
    # ``app.state.settings`` exists to be monkeypatched, and overwriting it
    # here would 401 every request made inside a ``with TestClient(app)``
    # block. Correspondingly the teardown deletes it only if THIS lifespan
    # created it, so a pre-seed survives the block it wrapped.
    created_session_secret = not hasattr(app.state, "session_secret")
    if created_session_secret:
        app.state.session_secret = load_or_create_session_secret(
            session_secret_path(settings.beets_dir)
        )
    app.state.beets_swap_lock = asyncio.Lock()
    # Let the cross-job claim gate see beets swaps too — a config Apply holds
    # this lock while tearing down the Library handle but claims no job slot.
    library_busy.register_swap_lock(app.state.beets_swap_lock)

    from app.events.broker import EventBroker

    app.state.event_broker = EventBroker(loop=asyncio.get_running_loop())

    from app.import_jobs.registry import registry as import_registry

    # The import runner builds a WebImportSession from a beets Library, so feed
    # it the raw lib (not the snapshot handle). The Trash dir is where the
    # duplicate-on-import Replace action moves the old copies (same reversible
    # Trash the /duplicates page uses) — and it is the pair the gate above
    # checked, not a second resolve of the same setting.
    import_registry.attach_library(
        handle.lib,
        boot_trash_dir,
        bank_dir=get_bank_dir(),
        playlists_dir=get_playlists_dir(),
        trash_origins_dir=boot_origins_dir,
    )
    import_registry.attach_event_broker(app.state.event_broker)

    # The acquisition seam drives completed inbox drops through the SAME single
    # import slot (Option A) — constructed AFTER attach_library so it shares that
    # registry, and started here so it can drain in the background. It defers
    # while the import slot / backfills / the swap lock are busy (it consumes the
    # existing gate; it is not a new mutex participant). Built before the ``try``
    # so it is in scope for the ``finally`` teardown.
    from app.acquisition.inbox import resolve_inbox_dir
    from app.acquisition.ledger import AcquisitionLedger
    from app.acquisition.queue import AcquisitionQueue

    inbox_dir = resolve_inbox_dir(settings, handle)
    ledger = AcquisitionLedger(inbox_dir / ".musicdrop-ledger.json")
    acquisition_queue = AcquisitionQueue(
        import_registry=import_registry,
        ledger=ledger,
        inbox_dir=inbox_dir,
        swap_lock=app.state.beets_swap_lock,
    )
    app.state.acquisition_queue = acquisition_queue
    app.state.inbox_dir = inbox_dir
    app.state.acquisition_ledger = ledger  # the Review page lists + annotates the inbox backlog
    acquisition_queue.start()

    # Bank reconciliation: rows stuck in "applying" from a mid-apply crash
    # revert to needs_review with a note (never blind-requeued).
    reconcile_interrupted(get_bank_dir())

    # The bank apply runner drains decided (queued) rows through the SAME
    # single import slot, deferring on the same gate union the acquisition
    # queue consumes. Started AFTER reconciliation so a crashed mid-apply row
    # is back in needs_review before the first drain pass; queued rows from
    # before the restart drain immediately - no decision re-post needed.
    from app.bank.apply_runner import BankApplyRunner

    def live_library() -> LibraryHandle:
        """The CURRENT library handle, read at call time.

        The config editor's Apply rebuilds beets and swaps
        ``app.state.beets_library`` mid-process, so the runner must not capture
        ``handle`` from above: its duplicate-enforcement check would then read
        the pre-Apply database. A getter is the whole fix.
        """
        current: LibraryHandle = app.state.beets_library
        return current

    bank_apply_runner = BankApplyRunner(
        bank_dir=get_bank_dir(),
        import_registry=import_registry,
        library=live_library,
        swap_lock=app.state.beets_swap_lock,
    )
    app.state.bank_apply_runner = bank_apply_runner
    bank_apply_runner.start()

    # Build the artist-image stack once: the disk cache + the persisted enabled
    # toggle are shared on app.state so the override + settings endpoints reach
    # the SAME instances the service uses. The cache dir is created lazily on
    # first write, so no startup mkdir.
    cache = ArtistImageCache(resolve_artist_image_cache_dir())
    # Derived album-cover thumbnails — a separate on-disk cache (the cover
    # ITSELF isn't cached here; that's beets' library, not this cache's job).
    # Dir is created lazily on first write, same as the artist-image cache.
    app.state.cover_thumb_cache = CoverThumbCache(resolve_cover_thumb_cache_dir())
    toggle = ArtistImageToggle(
        resolve_artist_image_cache_dir() / "_enabled.json", default=settings.artist_images_enabled
    )
    app.state.artist_image_cache = cache
    app.state.artist_image_toggle = toggle
    art_write_toggle = ArtistArtWriteToggle(
        resolve_artist_image_cache_dir() / "_art_write_enabled.json",
        default=settings.artist_art_write_enabled,
    )
    app.state.artist_art_write_toggle = art_write_toggle
    http_client = httpx.AsyncClient(
        timeout=10.0,
        headers={
            "User-Agent": (
                f"{settings.app_name}/{settings.version} (+https://github.com/Elienop/musicdrop)"
            )
        },
    )
    app.state.artist_image_http_client = http_client
    # ONE set of source objects per httpx client: the automatic chain and the
    # by-id fetch the API serves both address these instances, so Spotify's
    # client-credentials token is cached once instead of twice.
    artist_image_sources = build_artist_image_sources(http_client, settings)
    app.state.artist_image_sources = artist_image_sources
    # The write toggle ALSO enables fetching (one switch): the engine resolves
    # portraits whenever EITHER the image toggle OR the write toggle is on.
    app.state.artist_image_service = _build_artist_image_service(
        cache,
        lambda: toggle.is_enabled() or art_write_toggle.is_enabled(),
        artist_image_sources,
    )
    # Uncached portraits resolve OFF the request path from here on: the filler
    # single-flights one resolve per artist and announces a drained burst with
    # one coalesced repaint. UNSCOPED for the same reason every artist-image
    # mutation is: the asset is served under a NORMALIZED name, so a display
    # name is not a reliable identity. Built before the ``try`` so it is in
    # scope for the ``finally`` teardown.
    artist_image_filler = ArtistImageFiller(on_filled=lambda: emit_art_changed(app))
    app.state.artist_image_filler = artist_image_filler
    from app.artwork.factory import build_fanart_background_source

    app.state.artist_background_source = build_fanart_background_source(http_client, settings)

    try:
        yield
    finally:
        # Stop the inbox drain first (join its thread) so teardown triggers no
        # NEW import. The drain runs each beets import on its own daemon worker
        # thread that we cannot join, so an import already in flight — an
        # auto-triggered inbox drop OR a manual import — may still own the single
        # slot here. Give it a bounded, best-effort moment to release that slot
        # (and commit its DB row) before we close the SQLite connection beneath
        # it: the same best-effort posture a manual import running at shutdown
        # already has — we never hard-kill the worker.
        acquisition_queue.stop()
        bank_apply_runner.stop()
        for _ in range(_SHUTDOWN_IMPORT_DRAIN_TICKS):
            if not import_registry.has_active_job():
                break
            await asyncio.sleep(_SHUTDOWN_IMPORT_DRAIN_INTERVAL)
        # Cancel outstanding background fills BEFORE the client they fetch
        # through is closed, so a shutdown mid-resolve raises CancelledError
        # (which the filler expects) rather than "client has been closed".
        await artist_image_filler.close()
        await http_client.aclose()
        close_library(handle.lib)
        # Remove the broker before the event loop is torn down so that any
        # subsequent test that skips the lifespan (and therefore has no broker)
        # does not find a stale EventBroker whose loop is already closed.
        del app.state.event_broker
        # Same hazard for the source registry: its sources hold the httpx client
        # just closed above, and unlike the other artwork state its getter has a
        # FALLBACK — a leaked registry would shadow that fallback, so a later
        # test skipping the lifespan would fetch through a closed client instead
        # of building its own. Both attributes are set before the `try`, so the
        # deletes cannot race a half-built lifespan.
        del app.state.artist_image_sources
        # Only what this lifespan created: a secret pre-seeded by the suite
        # must outlive the block, or every test after the first
        # ``with TestClient(app)`` would run against a gate with no key and 401.
        if created_session_secret:
            del app.state.session_secret


class App(FastAPI):
    """FastAPI whose served OpenAPI schema also declares the ASGI guards' 400/401/403/413.

    ``host_guard`` (400), ``auth/gate`` (401 on every gated path),
    ``origin_guard`` (403 on the UNSAFE_METHODS writes), and ``body_limit``
    (413 on bodied requests) reject requests before any route runs, so no
    route's ``responses=`` can describe them; ``overlay_middleware_responses``
    adds them to every operation instead. ``super().openapi()`` keeps FastAPI's
    ``openapi_schema`` cache (the base schema is computed once) and the overlay
    is idempotent on repeat calls.

    It never touches a declared RESPONSE entry or any 422. It does set
    ``security`` unconditionally — ``[]`` on the five gate-exempt operations,
    and the document-wide requirement at the top level — because that is a fact
    about the middleware rather than a description a route could know better.
    """

    # dict[str, Any], not dict[str, object]: this override keeps FastAPI's own
    # signature, so existing consumers (tests subscript the schema directly)
    # stay natural instead of isinstance-narrowing at every step.
    def openapi(self) -> dict[str, Any]:
        return overlay_middleware_responses(super().openapi())


app = App(
    title=settings.app_name,
    version=settings.version,
    lifespan=lifespan,
    # A filesystem path that is not valid UTF-8 must degrade to a placeholder,
    # never 500 the endpoint that mentions it — see app/wire.py for why the
    # scrub lives at the sink rather than at each path-to-wire boundary.
    default_response_class=SurrogateSafeJSONResponse,
)
install_wire_safety(app)

extra_origins = resolve_extra_origins(settings.static_dir)

allowed_hosts = resolve_allowed_hosts(settings.static_dir, settings.allowed_hosts)

# The middleware stack, innermost first: session gate, origin guard, body
# limit, host guard, security headers, CORS. Starlette applies add_middleware
# in REVERSE add order, so each block below wraps the ones above it.
#
# Innermost of them all — the last thing before the router. Three properties
# make that the right seat rather than an arbitrary one:
#  * the security-headers stamper wraps outside every rejecting guard, so a 401
#    is stamped with nosniff/CSP exactly like the 400/403/413 are (pinned by
#    test_unauthenticated_401_carries_the_security_headers);
#  * CORS still answers preflights outermost, and OPTIONS never reaches here;
#  * the cheaper guards keep winning: a wrong Host is still 400, an oversize
#    body still 413 and a foreign-origin write still 403, all without spending
#    an HMAC — so every existing precedence pin is untouched and
#    test_disallowed_host_beats_the_session_gate pins the new pair.
# Authentication being INSIDE the CSRF guard also means a cross-origin write
# from a logged-in browser is refused as cross-origin (403), not accepted as
# authenticated: the session cookie does not buy a page the right to use it.
# That ordering is doing real work, because the cookie's SameSite=Lax is
# WEAKER here than it looks: cookies are scoped to a host, not a port, so any
# other HTTP service on the same LAN IP is same-SITE and Lax does nothing
# against it. What actually stops that neighbour is the origin guard's
# port-aware authority compare. Do not weaken it on the belief SameSite
# covers this.
app.add_middleware(SessionGateMiddleware)

# Added after the session gate above, so it wraps outside it: a cross-origin
# write is refused as CSRF before the gate spends an HMAC deciding whether the
# forged request was also signed in. BodySizeLimit MUST wrap outside THIS one
# so an oversize
# body is refused before the origin guard (or CORS) touches it — that one is
# load-bearing and pinned by test_oversize_body_beats_the_origin_guard. (CORS
# does wrap outside the guard — it must, to sit outermost per python:S8414 —
# but that position is not load-bearing for preflights: CORS answers them
# itself, and the guard ignores OPTIONS by construction, so preflights are
# answered either way.)
# (A guard 403 carries no Access-Control-Allow-Origin header — CORSMiddleware
# still stamps Allow-Credentials: the guard's allowed origins are a superset of
# the CORS allowlist, so an origin the guard rejects was never CORS-approved
# either — the rejection is opaque to a foreign page, which is fine.)
app.add_middleware(OriginGuardMiddleware, extra_origins=extra_origins)
# Added after the origin guard above so it wraps outside it: an oversize body
# is refused before any route touches it — CORS wraps outside it and sees the
# request first, but it never reads the body, so the oversize bytes stop here.
app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)

# Added after BodySizeLimit so it wraps outside those two: a request whose
# Host is not allowlisted (the DNS-rebinding guard) is refused before the body
# limit or the origin guard spend anything on it. CORS now sits outside it and
# runs first on every request, but preflight OPTIONS never reach this guard —
# CORSMiddleware answers them itself. Pinned by
# test_disallowed_host_beats_the_body_limit.
app.add_middleware(HostGuardMiddleware, allowed_hosts=allowed_hosts)

# Added after the four guards so it wraps OUTSIDE ALL OF THEM: the guards
# above write their rejections (400/413/403/401) straight to the transport without
# reaching the router, so this is the only position from which the security
# headers land on them too. It does not need to be outermost overall — CORS
# wraps outside it (added last, for python:S8414, below) — and it never
# rejects, buffers or reorders: it edits the response head in flight, so the
# precedence the two ordering pins above describe is unchanged. Pinned by
# test_security_headers_wrap_outside_the_guards.
app.add_middleware(SecurityHeadersMiddleware)

# Added LAST of all, so it wraps OUTERMOST — the position python:S8414
# requires for CORSMiddleware. The one trade of sitting outside the stamper:
# CORS answers OPTIONS preflights itself at this outermost layer, so those
# responses never reach the stamper and carry no security headers — inert on a
# bodiless OPTIONS response (nothing renders, nothing is framed, nothing is
# embedded, no body to sniff). It never rejects an ordinary request, so the
# guard order above is unchanged. The preflight shape is pinned by
# test_cors_preflight_is_answered_by_cors_itself.
app.add_middleware(
    CORSMiddleware,
    # Dev: the Vite server. Prod: empty — cross-origin pages get no read
    # access either (owner decision, see the origin-guard spec).
    allow_origins=list(extra_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# One loud line so the deploy-time posture is never silent (`static_dir`
# selects dev/prod for BOTH guards; MUSICDROP_ALLOWED_HOSTS extends the host
# guard). Closes the "static_dir silently controls the CSRF posture" minor.
# Logged through `uvicorn.error`, NOT `__name__`: uvicorn's LOGGING_CONFIG
# configures only its own loggers and leaves root at WARNING with no handlers,
# so an INFO record from `app.main` is discarded before it reaches any output
# under the Dockerfile CMD. Pinned by test_posture_log_emits_under_real_uvicorn.
#
# The session-cookie clause is a fixed RULE, not a %s state, and deliberately
# so: the Secure flag is decided per request from that request's scheme
# (app/auth/cookies.py), so there is no boot-time value to report. Wording it
# as a state would be a claim the process cannot make — an operator behind a
# TLS proxy and one on the LAN read the same line and both read the truth.
logging.getLogger("uvicorn.error").info(
    "security posture: %s; extra write origins: %s; allowed hosts: IP literals, localhost%s;"
    " auth: %s; session cookie: Secure on HTTPS requests, plain otherwise",
    "prod (static_dir set)" if settings.static_dir else "dev (static_dir empty)",
    ", ".join(extra_origins) or "none",
    "".join(f", {name}" for name in allowed_hosts),
    # The auth clause is the difference between a locked deployment and one
    # that refuses everything: an unset (or unreadable) password hash is
    # indistinguishable from a working one until the first login fails, and this
    # line is the only place it is ever said out loud. It reports the SOURCE
    # too, because with two of them (env var, stored file) an operator who
    # removed the compose line has to be told the stored one is still there.
    # Resolved once, at import, and read off the disk: a file written after boot
    # is invisible to this line until the next restart.
    #
    # One no-argument call rather than unpacking the resolver here: the arguments
    # ARE the message, and a call site that builds them is a call site that can
    # get them wrong with nothing to notice. app/auth/gate.py::boot_auth_posture
    # owns the composition and is pinned per state.
    boot_auth_posture(),
)

app.include_router(health_router, prefix="/api")
app.include_router(auth_router, prefix="/api")
app.include_router(events_router, prefix="/api")
app.include_router(albums_router, prefix="/api")
app.include_router(artists_router, prefix="/api")
app.include_router(browse_router, prefix="/api")
app.include_router(search_router, prefix="/api")
app.include_router(import_router, prefix="/api")
app.include_router(config_router, prefix="/api")
app.include_router(duplicates_router, prefix="/api")
app.include_router(lyrics_router, prefix="/api")
app.include_router(reorganize_router, prefix="/api")
app.include_router(disk_sync_router, prefix="/api")
app.include_router(stats_router, prefix="/api")
app.include_router(playlists_router, prefix="/api")
app.include_router(plex_router, prefix="/api")
app.include_router(slskd_router, prefix="/api")
app.include_router(acquisition_router, prefix="/api")
app.include_router(bank_router, prefix="/api")
app.include_router(trash_router, prefix="/api")

# Production single-image mode: serve the built SPA. Registered after every
# API router so the catch-all cannot shadow /api/*. Dev (static_dir unset)
# skips this entirely.
if settings.static_dir:
    mount_static(app, settings.static_dir)
