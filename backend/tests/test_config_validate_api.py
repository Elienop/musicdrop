"""Tests for ``POST /api/config/validate``.

Cheap lint pass — never writes, returns 200 even on errors so CodeMirror's
async ``linter()`` source can display them inline.
"""

from fastapi.testclient import TestClient


def test_validate_returns_empty_on_starter(client: TestClient) -> None:
    # Use the starter content (already in conftest's beets_library fixture)
    snap = client.get("/api/config").json()
    r = client.post("/api/config/validate", json={"yaml_text": snap["yaml_text"]})
    assert r.status_code == 200
    assert r.json() == {"errors": []}


def test_validate_returns_errors_on_invalid_bool(client: TestClient) -> None:
    text = "directory: /tmp\nlibrary: /tmp/x\nimport:\n  copy: maybe\n"
    r = client.post("/api/config/validate", json={"yaml_text": text})
    assert r.status_code == 200
    errors = r.json()["errors"]
    assert any(e["loc"] == "import.copy" and e["line"] is not None for e in errors), (
        f"expected import.copy error with line, got: {errors}"
    )


def test_validate_returns_yaml_parse_error(client: TestClient) -> None:
    r = client.post(
        "/api/config/validate",
        json={"yaml_text": "this: is: not [valid YAML"},
    )
    assert r.status_code == 200
    errors = r.json()["errors"]
    assert len(errors) == 1
    assert errors[0]["loc"] == ""
    assert errors[0]["line"] is not None


def test_validate_returns_safe_error_on_empty_text(client: TestClient) -> None:
    """A cleared editor buffer must never trigger a 500.

    ``parse_yaml("")`` returns ``None``; ``validate_known_keys(None)`` then
    drives Pydantic to a ``model_type`` error (loc == ``""``). Pinning this
    so a future refactor of ``validate_known_keys`` can't silently regress
    into raising AttributeError.
    """
    r = client.post("/api/config/validate", json={"yaml_text": ""})
    assert r.status_code == 200
    errors = r.json()["errors"]
    assert len(errors) >= 1
    assert errors[0]["type"] == "model_type"


def test_validate_openapi_uses_named_response_schema(client: TestClient) -> None:
    """The validate endpoint must reference a named ``ValidateResponse`` schema
    in OpenAPI — not an inline ``additionalProperties`` map. The frontend
    codegen (T10) depends on this for a clean ``{errors: ValidationErrorItem[]}``
    TypeScript type."""
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/api/config/validate"]["post"]
    ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/ValidateResponse")
    assert "ValidateResponse" in spec["components"]["schemas"]


def test_validate_returns_all_distinct_schema_errors(client: TestClient) -> None:
    """One YAML body with TWO known-key errors must surface both, not just the first."""
    text = (
        "directory: /tmp\nlibrary: /tmp/x\n"
        "import:\n  copy: maybe\n"
        "match:\n  strong_rec_thresh: 5.0\n"
    )
    r = client.post("/api/config/validate", json={"yaml_text": text})
    assert r.status_code == 200
    locs = {e["loc"] for e in r.json()["errors"]}
    assert "import.copy" in locs
    assert "match.strong_rec_thresh" in locs
