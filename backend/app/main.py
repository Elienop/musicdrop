import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.albums import router as albums_router
from app.api.artists import router as artists_router
from app.api.config_ import router as config_router
from app.api.duplicates import router as duplicates_router
from app.api.health import router as health_router
from app.api.import_ import router as import_router
from app.api.lyrics import router as lyrics_router
from app.api.reorganize import router as reorganize_router
from app.api.search import router as search_router
from app.api.stats import router as stats_router
from app.artwork.cache import ArtistImageCache
from app.artwork.rate_limit import TokenBucketLimiter
from app.artwork.service import ArtistImageService
from app.artwork.toggle import ArtistArtWriteToggle, ArtistImageToggle
from app.beets.library import LibraryHandle, close_library
from app.beets.setup import setup_beets
from app.config import settings

# Repo root is the parent of the backend/ package dir (this file is
# backend/app/main.py). Relative cache paths resolve under it so the
# artist-image cache lands in the gitignored repo-root data/, not backend/data/.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve_library() -> LibraryHandle:
    """Run beets' startup and return the opened library handle.

    Delegates to setup_beets, which mirrors beets' own _setup: ensure BEETSDIR
    exists, copy the starter config.yaml on first run, force-resolve confuse,
    load the plugins listed in the user's config, then open the library with
    path formats + replacements and fire library_opened. Always returns a
    handle (the dir/file are created if missing). Sync helper: runs once at
    startup (cold path).
    """
    return setup_beets(settings.beets_dir)


def _resolve_cache_dir() -> Path:
    """Resolve the artist-image cache dir, anchoring relatives to the repo root."""
    configured = Path(settings.artist_image_cache_dir)
    if configured.is_absolute():
        return configured
    return _REPO_ROOT / configured


def _build_artist_image_service(
    client: httpx.AsyncClient, cache: ArtistImageCache, is_enabled: Callable[[], bool]
) -> ArtistImageService:
    from app.artwork.factory import build_source_chain

    limiter = TokenBucketLimiter(
        rate_per_sec=settings.artist_image_rate_per_sec,
        max_concurrency=settings.artist_image_max_concurrency,
    )
    return ArtistImageService(
        source=build_source_chain(client, settings),
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

    from app.beets.trash import resolve_trash_dir
    from app.import_jobs.registry import registry as import_registry

    # The import runner builds a WebImportSession from a beets Library, so feed
    # it the raw lib (not the snapshot handle). The Trash dir is where the
    # duplicate-on-import Replace action moves the old copies (same reversible
    # Trash the /duplicates page uses).
    import_registry.attach_library(handle.lib, resolve_trash_dir(settings, handle))

    # Build the artist-image stack once: the disk cache + the persisted enabled
    # toggle are shared on app.state so the override + settings endpoints reach
    # the SAME instances the service uses. The cache dir is created lazily on
    # first write, so no startup mkdir.
    cache = ArtistImageCache(_resolve_cache_dir())
    toggle = ArtistImageToggle(
        _resolve_cache_dir() / "_enabled.json", default=settings.artist_images_enabled
    )
    app.state.artist_image_cache = cache
    app.state.artist_image_toggle = toggle
    art_write_toggle = ArtistArtWriteToggle(
        _resolve_cache_dir() / "_art_write_enabled.json",
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
    # The write toggle ALSO enables fetching (one switch): the engine resolves
    # portraits whenever EITHER the image toggle OR the write toggle is on.
    app.state.artist_image_service = _build_artist_image_service(
        http_client, cache, lambda: toggle.is_enabled() or art_write_toggle.is_enabled()
    )
    from app.artwork.factory import build_fanart_background_source

    app.state.artist_background_source = build_fanart_background_source(http_client, settings)

    try:
        yield
    finally:
        await http_client.aclose()
        close_library(handle.lib)


app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # Vite dev server (frontend added later)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router, prefix="/api")
app.include_router(albums_router, prefix="/api")
app.include_router(artists_router, prefix="/api")
app.include_router(search_router, prefix="/api")
app.include_router(import_router, prefix="/api")
app.include_router(config_router, prefix="/api")
app.include_router(duplicates_router, prefix="/api")
app.include_router(lyrics_router, prefix="/api")
app.include_router(reorganize_router, prefix="/api")
app.include_router(stats_router, prefix="/api")
