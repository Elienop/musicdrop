"""Read-only Config view endpoint.

Reads ``request.app.state.beets_library`` directly (no FastAPI ``Depends`` /
dependency override) because the snapshot builder needs the real
:class:`LibraryHandle` snapshot fields (``loaded_at`` / ``file_mtime_at_load`` /
``config_path``) — not the lib-only wrapper that the album endpoints accept
via a ``get_library`` override.
"""

from fastapi import APIRouter, Request

from app.beets.config_snapshot import build_config_snapshot
from app.beets.library import LibraryHandle
from app.models.config_api import BeetsConfigSnapshot

router = APIRouter(tags=["config"])


@router.get("/config", response_model=BeetsConfigSnapshot)
def get_config(request: Request) -> BeetsConfigSnapshot:
    handle: LibraryHandle = request.app.state.beets_library
    return build_config_snapshot(handle)
