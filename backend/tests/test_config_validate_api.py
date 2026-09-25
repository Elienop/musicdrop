"""Tests for ``POST /api/config/validate``.

Cheap lint pass — never writes, returns 200 even on errors so CodeMirror's
async ``linter()`` source can display them inline.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _head(tmp_path: Path) -> str:
    """``directory:`` and ``library:`` the schema accepts: the fixture's own music
    dir, and a file under ``tmp_path``. Measured: ``library: /tmp/x`` failed 11
    tests here while a directory was at ``/tmp/x``."""
    return f"directory: {tmp_path / 'music'}\nlibrary: {tmp_path / 'x'}\n"


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


def test_validate_returns_errors_on_invalid_bool(client: TestClient, tmp_path: Path) -> None:
    text = _head(tmp_path) + "import:\n  copy: maybe\n"
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


def test_validate_refuses_a_reused_anchor_as_beets_does(client: TestClient) -> None:
    """Measured before: clean, where beets' loader refuses the file. The text is
    beets' loader's own (PyYAML), sent back to the operator who typed it."""
    text = "directory: /tmp/music\nlibrary: /tmp/x\nplex:\n  token: &a Zq7Secret\n  user: &a u\n"

    r = client.post("/api/config/validate", json={"yaml_text": text})

    assert r.status_code == 200
    assert r.json() == {
        "errors": [
            {
                "loc": "",
                "msg": "found duplicate anchor 'a'; first occurrence\n"
                '  in "<unicode string>", line 4, column 10:\n'
                "      token: &a Zq7Secret\n"
                "             ^\n"
                "second occurrence\n"
                '  in "<unicode string>", line 5, column 9:\n'
                "      user: &a u\n"
                "            ^",
                "type": "yaml_parse",
                "line": 5,
                "column": 8,
            }
        ],
        "advisories": [],
    }


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


def test_validate_surfaces_advisories_through_the_api(client: TestClient, tmp_path: Path) -> None:
    """The advisory channel must survive the route, not just the rule function.

    Rule-level coverage lives in ``test_config_advisories.py``; this pins that
    the field is actually serialised onto the 200 body the editor reads.
    """
    text = _head(tmp_path) + "import:\n  autotag: no\n  duplicate_action: skip\n"
    r = client.post("/api/config/validate", json={"yaml_text": text})
    assert r.status_code == 200
    advisories = r.json()["advisories"]
    assert {a["key"] for a in advisories} == {"import.autotag", "import.duplicate_action"}
    assert all(a["message"] for a in advisories)


def test_validate_advisory_only_config_is_still_valid(client: TestClient, tmp_path: Path) -> None:
    """Advisories are NOT errors.

    CodeMirror paints the ``errors`` list red, and every config these rules fire
    on is valid — so a config whose only problem is an inert key must come back
    with an empty ``errors`` list.
    """
    text = _head(tmp_path) + "import:\n  singletons: yes\n  incremental: yes\n"
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


def test_validate_returns_all_distinct_schema_errors(client: TestClient, tmp_path: Path) -> None:
    """One YAML body with TWO known-key errors must surface both, not just the first."""
    text = _head(tmp_path) + "import:\n  copy: maybe\nmatch:\n  strong_rec_thresh: 5.0\n"
    r = client.post("/api/config/validate", json={"yaml_text": text})
    assert r.status_code == 200
    locs = {e["loc"] for e in r.json()["errors"]}
    assert "import.copy" in locs
    assert "match.strong_rec_thresh" in locs


#: confuse's own text for ``.get(bool)`` on a string (``confuse/templates.py``).
_NOT_A_BOOL = "must be a bool, not str"


def test_validate_refuses_a_quoted_and_an_unquoted_string_for_a_filing_flag(
    client: TestClient, tmp_path: Path
) -> None:
    """The old text said "write y without the quotes" for an unquoted ``y``.

    beets reads both as strings and refuses them (``must be a bool, not str``);
    the row is beets' sentence, split so the editor prints the key once.
    """
    text = _head(tmp_path) + "import:\n  copy: 'no'\n  move: y\n"

    r = client.post("/api/config/validate", json={"yaml_text": text})

    assert r.status_code == 200
    assert r.json() == {
        "errors": [
            {
                "loc": f"import.{key}",
                "msg": _NOT_A_BOOL,
                "type": "beets_read",
                "line": line,
                "column": 8,
            }
            for key, line in (("copy", 4), ("move", 5))
        ],
        "advisories": [],
    }


@pytest.mark.parametrize("value", ["n", "'no'", "1"])
@pytest.mark.parametrize("key", ["write", "delete", "remux_mp3_in_wav"])
def test_validate_refuses_a_non_bool_for_every_other_switch_beets_reads_typed(
    client: TestClient, tmp_path: Path, key: str, value: str
) -> None:
    """beets reads these with ``.get(bool)`` (``importer/stages.py:385``,
    ``importer/tasks.py:510,1475``). Pydantic read ``n`` and ``'no'`` as False
    here and ``1`` as True, so ``write: 1`` used to save and fail every import."""
    text = _head(tmp_path) + f"import:\n  {key}: {value}\n"

    r = client.post("/api/config/validate", json={"yaml_text": text})

    assert r.status_code == 200
    kind = "int" if value == "1" else "str"
    assert r.json() == {
        "errors": [
            {
                "loc": f"import.{key}",
                "msg": f"must be a bool, not {kind}",
                "type": "beets_read",
                "line": 4,
                "column": len(f"  {key}: "),
            }
        ],
        "advisories": [],
    }


@pytest.mark.parametrize("value", ["n", "'no'"])
@pytest.mark.parametrize("key", ["autotag", "singletons", "incremental", "link", "hardlink"])
def test_validate_refuses_a_string_for_a_switch_beets_tests_with_a_bare_if(
    client: TestClient, tmp_path: Path, key: str, value: str
) -> None:
    """beets never refuses these: it tests them with a bare ``if``
    (``importer/session.py:99-138``), where any non-empty string is on, so
    ``hardlink: 'no'`` hardlinks. MusicDrop's own rule refuses the string, with
    the row it had before beets' typed reads were added."""
    text = _head(tmp_path) + f"import:\n  {key}: {value}\n"

    r = client.post("/api/config/validate", json={"yaml_text": text})

    assert r.status_code == 200
    assert r.json() == {
        "errors": [
            {
                "loc": f"import.{key}",
                "msg": "Value error, must be a bool: write yes or no, without quotes",
                "type": "value_error",
                "line": 4,
                "column": len(f"  {key}: "),
            }
        ],
        "advisories": [],
    }


def test_validate_reads_a_file_with_a_document_marker_as_yaml_1_1(
    client: TestClient, tmp_path: Path
) -> None:
    """``---`` made ruamel read ``yes`` as a string (YAML 1.2); beets reads a bool.

    One row, for ``maybe``, on the file's own line 7.
    """
    text = "---\n# note\n" + _head(tmp_path) + "import:\n  copy: yes\n  move: maybe\n"

    r = client.post("/api/config/validate", json={"yaml_text": text})

    assert r.status_code == 200
    assert r.json() == {
        "errors": [
            {
                "loc": "import.move",
                "msg": _NOT_A_BOOL,
                "type": "beets_read",
                "line": 7,
                "column": 8,
            }
        ],
        "advisories": [],
    }
