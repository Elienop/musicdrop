import pytest
from fastapi.testclient import TestClient

from app.plex import service
from app.plex.errors import PlexNotConfigured


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
    assert r.json() == {
        "base_url": "",
        "library_path": "",
        "library_section": "",
        "has_token": False,
    }

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


def test_settings_round_trips_library_section(client: TestClient) -> None:
    # The ``client`` fixture routes the Plex store into the tmp beets_dir, so the
    # PUT persists and a fresh GET reads it back (mirrors the base_url/library_path
    # round-trip above).
    r = client.put("/api/plex/settings", json={"library_section": "MusicDrop"})
    assert r.status_code == 200
    assert r.json()["library_section"] == "MusicDrop"
    assert client.get("/api/plex/settings").json()["library_section"] == "MusicDrop"


def test_sections_endpoint_lists_titles_with_their_folders(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The folders ride along on the wire so Settings can show the user what Plex
    # actually reports instead of asking them to retype it from memory.
    from app.models.plex import PlexSectionInfo

    monkeypatch.setattr(
        "app.api.plex.service.list_music_sections",
        lambda config: [
            PlexSectionInfo(title="Music", locations=["/data/music"]),
            PlexSectionInfo(title="MusicDrop", locations=["/musicdrop", "/mnt/spill"]),
        ],
    )
    r = client.get("/api/plex/sections")
    assert r.status_code == 200
    assert r.json() == {
        "sections": [
            {"title": "Music", "locations": ["/data/music"]},
            {"title": "MusicDrop", "locations": ["/musicdrop", "/mnt/spill"]},
        ]
    }


def test_sections_endpoint_409_when_unconfigured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(config: object) -> list[object]:
        raise PlexNotConfigured("Plex is not configured.")

    monkeypatch.setattr("app.api.plex.service.list_music_sections", _raise)
    assert client.get("/api/plex/sections").status_code == 409


def test_playlists_endpoint_lists_audio(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models.plex import PlexPlaylistInfo

    monkeypatch.setattr(
        "app.api.plex.playlists_pull.list_audio_playlists",
        lambda config: [
            PlexPlaylistInfo(name="Road", track_count=2, rating_key="11"),
            # Plex allows duplicate titles — the listing must keep both, each
            # carrying its own identity so the picker can tell them apart.
            PlexPlaylistInfo(name="Road", track_count=5, rating_key="22"),
        ],
    )
    r = client.get("/api/plex/playlists")
    assert r.status_code == 200
    assert r.json() == {
        "playlists": [
            {"name": "Road", "track_count": 2, "rating_key": "11"},
            {"name": "Road", "track_count": 5, "rating_key": "22"},
        ]
    }


def test_playlists_endpoint_409_when_unconfigured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(config: object) -> list[object]:
        raise PlexNotConfigured("Plex is not configured.")

    monkeypatch.setattr("app.api.plex.playlists_pull.list_audio_playlists", _raise)
    assert client.get("/api/plex/playlists").status_code == 409
