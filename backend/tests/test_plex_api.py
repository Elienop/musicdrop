import pytest
from fastapi.testclient import TestClient

from app.plex import service


class _FakeUser:
    def __init__(self, uid: int, title: str, home: bool) -> None:
        self.id = uid
        self.title = title
        self.username = title
        self.email = f"{title}@example.com"
        self.home = home


class _FakeAccount:
    def users(self) -> list[_FakeUser]:
        return [_FakeUser(1, "alice", True)]


class _FakeServer:
    friendlyName = "Living Room"

    def myPlexAccount(self) -> _FakeAccount:
        return _FakeAccount()


def test_settings_round_trip_redacts_token(client: TestClient) -> None:
    r = client.get("/api/plex/settings")
    assert r.status_code == 200
    assert r.json() == {"base_url": "", "library_path": "", "has_token": False}

    r = client.put(
        "/api/plex/settings",
        json={"base_url": "http://plex:32400", "token": "secret", "library_path": "/data/music"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["base_url"] == "http://plex:32400"
    assert body["library_path"] == "/data/music"
    assert body["has_token"] is True
    assert "token" not in body and "secret" not in r.text


def test_test_endpoint_ok(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.client, "connect", lambda base_url, token: _FakeServer())
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})
    r = client.post("/api/plex/test")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "server_name": "Living Room", "error": None}


def test_test_endpoint_unconfigured(client: TestClient) -> None:
    r = client.post("/api/plex/test")
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_users_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.client, "connect", lambda base_url, token: _FakeServer())
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})
    r = client.get("/api/plex/users")
    assert r.status_code == 200
    assert r.json() == {"users": [{"id": "1", "name": "alice", "home": True}]}


def test_users_unconfigured_409(client: TestClient) -> None:
    r = client.get("/api/plex/users")
    assert r.status_code == 409
