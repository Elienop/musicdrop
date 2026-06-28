from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from beets.library import Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.api.artists import get_artist_image_cache
from app.artwork.cache import ArtistImageCache
from app.main import app
from tests.conftest import make_test_handle


class _RecordingBroker:
    """Records which event kind each emit helper triggers.

    The emit helpers call exactly one of these two methods, so the recorded
    list lets a test assert an endpoint emits library:changed vs art:changed.
    """

    def __init__(self) -> None:
        self.events: list[str] = []

    def publish_library_changed(self) -> None:
        self.events.append("library:changed")

    def publish_art_changed(self) -> None:
        self.events.append("art:changed")


@pytest.fixture
def edit_client(edit_lib: Library, tmp_path: Path) -> Iterator[tuple[TestClient, _RecordingBroker]]:
    handle = make_test_handle(edit_lib, tmp_path)
    broker = _RecordingBroker()
    app.state.event_broker = broker
    app.state.beets_library = handle
    app.dependency_overrides[get_library] = lambda: handle
    yield TestClient(app), broker
    app.dependency_overrides.clear()
    if hasattr(app.state, "event_broker"):
        delattr(app.state, "event_broker")
    if hasattr(app.state, "beets_library"):
        delattr(app.state, "beets_library")


def test_album_edit_emits_library_changed(
    edit_client: tuple[TestClient, _RecordingBroker],
) -> None:
    client, broker = edit_client
    albums = client.get("/api/albums").json()["items"]
    album_id = albums[0]["id"]
    resp = client.post(f"/api/albums/{album_id}/edit", json={"album": {"title": "X"}, "tracks": []})
    assert resp.status_code == 200
    # A tag edit changes list/metadata data, not image bytes → library:changed.
    assert broker.events == ["library:changed"]


@pytest.fixture
def art_client(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, _RecordingBroker, ArtistImageCache]]:
    cache = ArtistImageCache(tmp_path)
    broker = _RecordingBroker()
    app.state.event_broker = broker
    app.dependency_overrides[get_artist_image_cache] = lambda: cache
    yield TestClient(app), broker, cache
    app.dependency_overrides.clear()
    if hasattr(app.state, "event_broker"):
        delattr(app.state, "event_broker")


def test_artist_image_clear_emits_art_changed(
    art_client: tuple[TestClient, _RecordingBroker, ArtistImageCache],
) -> None:
    client, broker, cache = art_client
    cache.write_override("ABBA", b"manual", "image/png")
    resp = client.delete("/api/artists/image/override", params={"name": "ABBA"})
    assert resp.status_code == 204
    # Clearing an artist override changes the served image BYTES → art:changed.
    assert broker.events == ["art:changed"]
