"""A running disk sync must 409 every other library writer, and vice versa."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from beets.library import Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.main import app
from tests.conftest import make_test_handle


@pytest.fixture
def sync_client(edit_lib: Library, tmp_path: Path) -> Iterator[TestClient]:
    handle = make_test_handle(edit_lib, tmp_path)
    app.dependency_overrides[get_library] = lambda: handle
    app.state.beets_library = handle
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _fake_sync_running(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.disk_sync_jobs.registry import get_disk_sync_registry

    get_disk_sync_registry().start()  # slot held, phase running


def test_reorganize_409_while_disk_sync_runs(
    sync_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_sync_running(monkeypatch)
    assert sync_client.post("/api/reorganize").status_code == 409


def test_lyrics_backfill_409_while_disk_sync_runs(
    sync_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_sync_running(monkeypatch)
    assert sync_client.post("/api/lyrics/backfill").status_code == 409


def test_delete_409_while_disk_sync_runs(
    sync_client: TestClient, edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_sync_running(monkeypatch)
    album_id = int(next(iter(edit_lib.albums())).id)
    assert sync_client.delete(f"/api/albums/{album_id}").status_code == 409


def test_import_409_while_disk_sync_runs(
    sync_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Mirror the request body an existing import test uses (grep
    # `post("/api/import"` in backend/tests/ and copy its json= payload) —
    # the gate must fire (409) before body-shape validation matters.
    _fake_sync_running(monkeypatch)
    src = tmp_path / "src"
    src.mkdir()
    r = sync_client.post("/api/import", json={"path": str(src)})
    assert r.status_code == 409
