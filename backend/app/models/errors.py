"""The error body FastAPI actually returns, as a model the schema can name.

A raised ``HTTPException`` renders ``{"detail": "<sentence>"}``. A route that
hand-declares an error status in ``responses={...}`` with only a ``description``
does NOT get that documented for free - FastAPI replaces the generated entry
rather than merging into it, so the status ends up with no ``content`` at all
and ``openapi-typescript`` renders it as ``content?: never``: a generated type
asserting the body cannot exist, for a status whose body the client must read.

Declaring ``{"model": ErrorDetail}`` alongside the description is what closes
that, for every status except 422.

422 is the status with more than one real body shape, so it needs its own
treatment. FastAPI generates a 422 documented as ``HTTPValidationError`` (whose
``detail`` is a LIST of ``{loc: (string|integer)[], msg, type}`` objects) for
every route with a body or parameters to validate. A route that ALSO raises
``HTTPException(422, ...)`` returns ITS OWN payload under that same status. Both
bodies are real, so:

- declaring ``{"model": <anything>}`` REPLACES FastAPI's entry and drops the
  validation shape;
- leaving 422 undeclared documents only the validation shape, so the generated
  client parses the route's own body as a list of FastAPI loc/msg objects;
- ``validation_or_detail_422`` and ``validation_or_model_422`` document BOTH, as
  an ``anyOf`` over the two shapes, and are what such a route should use.

Which of those two to reach for depends on what the route's own raise sends:

- ``HTTPException(422, "<sentence>")`` - a well-formed request that is
  semantically impossible, e.g. an unknown item id - renders an ``ErrorDetail``,
  so ``validation_or_detail_422``;
- anything else needs a model of its own and ``validation_or_model_422``. The
  config editor's two save routes are the case in the app today: they raise with
  a LIST of per-error objects. Declaring those with ``validation_or_detail_422``
  would type a list as a sentence, which is the wrongly-typed failure this
  module argues is worse than an undeclared status.

A route that never raises 422 itself leaves it undeclared - FastAPI's own entry
is already the whole truth there.

The ``anyOf`` has one sharp edge worth knowing: FastAPI emits the
``HTTPValidationError`` COMPONENT only while at least one route still lets it
auto-generate a 422, so the second ``$ref`` would dangle if every route in the
app ever hand-declared its own. ``tests/test_openapi_refs.py`` fails loudly if
that day comes.
"""

from typing import Final

from pydantic import BaseModel

from app.models.config_editor import ValidationErrorItem

#: The two component paths the 422 entries below reference. Spelled here rather
#: than derived, because they are what FastAPI names these components in the
#: generated schema - see app/openapi_overlay.py for the same constant.
_ERROR_DETAIL_REF: Final = "#/components/schemas/ErrorDetail"
_VALIDATION_ERROR_REF: Final = "#/components/schemas/HTTPValidationError"


class ErrorDetail(BaseModel):
    """The body of any non-validation error response: one ASCII sentence."""

    detail: str


class OperationFailure(BaseModel):
    """What a long-running library op failed at, and what the user can do next.

    ``message`` names the cause (it embeds the underlying exception's text);
    ``recovery`` says what state the library is in and what to do about it.
    ``message`` rather than ``detail`` for the inner key on purpose - Starlette
    already wraps the payload in an outer ``detail``, so an inner ``detail``
    would render the confusing ``{"detail": {"detail": ...}}``.
    """

    message: str
    recovery: str


class StructuredErrorDetail(BaseModel):
    """The SECOND real error body in this API: ``{"detail": {message, recovery}}``.

    Every blanket ``except Exception`` on a beets write op raises a 500 whose
    ``detail`` is an OBJECT, not a sentence (cover install, album edit, artist
    rename, duplicate resolve, delete, config Apply). The frontend unwraps it in
    ``frontend/src/api/lib.ts::structuredDetailMessage``, so the shape is read,
    not decorative.

    Declaring such a 500 as ``ErrorDetail`` would be worse than leaving it out:
    it swaps an UNDECLARED status for a WRONGLY-TYPED one, and the generated
    client would parse an object as a string. Use this model instead, and only
    on routes whose 500 really carries the nested shape - a route that can also
    500 with a flat sentence needs an ``anyOf`` (none does today).
    """

    detail: OperationFailure


class ConfigSaveConflict(BaseModel):
    """What the config editor sends back when its compare-and-swap loses.

    ``POST /api/config/save`` and ``POST /api/config/naming/save`` hash the file
    on disk and compare it with the ``base_sha256`` the editor loaded from
    (``app/beets/config_editor.py``). A mismatch means someone else wrote
    ``config.yaml`` in between, so the save is refused with 409 and the client is
    handed everything it needs to recover WITHOUT a second round trip:
    ``current_yaml_text`` is the file as it now stands and ``current_sha256`` is
    the CAS token to resubmit with if the user chooses to overwrite anyway.

    ``detail`` is the human sentence ("File changed on disk"). Unlike
    :class:`OperationFailure`, the inner key really IS named ``detail`` here -
    that is what the raise sends, and this model documents the wire, not the
    naming we would pick today.
    """

    detail: str
    current_yaml_text: str
    current_sha256: str


class ConfigSaveConflictDetail(BaseModel):
    """The THIRD real error body: ``{"detail": {detail, current_yaml_text, current_sha256}}``.

    Starlette wraps an ``HTTPException``'s ``detail`` verbatim, so the object the
    config editor raises with becomes the value of the outer ``detail`` key.

    Neither existing model describes it, and declaring it as either would be a
    lie the generated client cannot recover from:

    - :class:`ErrorDetail` types ``detail`` as a ``str``, so the two fields the
      conflict UI reads would not exist in the contract at all;
    - :class:`StructuredErrorDetail` types ``detail`` as ``{message, recovery}``
      - three keys wrong out of three, and it would promise a ``recovery``
      sentence this body never carries.

    A wrongly-typed status is worse than an undeclared one (see
    :class:`StructuredErrorDetail`), which is why this shape got a model of its
    own rather than being folded into one of those two.
    """

    detail: ConfigSaveConflict


class ConfigValidationErrorDetail(BaseModel):
    """The FOURTH real error body: ``POST /api/config/save``'s 422.

    Both of that route's own 422s - the ruamel parse failure and the known-keys
    schema failure (``app/beets/config_editor.py::save``) - raise with a LIST of
    :class:`~app.models.config_editor.ValidationErrorItem` payloads, which
    Starlette renders verbatim under the outer ``detail`` key. The items are the
    SAME rows ``POST /api/config/validate`` returns on a 200, which is what lets
    the editor feed a rejected Save straight into its CodeMirror gutter.

    FastAPI's ``HTTPValidationError`` is not this shape and cannot stand in for
    it: its items carry ``loc`` as an ARRAY of path segments and have no ``line``
    or ``column`` at all. The editor's CodeMirror gutter consumes the same row
    shape from ``POST /api/config/validate``'s 200 body, where it is correctly
    typed; no live client reads this save 422 body today (the save mutation's
    error handler covers 409 only and the 422 body is discarded). Declaring this
    422 as that model would still mistype the generated TypeScript - a
    wrongly-typed status is worse than an undeclared one.
    """

    detail: list[ValidationErrorItem]


class NamingRuleError(BaseModel):
    """One rejected ``replace:`` row of ``POST /api/config/naming/save``.

    The same three keys as a :class:`~app.models.config_editor.ValidationErrorItem`
    and DELIBERATELY not that model: the naming save builds these dicts by hand
    (``app/beets/config_editor.py::save_naming``) with no ``line``/``column``,
    because a bad regex comes from a form row rather than from a position in the
    YAML document - ``loc`` is the row index (``"replace[0]"``), which is what
    the panel would highlight. Reusing the five-field model here would promise a
    line number this route can never send.
    """

    loc: str
    """Which submitted row was rejected, as ``replace[<index>]``."""

    msg: str
    type: str


class NamingValidationErrorDetail(BaseModel):
    """The FIFTH real error body: ``POST /api/config/naming/save``'s 422."""

    detail: list[NamingRuleError]


def _inline_refs(node: object, defs: dict[str, object]) -> object:
    """``node`` with every ``#/$defs/...`` reference replaced by what it names."""
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        return _inline_refs(defs[ref.rsplit("/", 1)[-1]], defs)
    return {key: _inline_refs(value, defs) for key, value in node.items()}


def _inlined_json_schema(model: type[BaseModel]) -> dict[str, object]:
    """``model``'s own JSON schema, with its nested definitions inlined.

    Inlined rather than referenced, because a raw response object registers no
    component: FastAPI writes ``components/schemas`` entries only for models it
    builds a request or response FIELD from, and an ``anyOf`` 422 has to be raw
    (a ``model`` key would replace FastAPI's entry). A ``$ref`` from here into
    ``#/components/schemas`` would therefore dangle for any model no route's
    body or 200 also uses - the failure ``tests/test_openapi_refs.py`` exists
    for. Inlining keeps each arm self-contained and derived from the model, so
    it can neither dangle nor drift.
    """
    schema = model.model_json_schema(ref_template="#/$defs/{model}")
    defs = schema.pop("$defs", {})
    return {key: _inline_refs(value, defs) for key, value in schema.items()}


def _either_shape_422(description: str, own_body: dict[str, object]) -> dict[str, object]:
    """A raw 422 response object over ``own_body`` and FastAPI's validation shape.

    Raw rather than ``{"model": ...}`` on purpose: a model would replace
    FastAPI's generated entry and lose the validation shape (module docstring).
    """
    schema = {"anyOf": [own_body, {"$ref": _VALIDATION_ERROR_REF}]}
    return {
        "description": description,
        "content": {"application/json": {"schema": schema}},
    }


def validation_or_detail_422(description: str) -> dict[str, object]:
    """A 422 entry for a route whose own 422 is a plain ``{"detail": "..."}``.

    For a route that raises its own ``HTTPException(422, "<sentence>")`` on top
    of FastAPI's request validation.
    """
    return _either_shape_422(description, {"$ref": _ERROR_DETAIL_REF})


def validation_or_model_422(model: type[BaseModel], description: str) -> dict[str, object]:
    """A 422 entry for a route whose own 422 body is ``model``, not a sentence.

    Same ``anyOf`` treatment as :func:`validation_or_detail_422`, with ``model``
    standing in for ``ErrorDetail`` because that route answers with something
    else - see :class:`ConfigValidationErrorDetail` for the case this exists for.
    """
    return _either_shape_422(description, _inlined_json_schema(model))
