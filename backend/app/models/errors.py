"""The error body FastAPI actually returns, as a model the schema can name.

A raised ``HTTPException`` renders ``{"detail": "<sentence>"}``. A route that
hand-declares an error status in ``responses={...}`` with only a ``description``
does NOT get that documented for free - FastAPI replaces the generated entry
rather than merging into it, so the status ends up with no ``content`` at all
and ``openapi-typescript`` renders it as ``content?: never``: a generated type
asserting the body cannot exist, for a status whose body the client must read.

Declaring ``{"model": ErrorDetail}`` alongside the description is what closes
that. The 422 case needs the opposite treatment - leave it UNDECLARED so
FastAPI's own ``HTTPValidationError`` (``detail`` is a LIST of loc/msg objects,
a different shape) survives.
"""

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    """The body of any non-validation error response: one ASCII sentence."""

    detail: str
