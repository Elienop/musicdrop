import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import library_busy
from app.api.acquisition import router as acquisition_router
from app.api.albums import router as albums_router
from app.api.artists import router as artists_router
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
from app.bank.store import reconcile_interrupted
from app.beets.library import LibraryHandle, close_library
from app.beets.setup import setup_beets
from app.body_limit import BodySizeLimitMiddleware
from app.config import resolve_artist_image_cache_dir, resolve_cover_thumb_cache_dir, settings
from app.events.emit import emit_art_changed
from app.origin_guard import OriginGuardMiddleware, resolve_extra_origins
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


def _build_artist_image_service(
    client: httpx.AsyncClient,
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
    handle = _resolve_library()
    app.state.beets_library = handle

    # Settings + the swap lock back the Apply endpoint (Task 8). The lock
    # serialises the in-process beets-globals teardown + rebuild so two
    # concurrent Applies cannot trample each other's ``app.state.beets_library``
    # swap; ``settings`` is exposed so the handler reads ``beets_dir`` without
    # re-importing the module (and so tests can monkeypatch it on a single
    # surface).
    app.state.settings = settings
    app.state.beets_swap_lock = asyncio.Lock()
    # Let the cross-job claim gate see beets swaps too — a config Apply holds
    # this lock while tearing down the Library handle but claims no job slot.
    library_busy.register_swap_lock(app.state.beets_swap_lock)

    from app.events.broker import EventBroker

    app.state.event_broker = EventBroker(loop=asyncio.get_running_loop())

    from app.beets.trash import resolve_trash_dir
    from app.import_jobs.registry import registry as import_registry

    # The import runner builds a WebImportSession from a beets Library, so feed
    # it the raw lib (not the snapshot handle). The Trash dir is where the
    # duplicate-on-import Replace action moves the old copies (same reversible
    # Trash the /duplicates page uses).
    import_registry.attach_library(
        handle.lib, resolve_trash_dir(settings, handle), bank_dir=get_bank_dir()
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

    bank_apply_runner = BankApplyRunner(
        bank_dir=get_bank_dir(),
        import_registry=import_registry,
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
        http_client,
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


app = FastAPI(
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

# Innermost of the three middlewares: CORSMiddleware must wrap the guard so
# preflight OPTIONS are answered before it, and BodySizeLimit wraps OUTERMOST
# so an oversize body is refused before anything else runs. (A guard 403
# carries no CORS headers: the guard's allowed origins are a superset of the
# CORS allowlist, so an origin the guard rejects was never CORS-approved
# either — the rejection is opaque to a foreign page, which is fine.)
app.add_middleware(OriginGuardMiddleware, extra_origins=extra_origins)
app.add_middleware(
    CORSMiddleware,
    # Dev: the Vite server. Prod: empty — cross-origin pages get no read
    # access either (owner decision, see the origin-guard spec).
    allow_origins=list(extra_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Added last so it wraps OUTERMOST (Starlette applies middleware in reverse add
# order): an oversize body is refused before CORS or any route touches it.
app.add_middleware(
    BodySizeLimitMiddleware,
    max_bytes=settings.max_body_bytes,
    allowed_origins=extra_origins,
)

app.include_router(health_router, prefix="/api")
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
