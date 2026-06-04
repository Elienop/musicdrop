from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.artists import get_artist_art_write_toggle
from app.artwork.toggle import ArtistArtWriteToggle
from app.main import app


@pytest.fixture
def toggle(tmp_path: Path) -> ArtistArtWriteToggle:
    return ArtistArtWriteToggle(tmp_path / "art.json", default=False)


@pytest.fixture
def client(toggle: ArtistArtWriteToggle) -> Iterator[TestClient]:
    app.dependency_overrides[get_artist_art_write_toggle] = lambda: toggle
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_settings_get_put(client: TestClient) -> None:
    assert client.get("/api/artists/art/settings").json() == {"enabled": False}
    put = client.put("/api/artists/art/settings", json={"enabled": True})
    assert put.json() == {"enabled": True}
    assert client.get("/api/artists/art/settings").json() == {"enabled": True}


def test_apply_403_when_disabled(client: TestClient) -> None:
    r = client.post("/api/artists/art/apply", params={"name": "ABBA"})
    assert r.status_code == 403


def test_backfill_status_idle(client: TestClient) -> None:
    assert client.get("/api/artists/art/backfill").json()["phase"] == "idle"
