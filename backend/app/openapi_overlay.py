"""Overlay the ASGI guards' 400/401/403/413 responses onto the FINISHED OpenAPI schema.

The four guards — ``app/host_guard.py`` (400, every method),
``app/auth/gate.py`` (401, every gated path), ``app/origin_guard.py`` (403,
writes only), and ``app/body_limit.py`` (413, bodied requests) — reject
requests BEFORE the router runs, so no route's ``responses=`` declaration can
describe them and no new route would be expected to. (The body limit
technically fires on ANY oversized declared Content-Length, even on a bodyless
operation; that degenerate traffic is deliberately not declared — 413 follows
``requestBody``.) This overlay declares them, once, for every operation —
current and future — with the true error-body shape: a ``$ref`` to
``#/components/schemas/ErrorDetail``
(the same ``{detail: str}`` body all four emit).

The 401 also brings the schema's authentication description with it: a cookie
``apiKey`` security scheme, required app-wide, and switched OFF per-operation
(``security: []``) on exactly the paths the gate exempts. Both the stamp and
the exemption ask ``app/auth/gate.py::path_requires_session`` — the SAME
predicate the middleware runs — so the contract cannot claim a 401 on a path
that is reachable anonymously, or promise anonymous access to one that is not.

This post-processing is the one place the contract learns about the guards.
It runs on the generated schema, never through FastAPI's ``responses=`` merge
rules, so an existing declaration for a status is preserved untouched (the few
routes with richer 403 descriptions keep them) and the 422 FastAPI generated
from ``HTTPValidationError`` is never added, removed, or modified — see
``app/models/errors.py`` for both hard constraints.
"""

from __future__ import annotations

from app.auth.gate import path_requires_session
from app.auth.session import SESSION_COOKIE_NAME
from app.models.errors import ErrorDetail
from app.origin_guard import UNSAFE_METHODS

_ERROR_DETAIL_REF = "#/components/schemas/ErrorDetail"

#: The name the security requirement and the scheme definition agree on.
_SESSION_SCHEME = "sessionCookie"

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
_SESSION_GATE_401 = (
    "Rejected by the session gate before the route ran: no valid MusicDrop "
    "session cookie was presented (missing, tampered with, or expired). Sign "
    "in at POST /api/auth/login."
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


def _ensure_security_scheme(components: dict[str, object]) -> None:
    """Describe the session cookie, so the 401 has a scheme to point at.

    ``apiKey``/``in: cookie`` is OpenAPI's only way to say "a cookie carries
    the credential"; ``http``/``bearer`` would describe an Authorization header
    this API never reads. Nothing generates code from it (openapi-typescript
    emits types, not a client), but it is what makes ``/docs`` say out loud
    that the API is authenticated and which five operations are not.
    """
    schemes = components.get("securitySchemes")
    if not isinstance(schemes, dict):
        schemes = {}
        components["securitySchemes"] = schemes
    schemes.setdefault(
        _SESSION_SCHEME,
        {
            "type": "apiKey",
            "in": "cookie",
            "name": SESSION_COOKIE_NAME,
            "description": (
                "The session cookie issued by POST /api/auth/login. HttpOnly, "
                "SameSite=Lax, and Secure when the request arrives over HTTPS "
                "(directly or via X-Forwarded-Proto); absent over plain HTTP "
                "so the LAN by-IP path keeps working."
            ),
        },
    )


def _stamp_operation(method: str, path: str, operation: object) -> None:
    """Stamp the guard responses onto one operation, only where ABSENT.

    - ``400`` on every operation (host guard, all methods);
    - ``401`` on operations the session gate covers — asked of
      ``path_requires_session``, the SAME predicate the middleware runs;
    - ``403`` on operations whose method is in ``UNSAFE_METHODS`` — THE SAME
      object the origin guard checks, so the contract follows if it changes;
    - ``413`` on operations with a ``requestBody``.

    Gate-exempt operations additionally get ``security: []``, which is how
    OpenAPI cancels the document-wide requirement for one operation. It is set
    unconditionally rather than only-where-absent: it is a fact about the
    middleware, not a description a route could know better than the gate does.

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
    if path_requires_session(path):
        if "401" not in responses:
            responses["401"] = _error_detail_response(_SESSION_GATE_401)
    else:
        operation["security"] = []
    if method.upper() in UNSAFE_METHODS and "403" not in responses:
        responses["403"] = _error_detail_response(_ORIGIN_GUARD_403)
    if "requestBody" in operation and "413" not in responses:
        responses["413"] = _error_detail_response(_BODY_LIMIT_413)


def overlay_middleware_responses(schema: dict[str, object]) -> dict[str, object]:
    """Add the guards' 400/401/403/413 to every operation, only where ABSENT.

    - ``400`` on every operation (host guard, all methods);
    - ``401`` on every operation the session gate covers;
    - ``403`` on operations whose method is in ``UNSAFE_METHODS`` — THE SAME
      object the origin guard checks, so the contract follows if it changes;
    - ``413`` on operations with a ``requestBody``.

    Also declares the session cookie as a security scheme and requires it
    document-wide, with ``security: []`` on the gate-exempt operations.

    Existing entries for those statuses (and every 422) are left untouched.
    Mutates ``schema`` in place and returns it.
    """
    components = schema.get("components")
    if not isinstance(components, dict):
        components = {}
        schema["components"] = components
    _ensure_error_detail(components)
    _ensure_security_scheme(components)
    # Document-wide, so a route added tomorrow is described as authenticated
    # without anyone remembering to say so — the same only-the-exceptions-are-
    # listed shape the gate itself has.
    schema.setdefault("security", [{_SESSION_SCHEME: []}])

    _stamp_paths(schema.get("paths"))
    return schema


def _stamp_paths(paths: object) -> None:
    """Walk every operation under ``paths`` and stamp it (see above)."""
    if not isinstance(paths, dict):
        return
    for path, path_item in paths.items():
        if not isinstance(path_item, dict) or not isinstance(path, str):
            continue
        for method, operation in path_item.items():
            if not isinstance(method, str) or method not in _HTTP_METHODS:
                continue
            _stamp_operation(method, path, operation)
