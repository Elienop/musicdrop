from fastapi.testclient import TestClient


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
