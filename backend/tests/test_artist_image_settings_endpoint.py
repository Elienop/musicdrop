from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.artists import get_artist_image_toggle
from app.artwork.toggle import ArtistImageToggle
from app.main import app


@pytest.fixture
def toggle(tmp_path: Path) -> ArtistImageToggle:
    return ArtistImageToggle(tmp_path / "_enabled.json", default=False)


@pytest.fixture
def client(toggle: ArtistImageToggle) -> Iterator[TestClient]:
    app.dependency_overrides[get_artist_image_toggle] = lambda: toggle
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_get_reflects_disabled_default(client: TestClient) -> None:
    resp = client.get("/api/artists/image/settings")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False}


def test_put_enables_and_persists(
    client: TestClient, toggle: ArtistImageToggle, tmp_path: Path
) -> None:
    resp = client.put("/api/artists/image/settings", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True}
    assert toggle.is_enabled() is True
    # Persisted: a fresh toggle on the same path reads True.
    assert ArtistImageToggle(tmp_path / "_enabled.json", default=False).is_enabled() is True


def test_put_then_get_roundtrips(client: TestClient) -> None:
    client.put("/api/artists/image/settings", json={"enabled": True})
    assert client.get("/api/artists/image/settings").json() == {"enabled": True}
