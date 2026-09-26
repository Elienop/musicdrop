from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.slskd import service


def test_settings_round_trip_redacts_secrets(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pinned: a lifespan test earlier in the suite leaves ``app.state.inbox_dir`` set.
    monkeypatch.setattr(app.state, "inbox_dir", tmp_path, raising=False)
    r = client.get("/api/slskd/settings")
    assert r.status_code == 200
    assert r.json() == {
        "base_url": "",
        "folder": str(tmp_path),
        "folder_exists": True,
        "downloads_prefix": "",
        "auto_import": False,
        "has_token": False,
        "has_webhook_secret": False,
        "last_download_missed": False,
    }

    r = client.put(
        "/api/slskd/settings",
        json={
            "base_url": "http://slskd:5030",
            "token": "apikey",
            "downloads_prefix": "/downloads",
            "webhook_secret": "hooksecret",
            "auto_import": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "base_url": "http://slskd:5030",
        "folder": str(tmp_path),
        "folder_exists": True,
        "downloads_prefix": "/downloads",
        "auto_import": True,
        "has_token": True,
        "has_webhook_secret": True,
        "last_download_missed": False,
    }
    # Neither secret is ever returned — not as a field, not anywhere in the body.
    assert "token" not in body
    assert "webhook_secret" not in body
    assert "apikey" not in r.text
    assert "hooksecret" not in r.text


def test_put_blank_token_keeps_existing(client: TestClient) -> None:
    client.put("/api/slskd/settings", json={"base_url": "http://slskd:5030", "token": "apikey"})
    # A later PUT that omits token must not wipe the saved key.
    r = client.put(
        "/api/slskd/settings", json={"base_url": "http://slskd:5030", "auto_import": True}
    )
    assert r.json()["has_token"] is True
    assert r.json()["auto_import"] is True


def test_get_reflects_has_webhook_secret_without_leaking_it(client: TestClient) -> None:
    # Before any save, no secret is configured.
    assert client.get("/api/slskd/settings").json()["has_webhook_secret"] is False

    client.put("/api/slskd/settings", json={"webhook_secret": "hooksecret"})

    r = client.get("/api/slskd/settings")
    assert r.status_code == 200
    assert r.json()["has_webhook_secret"] is True
    # The flag is exposed, never the value itself.
    assert "hooksecret" not in r.text


def test_test_endpoint_ok(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.client, "check", lambda base_url, token: "0.22.3")
    client.put("/api/slskd/settings", json={"base_url": "http://slskd:5030", "token": "t"})
    r = client.post("/api/slskd/test")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "version": "0.22.3", "error": None}


def test_test_endpoint_unconfigured(client: TestClient) -> None:
    r = client.post("/api/slskd/test")
    assert r.status_code == 200
    assert r.json()["ok"] is False
