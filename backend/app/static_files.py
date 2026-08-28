"""Serve the built SPA from FastAPI in production (the single-image story).

Activated only when MUSICDROP_STATIC_DIR points at a Vite dist dir (the
Docker image sets it; dev never does, so dev behavior is untouched).
Hashed /assets get immutable cache headers; everything else that isn't
/api falls back to index.html (no-cache) so client-side deep links work.
"""

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from app.models.errors import ErrorDetail

logger = logging.getLogger(__name__)


class ImmutableStaticFiles(StaticFiles):
    """StaticFiles for hashed filenames: cache forever, revalidate never."""

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def mount_static(app: FastAPI, static_dir: str) -> None:
    """Mount the SPA onto `app`. No-op if `static_dir` has no index.html.

    Must be called AFTER all API routers are included: the catch-all
    matches last-registered, so /api/* keeps its JSON 404s.
    """
    root = Path(static_dir).resolve()
    index = root / "index.html"
    if not index.is_file():
        # main.py already switched to the PRODUCTION posture (dev origins
        # refused) on the strength of a truthy static_dir — say out loud that
        # no SPA will actually be served, and where to look. Fail-closed
        # behavior is unchanged: nothing is mounted.
        logger.warning(
            "static_dir %s has no index.html: production posture is active "
            "(dev origins refused) but no SPA will be served. The realistic "
            "cause is a stale MUSICDROP_STATIC_DIR (e.g. in backend/.env) — "
            "unset it for dev, or point it at a built frontend dist dir.",
            static_dir,
        )
        return

    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", ImmutableStaticFiles(directory=assets), name="assets")

    @app.get(
        "/{path:path}",
        include_in_schema=False,
        responses={
            404: {
                "model": ErrorDetail,
                "description": (
                    "The path is an unmatched API path or a malformed URL, "
                    "so no static file is served."
                ),
            }
        },
    )
    async def spa_fallback(path: str) -> FileResponse:
        # /api is API territory — unmatched API paths must stay JSON 404s.
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404)
        try:
            candidate = (root / path).resolve()
        except (ValueError, OSError):
            # Malformed path (embedded NUL etc.) — a 404, never a 500.
            raise HTTPException(status_code=404) from None
        # Root-level build files (favicon.svg, robots.txt…) are served as
        # themselves; resolve() containment rejects ../ traversal. index.html
        # is deliberately EXCLUDED so its literal URL gets the same no-cache
        # policy as / (a cached shell would point at vanished hashed chunks).
        if path and path != "index.html" and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        # Everything else is a client-side route: hand over index.html and
        # never let browsers cache it (it references hashed assets).
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
