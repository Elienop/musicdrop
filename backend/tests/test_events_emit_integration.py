from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from beets.library import Library
from fastapi.testclient import TestClient

import app.api.artists as artists_mod
from app.api.albums import get_library
from app.api.artists import get_artist_image_cache, get_artist_image_service
from app.artwork.cache import ArtistImageCache
from app.beets.artist_art import ArtTrashStore
from app.main import app
from tests.conftest import beets_dir_for, make_test_handle, protected_for


class _RecordingBroker:
    """Records which event kind each emit helper triggers.

    The emit helpers call exactly one of these two methods, so the recorded
    list lets a test assert an endpoint emits library:changed vs art:changed.
    ``art_scopes`` additionally records WHICH asset each art event named, so a
    test can pin that an endpoint scopes its bump instead of repainting every
    image in every open tab.
    """

    def __init__(self) -> None:
        self.events: list[str] = []
        self.art_scopes: list[str | None] = []

    def publish_library_changed(self) -> None:
        self.events.append("library:changed")

    def publish_art_changed(self, scope: str | None = None) -> None:
        self.events.append("art:changed")
        self.art_scopes.append(scope)


@pytest.fixture
def edit_client(edit_lib: Library, tmp_path: Path) -> Iterator[tuple[TestClient, _RecordingBroker]]:
    handle = make_test_handle(edit_lib, beets_dir_for(tmp_path))
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, _RecordingBroker, ArtistImageCache]]:
    cache = ArtistImageCache(tmp_path / "cache")
    # Reset moves a stored override to Trash and clears the automatic slot, and
    # stub handle below cannot satisfy the real resolver (it needs a beets
    # library). What lands in this store is pinned in
    # tests/test_artist_image_reset_to_trash.py; here it only has to work.
    store = ArtTrashStore(
        trash_dir=tmp_path / "trash",
        origins_dir=tmp_path / "trash-origins",
        protected=protected_for(
            trash_dir=tmp_path / "trash", origins_dir=tmp_path / "trash-origins"
        ),
    )
    monkeypatch.setattr(artists_mod, "_checked_art_trash_store", lambda *_a, **_kw: store)
    broker = _RecordingBroker()
    app.state.event_broker = broker
    app.dependency_overrides[get_artist_image_cache] = lambda: cache

    class _OffService:
        """Reset kicks a background refill when the feature is on, and that
        fill emits its OWN coalesced art:changed. These tests count emits, so
        they pin the reset's emit with the refill deliberately off - the refill
        is covered in test_artist_image_override_endpoint.py."""

        def is_enabled(self) -> bool:
            return False

    app.dependency_overrides[get_artist_image_service] = lambda: _OffService()
    app.dependency_overrides[get_library] = lambda: SimpleNamespace(lib=object())
    yield TestClient(app), broker, cache
    app.dependency_overrides.clear()
    if hasattr(app.state, "event_broker"):
        delattr(app.state, "event_broker")


def test_album_cover_install_emits_album_scoped_art_changed(
    edit_client: tuple[TestClient, _RecordingBroker],
) -> None:
    client, broker = edit_client
    albums = client.get("/api/albums").json()["items"]
    album_id = albums[0]["id"]
    png = (Path(__file__).parent / "fixtures" / "cover.png").read_bytes()
    resp = client.post(
        f"/api/albums/{album_id}/cover", files={"file": ("cover.png", png, "image/png")}
    )
    assert resp.status_code == 200
    # Only THIS album's cover changed — tabs must remount that one <img>, not
    # every cover on a 192-per-page browse grid.
    assert broker.events == ["art:changed"]
    assert broker.art_scopes == [f"album:{album_id}"]


def test_artist_image_reset_emits_art_changed(
    art_client: tuple[TestClient, _RecordingBroker, ArtistImageCache],
) -> None:
    client, broker, cache = art_client
    cache.write_override("ABBA", b"manual", "image/png")
    resp = client.post("/api/artists/image/reset", params={"name": "ABBA"})
    assert resp.status_code == 200
    # Resetting an artist portrait changes the served image BYTES → art:changed,
    # UNSCOPED: the portrait is served under a NORMALIZED name, so a raw
    # display name is not a reliable asset identity (see api/artists.py).
    assert broker.events == ["art:changed"]
    assert broker.art_scopes == [None]


def test_artist_image_upload_emits_unscoped_art_changed(
    art_client: tuple[TestClient, _RecordingBroker, ArtistImageCache],
) -> None:
    client, broker, _cache = art_client
    png = (Path(__file__).parent / "fixtures" / "cover.png").read_bytes()
    resp = client.post(
        "/api/artists/image/override",
        params={"name": "Radiohead"},
        files={"file": ("p.png", png, "image/png")},
    )
    assert resp.status_code == 200
    assert broker.art_scopes == [None]


def test_artist_art_backfill_completion_stays_unscoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sweep repaints MANY artists, so its completion event carries no scope —
    the frontend then bumps the global counter and refreshes everything."""
    import app.api.artists as artists_mod

    captured: dict[str, object] = {}

    def _fake_start(*_args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(artists_mod, "start_art_backfill", _fake_start)
    broker = _RecordingBroker()
    stub_app = SimpleNamespace(state=SimpleNamespace(event_broker=broker))
    artists_mod._start(stub_app, cast(Any, None), None, force=False, artist=None)

    on_complete = captured["on_complete"]
    assert callable(on_complete)
    on_complete()
    assert broker.events == ["art:changed"]
    assert broker.art_scopes == [None]
