import hashlib

import pytest
from fastapi.testclient import TestClient

from app.beets.library import LibraryHandle


def _cfg_sha(client: TestClient) -> str:
    body = client.get("/api/config/naming").json()
    return str(body["sha256"])


def test_get_naming_returns_split_and_previews(client: TestClient) -> None:
    r = client.get("/api/config/naming")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {
        "default",
        "comp",
        "singleton",
        "custom",
        "replace",
        "sha256",
        "previews",
    }


def test_preview_renders_against_synthetic_when_empty(client: TestClient) -> None:
    r = client.post(
        "/api/config/naming/preview",
        json={
            "rules": [{"query": "default", "template": "$albumartist/$album/$track $title"}],
            "replace": [],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["rendered"][0]["sample_path"] == "Adele/25/01 Hello.flac"
    assert body["replace_errors"] == []


def test_preview_reports_bad_replace_regex(client: TestClient) -> None:
    r = client.post(
        "/api/config/naming/preview",
        json={
            "rules": [{"query": "default", "template": "$album/$title"}],
            "replace": [{"pattern": "(", "replacement": "_"}],
        },
    )
    body = r.json()
    assert body["replace_errors"][0]["index"] == 0


def test_save_naming_writes_and_sets_apply_pending(client: TestClient) -> None:
    sha = _cfg_sha(client)
    r = client.post(
        "/api/config/naming/save",
        json={
            "rules": [{"query": "default", "template": "$albumartist/$album/$track $title"}],
            "replace": [{"pattern": "[?]", "replacement": "_"}],
            "base_sha256": sha,
        },
    )
    assert r.status_code == 200
    assert r.json()["apply_pending"] is True
    assert client.get("/api/config/naming").json()["default"].endswith("$track $title")


def test_save_naming_409_on_stale_sha(client: TestClient) -> None:
    r = client.post(
        "/api/config/naming/save",
        json={"rules": [], "replace": [], "base_sha256": "stale"},
    )
    assert r.status_code == 409


_UNCLOSED_TEXT = (
    "while parsing a flow sequence\n"
    '  in "<unicode string>", line 2, column 4:\n'
    "    x: [unclosed\n"
    "       ^ (line: 2)\n"
    "expected ',' or ']', but got '<stream end>'\n"
    '  in "<unicode string>", line 3, column 1:\n'
    "    \n"
    "    ^ (line: 3)"
)

_BROKEN_ON_DISK = pytest.mark.parametrize(
    ("text", "error"),
    [("a: 1\nx: [unclosed\n", _UNCLOSED_TEXT), ("a: 1\nx: !!bool ture\n", "KeyError: 'ture'")],
    ids=["syntax", "mistyped-tag"],
)


@_BROKEN_ON_DISK
def test_get_naming_names_the_parse_error_of_the_file_on_disk(
    client: TestClient, beets_library: LibraryHandle, text: str, error: str
) -> None:
    """Measured before: a bare 500 for both, while ``GET /api/config`` answered 200."""
    beets_library.config_path.write_text(text, encoding="utf-8")

    r = client.get("/api/config/naming")

    assert r.status_code == 422, r.text
    assert r.json() == {"detail": f"config.yaml does not parse: {error}"}
    assert client.get("/api/config").status_code == 200


@_BROKEN_ON_DISK
def test_save_naming_names_the_parse_error_of_the_file_on_disk(
    client: TestClient, beets_library: LibraryHandle, text: str, error: str
) -> None:
    """The base hash matches the broken file, so the save reaches its re-read."""
    beets_library.config_path.write_text(text, encoding="utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    r = client.post(
        "/api/config/naming/save",
        json={"rules": [], "replace": [], "base_sha256": sha},
    )

    assert r.status_code == 422, r.text
    assert r.json() == {
        "detail": [{"loc": "", "msg": f"config.yaml does not parse: {error}", "type": "yaml_parse"}]
    }
    assert beets_library.config_path.read_text(encoding="utf-8") == text
