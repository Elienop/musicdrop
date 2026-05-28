"""Read-only Config view endpoint.

Reads ``request.app.state.beets_library`` directly (no FastAPI ``Depends`` /
dependency override) because the snapshot builder needs the real
:class:`LibraryHandle` snapshot fields (``loaded_at`` / ``file_mtime_at_load`` /
``config_path``) — not the lib-only wrapper that the album endpoints accept
via a ``get_library`` override.
"""

from fastapi import APIRouter, Request
from ruamel.yaml.error import YAMLError

from app.beets.config_editor import parse_yaml, validate_known_keys
from app.beets.config_snapshot import build_config_snapshot
from app.beets.library import LibraryHandle
from app.models.config_api import BeetsConfigSnapshot
from app.models.config_editor import ValidateRequest, ValidateResponse, ValidationErrorItem

router = APIRouter(tags=["config"])


@router.get("/config", response_model=BeetsConfigSnapshot)
def get_config(request: Request) -> BeetsConfigSnapshot:
    handle: LibraryHandle = request.app.state.beets_library
    return build_config_snapshot(handle)


@router.post("/config/validate", response_model=ValidateResponse, tags=["config"])
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
