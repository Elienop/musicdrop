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
    assert any(e["loc"] == "import.copy" for e in errors)


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
