"""Read-only Config view endpoint.

Reads ``request.app.state.beets_library`` directly (no FastAPI ``Depends`` /
dependency override) because the snapshot builder needs the real
:class:`LibraryHandle` snapshot fields (``loaded_at`` / ``file_mtime_at_load`` /
``config_path``) — not the lib-only wrapper that the album endpoints accept
via a ``get_library`` override.
"""

from typing import Final

from fastapi import APIRouter, Request

from app.beets.config_check import check_config_text
from app.beets.config_editor import (
    _settings,
    read_naming,
    save_naming,
)
from app.beets.config_editor import apply as apply_config_op
from app.beets.config_editor import save as save_config_op
from app.beets.config_snapshot import build_config_snapshot
from app.beets.library import LibraryHandle
from app.beets.naming import assemble_rules, render_samples
from app.models.config_api import BeetsConfigSnapshot
from app.models.config_editor import (
    NamingConfig,
    NamingPreviewRequest,
    NamingPreviewResponse,
    SaveNamingRequest,
    SaveRequest,
    ValidateRequest,
    ValidateResponse,
)
from app.models.errors import (
    ConfigSaveConflictDetail,
    ConfigValidationErrorDetail,
    ErrorDetail,
    NamingValidationErrorDetail,
    StructuredErrorDetail,
    validation_or_model_422,
)

router = APIRouter(tags=["config"])

#: The OpenAPI entry for the compare-and-swap 409 that BOTH save routes raise
#: (``config_editor.save`` and ``config_editor.save_naming`` run the same
#: read-hash-compare step under ``_SAVE_LOCK``). It is the ONLY 409 either route
#: can reach - neither consults the library-busy gate, so neither can refuse for
#: a running job or a held swap lock - which is why the sentence names one cause
#: and stops there.
#:
#: The model is NOT ``ErrorDetail``: this 409 answers with a three-field object
#: under ``detail``, and typing it as a sentence would hide the two fields the
#: editor's conflict UI reads (see app/models/errors.py).
_SAVE_CAS_CONFLICT_RESPONSE: Final = {
    "model": ConfigSaveConflictDetail,
    "description": (
        "config.yaml changed on disk since the editor loaded it, so the save was"
        " refused; the body carries the file's current text and hash."
    ),
}

#: The 422 of ``POST /api/config/save``. NOT FastAPI's ``HTTPValidationError``,
#: which is what an undeclared 422 would document: ``config_editor.save`` raises
#: with a LIST of ``ValidationErrorItem`` rows whose ``loc`` is a plain string
#: and which carry ``line``/``column`` - two fields the validation shape does not
#: have. The editor's gutter consumes the same row shape from
#: ``POST /api/config/validate``'s 200; no live client reads this 422 body today,
#: but declaring it as the validation model would mistype the generated client.
#: The entry is an anyOf because FastAPI's own shape is ALSO reachable here: the
#: route takes a ``SaveRequest`` body, so a malformed request never reaches the
#: adapter and answers with the validation shape instead.
_SAVE_VALIDATION_RESPONSE: Final = validation_or_model_422(
    ConfigValidationErrorDetail,
    (
        # The rows carry a 1-based line and 0-based column where there is one,
        # and a malformed request body answers with FastAPI's own shape instead.
        "beets could not read the YAML or a value in it, an include would be skipped, its"
        " directory:/library: would break the store layout, or config.yaml on disk cannot be"
        " read or written; the body lists one item per problem."
    ),
)

#: The 422 of ``POST /api/config/naming/save``, which is a DIFFERENT shape from
#: the one above: ``config_editor.save_naming`` builds its items by hand with
#: three keys and no ``line``/``column``, because a bad regex comes from a form
#: row (``loc`` is ``replace[<index>]``) rather than from a position in the YAML
#: document. Sharing one model would promise a line number this route can never
#: send - see app/models/errors.py::NamingRuleError. A config.yaml on disk that
#: cannot be read or written, does not parse or is not a mapping is one row with an empty
#: ``loc``.
_NAMING_SAVE_VALIDATION_RESPONSE: Final = validation_or_model_422(
    NamingValidationErrorDetail,
    (
        "A submitted replace: pattern is not a valid regular expression, or"
        " config.yaml on disk cannot be read or written, does not parse, is not a mapping or"
        " would not pass Validate; the body names the problem. A malformed request body answers"
        " with FastAPI's validation shape instead."
    ),
)


@router.get("/config")
def get_config(request: Request) -> BeetsConfigSnapshot:
    handle: LibraryHandle = request.app.state.beets_library
    return build_config_snapshot(handle)


@router.post("/config/validate")
def validate_config(req: ValidateRequest, request: Request) -> ValidateResponse:
    """Read-only lint pass, 200 even on errors so the editor can show them inline."""
    # Everything below stays a COMMENT: FastAPI publishes a route docstring as
    # the operation's OpenAPI description, and these paragraphs are about how
    # this file works rather than about the endpoint's contract.
    #
    # The check is ``check_config_text``, the one ``config_editor.save`` runs, so
    # the gutter and the Save refusal are computed from one function. It reads
    # the text with beets' own loader and typed reads; ``request`` is taken for
    # the settings + live handle the containment rows need, because whether a
    # ``directory:`` is acceptable depends on where MUSICDROP_TRASH_DIR,
    # MUSICDROP_TRASH_ORIGINS_DIR and MUSICDROP_BEETS_DIR resolve.
    #
    # ``getattr``, not the direct read every other route in this file does: this
    # is the one config route that does not otherwise need a library, and two
    # guard tests (test_origin_guard / test_host_guard) exercise it in a
    # lifespan-less child process for exactly that reason. There the containment
    # rows are left out (fails OPEN); under the lifespan the handle is set before
    # the server accepts a request.
    handle: LibraryHandle | None = getattr(request.app.state, "beets_library", None)
    check = check_config_text(req.yaml_text, settings=_settings(request.app), handle=handle)
    return ValidateResponse(errors=check.errors, advisories=check.advisories)


@router.post(
    "/config/save",
    responses={409: _SAVE_CAS_CONFLICT_RESPONSE, 422: _SAVE_VALIDATION_RESPONSE},
)
def save_config(req: SaveRequest, request: Request) -> BeetsConfigSnapshot:
    """Write the submitted YAML to config.yaml as typed, after Validate's check and CAS."""
    # The rest of the contract is here rather than in the docstring, which
    # FastAPI publishes whole: the response is the freshly-built
    # ``BeetsConfigSnapshot``, whose ``apply_pending`` is ``True`` until Apply
    # reloads beets' globals; 422 when the check refuses the text (the rows
    # ``POST /api/config/validate`` returns) or on a read or write failure; 409
    # on a CAS mismatch. The text is written exactly as submitted.
    #
    # A comment, not a docstring paragraph — FastAPI publishes the docstring as
    # this operation's OpenAPI description. The error mapping lives inside
    # ``save_config_op``, and the settings are threaded in because the
    # containment row needs them: a ``directory:`` is refusable only relative to
    # where Trash, the origin store and the beets data dir resolve.
    handle: LibraryHandle = request.app.state.beets_library
    return save_config_op(handle, req, settings=_settings(request.app))


@router.get(
    "/config/naming",
    responses={
        422: {
            "model": ErrorDetail,
            "description": (
                "config.yaml on disk cannot be read, does not parse or is not a mapping;"
                " the detail says why."
            ),
        },
    },
)
def get_naming(request: Request) -> NamingConfig:
    """Current ``paths:``/``replace:`` split into rows, with live previews."""
    handle: LibraryHandle = request.app.state.beets_library
    cfg = read_naming(handle)
    rules = assemble_rules(
        default=cfg.default, comp=cfg.comp, singleton=cfg.singleton, custom=cfg.custom
    )
    rendered, replace_errors = render_samples(handle.lib, rules=rules, replace=cfg.replace)
    return cfg.model_copy(update={"previews": rendered, "replace_errors": replace_errors})


@router.post("/config/naming/preview")
def preview_naming(req: NamingPreviewRequest, request: Request) -> NamingPreviewResponse:
    """Pure render of draft rules + replace against auto-picked samples. Read-only."""
    handle: LibraryHandle = request.app.state.beets_library
    rendered, replace_errors = render_samples(handle.lib, rules=req.rules, replace=req.replace)
    return NamingPreviewResponse(rendered=rendered, replace_errors=replace_errors)


@router.post(
    "/config/naming/save",
    responses={409: _SAVE_CAS_CONFLICT_RESPONSE, 422: _NAMING_SAVE_VALIDATION_RESPONSE},
)
def save_naming_route(req: SaveNamingRequest, request: Request) -> BeetsConfigSnapshot:
    """Write ``paths:``/``replace:`` back into config.yaml (CAS, 409/422). Apply
    is the existing ``POST /api/config/apply``."""
    handle: LibraryHandle = request.app.state.beets_library
    return save_naming(handle, req, settings=_settings(request.app))


@router.post(
    "/config/apply",
    # Named models on both: a description-only entry drops the `content` block
    # and openapi-typescript renders `content?: never` for a body the client
    # must read (see app/models/errors.py). Both are raised inside
    # apply_config_op (app/beets/config_editor.py), not here.
    responses={
        # The ONLY 409 reachable from this route is the `swap_blocked_by_job()`
        # gate in `apply` (config_editor.py). The CAS "file changed on disk"
        # 409s live in `save` / `save_naming`, which this route never calls, so
        # naming them here would document a cause Apply cannot produce.
        409: {
            "model": ErrorDetail,
            "description": (
                "A library job (an import, a lyrics backfill, an artist-art backfill, a"
                " reorganize backfill, or a disk sync) is running, so the reload is refused"
                " until it finishes."
            ),
        },
        # Same structured body as the 500 and for the same reader: the page
        # prints `detail.recovery` after "Apply failed. ". A 409 could not carry
        # it — the frontend renders every Apply 409 as the library-job sentence.
        # No "nothing was changed" here: the post-load backstop in `apply`
        # refuses AFTER the new handle is swapped in.
        422: {
            "model": StructuredErrorDetail,
            "description": (
                "config.yaml on disk is not a regular file, is unreadable, skips an include,"
                " breaks the store layout, or failed to load and the old config was put back;"
                " the recovery line says what to fix."
            ),
        },
        500: {
            "model": StructuredErrorDetail,
            "description": (
                "The rebuild failed and putting the old config back failed too; fix the"
                " error the recovery line quotes and Apply again."
            ),
        },
    },
)
async def apply_config(request: Request) -> BeetsConfigSnapshot:
    """Reload beets in-process after a Save, swapping ``app.state.beets_library``."""
    # Thin pass-through: the gating (import-in-progress -> 409), the
    # ``asyncio.Lock`` on ``app.state.beets_swap_lock``, the threadpool offload
    # and the recovery-hint error mapping all live in the adapter, so the beets
    # boundary stays clean (CLAUDE.md rule 3).
    return await apply_config_op(request)
