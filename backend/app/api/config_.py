"""Read-only Config view endpoint.

Reads ``request.app.state.beets_library`` directly (no FastAPI ``Depends`` /
dependency override) because the snapshot builder needs the real
:class:`LibraryHandle` snapshot fields (``loaded_at`` / ``file_mtime_at_load`` /
``config_path``) — not the lib-only wrapper that the album endpoints accept
via a ``get_library`` override.
"""

from typing import Final

from fastapi import APIRouter, Request
from ruamel.yaml.error import YAMLError

from app.beets.config_editor import (
    _settings,
    parse_yaml,
    read_naming,
    save_naming,
    store_layout_errors,
    validate_known_keys,
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
    ValidationErrorItem,
    import_advisories,
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
        "The submitted YAML did not parse, a key MusicDrop models has the wrong"
        " shape, or the submitted directory: would put the music library at or"
        " under the Trash directory (or over the Trash origin store); the body"
        " lists one item per problem, with the 1-based line and 0-based column to"
        " mark where there is one. A malformed request body answers with"
        " FastAPI's validation shape instead."
    ),
)

#: The 422 of ``POST /api/config/naming/save``, which is a DIFFERENT shape from
#: the one above: ``config_editor.save_naming`` builds its items by hand with
#: three keys and no ``line``/``column``, because a bad regex comes from a form
#: row (``loc`` is ``replace[<index>]``) rather than from a position in the YAML
#: document. Sharing one model would promise a line number this route can never
#: send - see app/models/errors.py::NamingRuleError.
_NAMING_SAVE_VALIDATION_RESPONSE: Final = validation_or_model_422(
    NamingValidationErrorDetail,
    (
        "A submitted replace: pattern is not a valid regular expression, so the"
        " save was refused before anything was written; the body names the"
        " offending row. A malformed request body answers with FastAPI's"
        " validation shape instead."
    ),
)


@router.get("/config")
def get_config(request: Request) -> BeetsConfigSnapshot:
    handle: LibraryHandle = request.app.state.beets_library
    return build_config_snapshot(handle)


@router.post("/config/validate")
def validate_config(req: ValidateRequest, request: Request) -> ValidateResponse:
    """Cheap lint pass — never writes. Returns 200 even on errors so the
    CodeMirror async lint source can display them inline.

    Takes ``request`` for the settings + live handle the containment check needs:
    whether a ``directory:`` is acceptable is not a property of the document
    alone, it depends on where ``MUSICDROP_TRASH_DIR`` /
    ``MUSICDROP_TRASH_ORIGINS_DIR`` / ``MUSICDROP_BEETS_DIR`` resolve. Same
    helper ``config_editor.save`` calls, so the gutter and the Save refusal
    cannot disagree.
    """
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
            ],
            # Nothing parsed, so there is no config to advise on. The key is
            # still sent: ``advisories`` is a required field, and a client that
            # had to test for its presence would be defending against a shape
            # this route never produces.
            advisories=[],
        )
    # Two independent channels: an advisory is a valid setting MusicDrop
    # overrides, so it is computed from the same document but never merged into
    # ``errors`` (the editor paints that list red). The containment row DOES
    # belong in ``errors``: a document that would delete the library on the next
    # Empty Trash is not a setting we merely override.
    # ``getattr``, not the direct read every other route in this file does: this
    # is the one config route that never needed a library, and two guard tests
    # (test_origin_guard / test_host_guard) exercise it in a lifespan-less child
    # process for exactly that reason. Production always has the handle — the
    # lifespan sets it before the server accepts a request — so the empty branch
    # is unreachable there. It fails OPEN only for the lint hint: the enforcing
    # gate is ``config_editor.save``, which holds a real handle and refuses.
    handle: LibraryHandle | None = getattr(request.app.state, "beets_library", None)
    layout_errors = (
        []
        if handle is None
        else store_layout_errors(data, settings=_settings(request.app), handle=handle)
    )
    return ValidateResponse(
        errors=validate_known_keys(data) + layout_errors,
        advisories=import_advisories(data),
    )


@router.post(
    "/config/save",
    responses={409: _SAVE_CAS_CONFLICT_RESPONSE, 422: _SAVE_VALIDATION_RESPONSE},
)
def save_config(req: SaveRequest, request: Request) -> BeetsConfigSnapshot:
    """Persist the user-submitted YAML to disk after CAS + schema checks.

    Returns the freshly-built :class:`BeetsConfigSnapshot` (whose
    ``apply_pending`` will be ``True`` until the upcoming Apply endpoint
    reloads beets' globals). Error mapping lives entirely inside
    :func:`save_config_op`: 422 on parse/schema/containment, 409 on CAS mismatch.
    The settings are threaded in because the containment row needs them — a
    ``directory:`` is only refusable relative to where Trash resolves.
    """
    handle: LibraryHandle = request.app.state.beets_library
    return save_config_op(handle, req, settings=_settings(request.app))


@router.get("/config/naming")
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
    return save_naming(handle, req)


@router.post(
    "/config/apply",
    # Named models on both: a description-only entry drops the `content` block
    # and openapi-typescript renders `content?: never` for a body the client
    # must read (see app/models/errors.py). Both are raised inside
    # apply_config_op (app/beets/config_editor.py), not here.
    responses={
        # The ONLY 409 reachable from this route is the `library_job_active()`
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
        422: {
            "model": StructuredErrorDetail,
            "description": (
                "The config.yaml on disk declares a directory: that would put the"
                " music library at or under the Trash directory, or over the Trash"
                " origin store, so beets was NOT reloaded and the previously"
                " loaded library is still serving."
            ),
        },
        500: {
            "model": StructuredErrorDetail,
            "description": (
                "The library rebuild failed during apply, but the saved config"
                " is safe on disk and will load on the next start."
            ),
        },
    },
)
async def apply_config(request: Request) -> BeetsConfigSnapshot:
    """Reload beets in-process after a Save, swapping ``app.state.beets_library``.

    Thin pass-through to :func:`apply_config_op`; all the gating
    (import-in-progress -> 409), locking (``asyncio.Lock`` on
    ``app.state.beets_swap_lock``), threadpool offload, and recovery-hint
    error mapping live in the adapter so the beets boundary stays clean
    (CLAUDE.md rule 3: no beets touched outside ``app/beets/``).
    """
    return await apply_config_op(request)
