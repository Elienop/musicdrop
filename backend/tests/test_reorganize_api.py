# backend/tests/test_reorganize_api.py
from collections.abc import Iterator
from pathlib import Path

import pytest
from beets.library import Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.main import app
from tests.conftest import make_test_handle


@pytest.fixture
def reorg_client(reorganize_lib: Library, tmp_path: Path) -> Iterator[TestClient]:
    handle = make_test_handle(reorganize_lib, tmp_path)
    app.dependency_overrides[get_library] = lambda: handle
    app.state.beets_library = handle
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_preview_library(reorg_client: TestClient) -> None:
    resp = reorg_client.get("/api/reorganize/preview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope_label"] == "library"
    assert body["total"] == 4 and body["will_move"] == 3 and body["already_in_place"] == 1


def test_preview_artist(reorg_client: TestClient) -> None:
    resp = reorg_client.get("/api/reorganize/preview", params={"artist": "Radiohead"})
    assert resp.status_code == 200
    assert resp.json()["scope_label"] == "Radiohead"
    assert resp.json()["will_move"] == 1


def test_preview_album_404(reorg_client: TestClient) -> None:
    assert reorg_client.get("/api/albums/999999/reorganize/preview").status_code == 404


def _fake_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the daemon-thread spawn with a synchronous no-op sweep.

    The real ``start_backfill`` launches a background thread that calls into beets
    (reading config like ``timeout``); if it is still running when the autouse
    reset fixtures tear the beets globals down it raises a stray
    ``confuse.NotFoundError``. Driving the registry to ``done`` synchronously
    (the same pattern as ``test_lyrics_api``) keeps these tests deterministic.
    """
    import app.api.reorganize as reorganize_api

    def fake_start_backfill(reg: object, handle: object, **kwargs: object) -> None:
        reg.set_total(0)  # type: ignore[attr-defined]  # fake reg is the real registry
        reg.finish("done")  # type: ignore[attr-defined]

    monkeypatch.setattr(reorganize_api, "start_backfill", fake_start_backfill)


def test_status_idle_then_start(reorg_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_sweep(monkeypatch)
    assert reorg_client.get("/api/reorganize/status").json()["phase"] == "idle"
    started = reorg_client.post("/api/reorganize")
    assert started.status_code == 200
    assert started.json()["phase"] in ("running", "done")
    assert started.json()["scope_label"] == "library"


def test_start_album_scope_sets_album_id(
    reorg_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_sweep(monkeypatch)
    album_id = reorg_client.get("/api/albums", params={"limit": 50}).json()["items"][0]["id"]
    resp = reorg_client.post(f"/api/albums/{album_id}/reorganize")
    assert resp.status_code == 200
    assert resp.json()["album_id"] == album_id


def test_stop_returns_status(reorg_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_sweep(monkeypatch)
    reorg_client.post("/api/reorganize")
    resp = reorg_client.post("/api/reorganize/stop")
    assert resp.status_code == 200
    assert resp.json()["phase"] in ("running", "stopped", "done")
