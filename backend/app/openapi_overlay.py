"""Overlay the ASGI guards' 400/403/413 responses onto the FINISHED OpenAPI schema.

The three guards — ``app/host_guard.py`` (400, every method),
``app/origin_guard.py`` (403, writes only), and ``app/body_limit.py`` (413,
bodied requests) — reject requests BEFORE the router runs, so no route's
``responses=`` declaration can describe them and no new route would be expected
to. (The body limit technically fires on ANY oversized declared Content-Length,
even on a bodyless operation; that degenerate traffic is deliberately not
declared — 413 follows ``requestBody``.) This overlay declares them, once, for
every operation — current and future — with the true error-body shape: a
``$ref`` to ``#/components/schemas/ErrorDetail``
(the same ``{detail: str}`` body all three emit).

This post-processing is the one place the contract learns about the guards.
It runs on the generated schema, never through FastAPI's ``responses=`` merge
rules, so an existing declaration for a status is preserved untouched (the few
routes with richer 403 descriptions keep them) and the 422 FastAPI generated
from ``HTTPValidationError`` is never added, removed, or modified — see
``app/models/errors.py`` for both hard constraints.
"""

from __future__ import annotations

from app.models.errors import ErrorDetail
from app.origin_guard import UNSAFE_METHODS

_ERROR_DETAIL_REF = "#/components/schemas/ErrorDetail"

# A path item's method keys (OpenAPI lowercases them; the guard's
# UNSAFE_METHODS are upper, matching the method token the middleware sees).
_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})

_HOST_GUARD_400 = (
    "Rejected by the host guard before the route ran: the Host header (or "
    "X-Forwarded-Host, when present) is not an allowed name (DNS-rebinding "
    "allowlist; bare IP literals, localhost, and MUSICDROP_ALLOWED_HOSTS pass)."
)
_ORIGIN_GUARD_403 = (
    "Rejected by the cross-origin write guard before the route ran: the Origin "
    "header is not allowed to write (browser-CSRF protection; requests without "
    "an Origin pass)."
)
_BODY_LIMIT_413 = (
    "Rejected by the body-size guard before the route ran: the declared "
    "Content-Length exceeds the limit."
)


def _error_detail_response(description: str) -> dict[str, object]:
    """The JSON OpenAPI response object every guard status is declared with."""
    return {
        "description": description,
        "content": {"application/json": {"schema": {"$ref": _ERROR_DETAIL_REF}}},
    }


def _ensure_error_detail(components: dict[str, object]) -> None:
    """Make sure the $ref target exists, so no reference can dangle.

    The schema usually already carries ErrorDetail (routes declare it inline),
    but the overlay must not depend on that: if it is absent, insert it from
    the model itself. ``model_json_schema()`` equals FastAPI's generated
    component only while ErrorDetail stays a FLAT model (nested models would
    inline ``#/$defs/`` where FastAPI splits components) — re-verify if it
    ever grows a nested field.
    """
    schemas = components.get("schemas")
    if not isinstance(schemas, dict):
        schemas = {}
        components["schemas"] = schemas
    if "ErrorDetail" not in schemas:
        schemas["ErrorDetail"] = ErrorDetail.model_json_schema()


def _stamp_operation(method: str, operation: object) -> None:
    """Stamp the guard responses onto one operation, only where ABSENT.

    - ``400`` on every operation (host guard, all methods);
    - ``403`` on operations whose method is in ``UNSAFE_METHODS`` — THE SAME
      object the origin guard checks, so the contract follows if it changes;
    - ``413`` on operations with a ``requestBody``.

    Existing entries for those statuses are left untouched.
    """
    if not isinstance(operation, dict):
        return
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        # Deliberate skip, not a fix-up: FastAPI always emits a
        # ``responses`` dict, so this only fires on malformed or
        # synthetic input, which the overlay declines to invent for.
        return
    if "400" not in responses:
        responses["400"] = _error_detail_response(_HOST_GUARD_400)
    if method.upper() in UNSAFE_METHODS and "403" not in responses:
        responses["403"] = _error_detail_response(_ORIGIN_GUARD_403)
    if "requestBody" in operation and "413" not in responses:
        responses["413"] = _error_detail_response(_BODY_LIMIT_413)


def overlay_middleware_responses(schema: dict[str, object]) -> dict[str, object]:
    """Add the guards' 400/403/413 to every operation, only where ABSENT.

    - ``400`` on every operation (host guard, all methods);
    - ``403`` on operations whose method is in ``UNSAFE_METHODS`` — THE SAME
      object the origin guard checks, so the contract follows if it changes;
    - ``413`` on operations with a ``requestBody``.

    Existing entries for those statuses (and every 422) are left untouched.
    Mutates ``schema`` in place and returns it.
    """
    components = schema.get("components")
    if not isinstance(components, dict):
        components = {}
        schema["components"] = components
    _ensure_error_detail(components)

    paths = schema.get("paths")
    if isinstance(paths, dict):
        for path_item in paths.values():
            if not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if not isinstance(method, str) or method not in _HTTP_METHODS:
                    continue
                _stamp_operation(method, operation)
    return schema
