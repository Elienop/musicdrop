import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.albums import router as albums_router
from app.api.artists import router as artists_router
from app.api.health import router as health_router
from app.api.import_ import router as import_router
from app.api.search import router as search_router
from app.artwork.cache import ArtistImageCache
from app.artwork.deezer import DeezerArtistImageSource
from app.artwork.rate_limit import TokenBucketLimiter
from app.artwork.service import ArtistImageService
from app.beets.library import LibraryHandle, close_library, open_library
from app.config import settings

# Repo root is the parent of the backend/ package dir (this file is
# backend/app/main.py). Relative cache paths resolve under it so the
# artist-image cache lands in the gitignored repo-root data/, not backend/data/.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve_library() -> LibraryHandle | None:
    """Open the configured beets library, or None when unset/missing.

    Sync helper: runs only at startup (cold path), so the one blocking
    filesystem check is fine and stays out of the async lifespan body.
    """
    path = settings.beets_library_path
    if not path or not os.path.exists(path):
        return None
    return open_library(path, settings.beets_library_directory)


def _resolve_cache_dir() -> Path:
    """Resolve the artist-image cache dir, anchoring relatives to the repo root."""
    configured = Path(settings.artist_image_cache_dir)
    if configured.is_absolute():
        return configured
    return _REPO_ROOT / configured


def _build_artist_image_service(client: httpx.AsyncClient) -> ArtistImageService:
    source = DeezerArtistImageSource(client=client, search_limit=settings.artist_image_search_limit)
    cache = ArtistImageCache(_resolve_cache_dir())
    limiter = TokenBucketLimiter(
        rate_per_sec=settings.artist_image_rate_per_sec,
        max_concurrency=settings.artist_image_max_concurrency,
    )
    return ArtistImageService(
        source=source,
        cache=cache,
        limiter=limiter,
        enabled=settings.artist_images_enabled,
        negative_ttl_seconds=settings.artist_image_negative_ttl_seconds,
        transient_ttl_seconds=settings.artist_image_transient_ttl_seconds,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Open the beets library once at startup (a SQLite connection we keep for
    # the process lifetime) and close it on shutdown. Stays None when no library
    # is configured or the file is missing, so the API degrades to empty pages.
    lib = _resolve_library()
    app.state.beets_library = lib

    from app.import_jobs.registry import registry as import_registry

    import_registry.attach_library(lib)

    # Build the artist-image stack once: a shared httpx client (timeout +
    # descriptive User-Agent) behind the rate-limited, disk-cached service.
    # The cache dir is created lazily on first write, so no startup mkdir.
    http_client = httpx.AsyncClient(
        timeout=10.0,
        headers={
            "User-Agent": (
                f"{settings.app_name}/{settings.version} (+https://github.com/Elienop/musicdrop)"
            )
        },
    )
    app.state.artist_image_service = _build_artist_image_service(http_client)

    try:
        yield
    finally:
        await http_client.aclose()
        if lib is not None:
            close_library(lib)


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
