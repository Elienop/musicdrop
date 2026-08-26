"""The error body FastAPI actually returns, as a model the schema can name.

A raised ``HTTPException`` renders ``{"detail": "<sentence>"}``. A route that
hand-declares an error status in ``responses={...}`` with only a ``description``
does NOT get that documented for free - FastAPI replaces the generated entry
rather than merging into it, so the status ends up with no ``content`` at all
and ``openapi-typescript`` renders it as ``content?: never``: a generated type
asserting the body cannot exist, for a status whose body the client must read.

Declaring ``{"model": ErrorDetail}`` alongside the description is what closes
that, for every status except 422.

422 is the one status with TWO real body shapes, so it needs its own treatment.
FastAPI generates a 422 documented as ``HTTPValidationError`` (whose ``detail``
is a LIST of loc/msg objects) for every route with a body or parameters to
validate. A route that ALSO raises ``HTTPException(422, "<sentence>")`` - for a
well-formed request that is semantically invalid, e.g. an unknown item id -
returns an ``ErrorDetail`` under that same status. Both bodies are real, so:

- declaring ``{"model": ErrorDetail}`` REPLACES FastAPI's entry and drops the
  validation shape;
- leaving 422 undeclared documents only the validation shape, so the generated
  client parses a sentence body as a list of loc/msg objects;
- ``validation_or_detail_422`` documents BOTH, as an ``anyOf`` over the two
  components, and is what such a route should use.

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

#: The two component paths the 422 entry below references. Spelled here rather
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
    """The OTHER real error body in this API: ``{"detail": {message, recovery}}``.

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


def validation_or_detail_422(description: str) -> dict[str, object]:
    """A 422 entry documenting BOTH error bodies the route can return.

    For a route that raises its own ``HTTPException(422, "<sentence>")`` on top
    of FastAPI's request validation. Returns a raw response object rather than
    ``{"model": ...}``: a model would replace FastAPI's generated entry and lose
    the validation shape (see the module docstring).
    """
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {
                    "anyOf": [
                        {"$ref": _ERROR_DETAIL_REF},
                        {"$ref": _VALIDATION_ERROR_REF},
                    ]
                }
            }
        },
    }
