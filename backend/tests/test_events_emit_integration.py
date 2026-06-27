from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from beets.library import Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.main import app
from tests.conftest import make_test_handle


class _CountingBroker:
    """Minimal stand-in: emit_library_changed only calls publish_library_changed."""

    def __init__(self) -> None:
        self.count = 0

    def publish_library_changed(self) -> None:
        self.count += 1


@pytest.fixture
def edit_client(edit_lib: Library, tmp_path: Path) -> Iterator[tuple[TestClient, _CountingBroker]]:
    handle = make_test_handle(edit_lib, tmp_path)
    broker = _CountingBroker()
    app.state.event_broker = broker
    app.state.beets_library = handle
    app.dependency_overrides[get_library] = lambda: handle
    yield TestClient(app), broker
    app.dependency_overrides.clear()
    if hasattr(app.state, "event_broker"):
        delattr(app.state, "event_broker")
    if hasattr(app.state, "beets_library"):
        delattr(app.state, "beets_library")


def test_album_edit_emits_library_changed(edit_client: tuple[TestClient, _CountingBroker]) -> None:
    client, broker = edit_client
    albums = client.get("/api/albums").json()["items"]
    album_id = albums[0]["id"]
    resp = client.post(f"/api/albums/{album_id}/edit", json={"album": {"title": "X"}, "tracks": []})
    assert resp.status_code == 200
    assert broker.count == 1
