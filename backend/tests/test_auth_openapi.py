"""What the contract says about authentication, versus what the gate does.

The gate rejects before any route runs, so nothing a route declares can
describe it — ``app/openapi_overlay.py`` says it instead. That makes the
contract a SECOND copy of "what is gated", and a second copy is a thing that
drifts: a schema promising anonymous access to a route the gate refuses sends
the generated client to a 401 it was told could not happen.

The defence is that both sides ask the same function. These tests check the
result of that on the real schema, including the direction that would be
invisible in the frontend build: a 401 stamped on a path that is actually open.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.auth.gate import EXEMPT_PATHS, path_requires_session
from app.auth.session import SESSION_COOKIE_NAME
from app.main import app

_ERROR_DETAIL = {"$ref": "#/components/schemas/ErrorDetail"}


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    result: dict[str, Any] = app.openapi()
    return result


def test_the_session_cookie_is_declared_as_a_security_scheme(schema: dict[str, Any]) -> None:
    scheme = schema["components"]["securitySchemes"]["sessionCookie"]
    assert scheme["type"] == "apiKey"
    assert scheme["in"] == "cookie"
    # The name a client would have to send — read from the production constant,
    # so renaming the cookie without regenerating the contract is caught by the
    # spec-drift guard rather than shipping a schema naming the old one.
    assert scheme["name"] == SESSION_COOKIE_NAME


def test_the_document_requires_the_session_by_default(schema: dict[str, Any]) -> None:
    """Required app-wide, so a route added tomorrow is described as
    authenticated without anyone remembering to say so."""
    assert schema["security"] == [{"sessionCookie": []}]


def test_every_gated_operation_declares_the_gates_401(schema: dict[str, Any]) -> None:
    """A census over the whole schema, not a sample.

    Written as a census because the failure this guards against is a NEW route
    that quietly lacks the declaration — a sampled test would keep passing.
    """
    missing = [
        f"{method} {path}"
        for path, item in schema["paths"].items()
        if path_requires_session(path)
        for method, operation in item.items()
        if isinstance(operation, dict) and "401" not in operation.get("responses", {})
    ]
    assert missing == []


def test_the_gates_401_carries_the_error_detail_body(schema: dict[str, Any]) -> None:
    """Not just declared — declared with the body it really returns.

    A ``description``-only entry renders as ``content?: never`` in the generated
    TypeScript: a type asserting the body cannot exist, for a status whose body
    the client must read to show "your session expired".
    """
    response = schema["paths"]["/api/config"]["get"]["responses"]["401"]
    assert response["content"]["application/json"]["schema"] == _ERROR_DETAIL
    assert "session gate" in response["description"]


def test_the_exempt_operations_are_not_described_as_authenticated(
    schema: dict[str, Any],
) -> None:
    """``security: []`` is how OpenAPI cancels the document-wide requirement.

    Without it, ``/docs`` would show a padlock on ``/api/health`` and a
    generated client would be told the healthcheck needs a cookie it cannot
    have.
    """
    for path in EXEMPT_PATHS:
        item = schema["paths"][path]
        for method, operation in item.items():
            assert operation.get("security") == [], f"{method} {path}"


def test_no_gate_401_is_stamped_on_an_exempt_path(schema: dict[str, Any]) -> None:
    """The drift direction the frontend build cannot see.

    ``/api/health`` has no 401 at all, and the webhook's 401 is its OWN — its
    module docstring pins "401 is the ONLY non-2xx it ever returns", which stays
    true only while the gate leaves that path alone. Asserted on the
    DESCRIPTION, because both would be an ``ErrorDetail`` 401 and the statuses
    alone cannot tell them apart.
    """
    assert "401" not in schema["paths"]["/api/health"]["get"]["responses"]
    assert "401" not in schema["paths"]["/api/auth/status"]["get"]["responses"]

    webhook = schema["paths"]["/api/slskd/webhook"]["post"]["responses"]["401"]
    assert "webhook secret" in webhook["description"]
    assert "session gate" not in webhook["description"]


def test_logins_own_401_survives_the_overlay(schema: dict[str, Any]) -> None:
    """Login is exempt, so its 401 must be the route's own sentence about the
    password — not a claim that a session cookie was missing."""
    login = schema["paths"]["/api/auth/login"]["post"]["responses"]["401"]
    assert "MUSICDROP_PASSWORD_HASH" in login["description"]
    assert login["content"]["application/json"]["schema"] == _ERROR_DETAIL


def test_the_overlay_never_replaces_a_routes_own_401(schema: dict[str, Any]) -> None:
    """Only-where-absent, the same rule the 400/403/413 stamps follow.

    Belt and braces with the two tests above: those name specific routes, this
    one states the rule that makes them true.
    """
    from app.openapi_overlay import overlay_middleware_responses

    own = {"description": "mine", "content": {"application/json": {"schema": _ERROR_DETAIL}}}
    synthetic: dict[str, object] = {
        "paths": {"/api/thing": {"get": {"responses": {"200": {"description": "ok"}, "401": own}}}}
    }
    result = overlay_middleware_responses(synthetic)
    paths: Any = result["paths"]
    assert paths["/api/thing"]["get"]["responses"]["401"] is own


def test_the_overlay_gates_a_path_it_has_never_seen(schema: dict[str, Any]) -> None:
    """A route added tomorrow inherits the 401 without anyone declaring it.

    This is the property that makes the overlay worth having rather than a
    per-route ``responses={401: ...}`` convention nobody would remember.
    """
    from app.openapi_overlay import overlay_middleware_responses

    synthetic: dict[str, object] = {
        "paths": {"/api/brand-new": {"get": {"responses": {"200": {"description": "ok"}}}}}
    }
    result = overlay_middleware_responses(synthetic)
    paths: Any = result["paths"]
    operation = paths["/api/brand-new"]["get"]
    assert operation["responses"]["401"]["content"]["application/json"]["schema"] == _ERROR_DETAIL
    assert "security" not in operation  # inherits the document-wide requirement
