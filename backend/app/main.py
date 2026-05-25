import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.albums import router as albums_router
from app.api.health import router as health_router
from app.beets.library import LibraryHandle, close_library, open_library
from app.config import settings


def _resolve_library() -> LibraryHandle | None:
    """Open the configured beets library, or None when unset/missing.

    Sync helper: runs only at startup (cold path), so the one blocking
    filesystem check is fine and stays out of the async lifespan body.
    """
    path = settings.beets_library_path
    if not path or not os.path.exists(path):
        return None
    return open_library(path, settings.beets_library_directory)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Open the beets library once at startup (a SQLite connection we keep for
    # the process lifetime) and close it on shutdown. Stays None when no library
    # is configured or the file is missing, so the API degrades to empty pages.
    lib = _resolve_library()
    app.state.beets_library = lib
    try:
        yield
    finally:
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
