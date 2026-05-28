"""Read-only Config view endpoint.

Reads ``request.app.state.beets_library`` directly (no FastAPI ``Depends`` /
dependency override) because the snapshot builder needs the real
:class:`LibraryHandle` snapshot fields (``loaded_at`` / ``file_mtime_at_load`` /
``config_path``) — not the lib-only wrapper that the album endpoints accept
via a ``get_library`` override.
"""

from fastapi import APIRouter, Request
from ruamel.yaml.error import YAMLError

from app.beets.config_editor import apply as apply_config_op
from app.beets.config_editor import parse_yaml, validate_known_keys
from app.beets.config_editor import save as save_config_op
from app.beets.config_snapshot import build_config_snapshot
from app.beets.library import LibraryHandle
from app.models.config_api import BeetsConfigSnapshot
from app.models.config_editor import (
    SaveRequest,
    ValidateRequest,
    ValidateResponse,
    ValidationErrorItem,
)

router = APIRouter(tags=["config"])


@router.get("/config", response_model=BeetsConfigSnapshot)
def get_config(request: Request) -> BeetsConfigSnapshot:
    handle: LibraryHandle = request.app.state.beets_library
    return build_config_snapshot(handle)


@router.post("/config/validate", response_model=ValidateResponse)
def validate_config(req: ValidateRequest) -> ValidateResponse:
    """Cheap lint pass — never writes. Returns 200 even on errors so the
    CodeMirror async lint source can display them inline."""
    try:
        data = parse_yaml(req.yaml_text)
    except YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        return ValidateResponse(
            errors=[
                ValidationErrorItem(
                    loc="",
                    msg=str(e),
                    type="yaml_parse",
                    line=(mark.line + 1) if mark else None,
                    column=mark.column if mark else None,
                )
            ]
        )
    return ValidateResponse(errors=validate_known_keys(data))


@router.post("/config/save", response_model=BeetsConfigSnapshot)
def save_config(req: SaveRequest, request: Request) -> BeetsConfigSnapshot:
    """Persist the user-submitted YAML to disk after CAS + schema checks.

    Returns the freshly-built :class:`BeetsConfigSnapshot` (whose
    ``apply_pending`` will be ``True`` until the upcoming Apply endpoint
    reloads beets' globals). Error mapping lives entirely inside
    :func:`save_config_op`: 422 on parse/schema, 409 on CAS mismatch.
    """
    handle: LibraryHandle = request.app.state.beets_library
    return save_config_op(handle, req)


@router.post("/config/apply", response_model=BeetsConfigSnapshot)
async def apply_config(request: Request) -> BeetsConfigSnapshot:
    """Reload beets in-process after a Save, swapping ``app.state.beets_library``.

    Thin pass-through to :func:`apply_config_op`; all the gating
    (import-in-progress -> 409), locking (``asyncio.Lock`` on
    ``app.state.beets_swap_lock``), threadpool offload, and recovery-hint
    error mapping live in the adapter so the beets boundary stays clean
    (CLAUDE.md rule 3: no beets touched outside ``app/beets/``).
    """
    return await apply_config_op(request)
