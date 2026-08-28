"""Tests for ``POST /api/config/validate``.

Cheap lint pass — never writes, returns 200 even on errors so CodeMirror's
async ``linter()`` source can display them inline.
"""

from fastapi.testclient import TestClient


def test_validate_returns_empty_on_starter(client: TestClient) -> None:
    # Use the starter content (already in conftest's beets_library fixture).
    # The exact-equality assertion is deliberate: it pins the FULL response
    # shape, so a new channel cannot be added without a reader noticing. The
    # starter sets `autotag: yes` and none of the other three advisory keys,
    # so it must come back with both channels empty.
    snap = client.get("/api/config").json()
    r = client.post("/api/config/validate", json={"yaml_text": snap["yaml_text"]})
    assert r.status_code == 200
    assert r.json() == {"errors": [], "advisories": []}


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


def test_validate_surfaces_advisories_through_the_api(client: TestClient) -> None:
    """The advisory channel must survive the route, not just the rule function.

    Rule-level coverage lives in ``test_config_advisories.py``; this pins that
    the field is actually serialised onto the 200 body the editor reads.
    """
    text = "directory: /tmp\nlibrary: /tmp/x\nimport:\n  autotag: no\n  duplicate_action: skip\n"
    r = client.post("/api/config/validate", json={"yaml_text": text})
    assert r.status_code == 200
    advisories = r.json()["advisories"]
    assert {a["key"] for a in advisories} == {"import.autotag", "import.duplicate_action"}
    assert all(a["message"] for a in advisories)


def test_validate_advisory_only_config_is_still_valid(client: TestClient) -> None:
    """Advisories are NOT errors.

    CodeMirror paints the ``errors`` list red, and every config these rules fire
    on is valid — so a config whose only problem is an inert key must come back
    with an empty ``errors`` list.
    """
    text = "directory: /tmp\nlibrary: /tmp/x\nimport:\n  singletons: yes\n  incremental: yes\n"
    r = client.post("/api/config/validate", json={"yaml_text": text})
    assert r.status_code == 200
    body = r.json()
    assert body["errors"] == []
    assert {a["key"] for a in body["advisories"]} == {
        "import.singletons",
        "import.incremental",
    }


def test_validate_yaml_parse_error_carries_an_empty_advisory_list(client: TestClient) -> None:
    """Nothing parsed, so there is no config to advise on — but the field must
    still be present, or the generated client's non-optional type is a lie."""
    r = client.post("/api/config/validate", json={"yaml_text": "this: is: not [valid YAML"})
    assert r.status_code == 200
    assert r.json()["advisories"] == []


def test_validate_openapi_declares_advisories_as_required(client: TestClient) -> None:
    """``advisories`` must be REQUIRED on ``ValidateResponse``, the same as
    ``errors``: an optional field generates ``advisories?:`` in TypeScript and
    every reader then has to defend against an absence the route cannot produce.
    The item schema must be a named ``$ref`` for the same reason the response is
    (see ``test_validate_openapi_uses_named_response_schema``)."""
    spec = client.get("/openapi.json").json()
    schema = spec["components"]["schemas"]["ValidateResponse"]
    assert "advisories" in schema["required"]
    assert schema["properties"]["advisories"]["items"]["$ref"].endswith("/ConfigAdvisory")
    advisory = spec["components"]["schemas"]["ConfigAdvisory"]
    assert set(advisory["required"]) == {"key", "message"}


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
