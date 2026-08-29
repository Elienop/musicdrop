"""Endpoint tests for /api/disk-sync (preview/start/status/stop)."""

from __future__ import annotations

import os
import shutil
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


def test_preview_lists_missing_file(sync_client: TestClient, edit_lib: Library) -> None:
    victim = next(iter(edit_lib.items()))
    os.remove(victim.path)
    r = sync_client.get("/api/disk-sync/preview")
    assert r.status_code == 200
    body = r.json()
    assert body["will_remove"] == 1
    assert len(body["removals"]) == 1


def test_preview_503_when_root_missing(sync_client: TestClient, edit_lib: Library) -> None:
    shutil.rmtree(os.fsdecode(edit_lib.directory))
    r = sync_client.get("/api/disk-sync/preview")
    assert r.status_code == 503
    assert "unavailable" in r.json()["detail"].lower()


def test_start_runs_job_to_done(sync_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Make the daemon synchronous-deterministic: run sweep inline.
    import app.api.disk_sync as api_mod
    from app.disk_sync_jobs.runner import sweep

    monkeypatch.setattr(
        api_mod,
        "start_backfill",
        lambda reg, handle, *, playlists_dir=None, on_complete=None: sweep(
            reg, handle, playlists_dir=playlists_dir, on_complete=on_complete
        ),
    )
    r = sync_client.post("/api/disk-sync")
    assert r.status_code == 200
    assert r.json()["phase"] in {"running", "done"}
    status = sync_client.get("/api/disk-sync/status")
    assert status.status_code == 200
    assert status.json()["phase"] == "done"


def test_start_passes_the_overridden_playlists_dir_to_the_worker(
    sync_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route must resolve get_playlists_dir via Depends, never a direct
    call — a direct call escapes app.dependency_overrides, handing the worker
    the settings-derived REAL playlist store while every test override
    silently misses it."""
    import app.api.disk_sync as api_mod
    from app.playlists.store import get_playlists_dir

    sentinel = tmp_path / "override-playlists"
    app.dependency_overrides[get_playlists_dir] = lambda: sentinel

    received: list[Path | None] = []
    monkeypatch.setattr(
        api_mod,
        "start_backfill",
        lambda reg, handle, *, playlists_dir=None, on_complete=None: received.append(playlists_dir),
    )
    assert sync_client.post("/api/disk-sync").status_code == 200
    assert received == [sentinel]


def test_start_409_while_reorganize_runs(sync_client: TestClient) -> None:
    from app.reorganize_jobs.registry import get_reorganize_backfill

    get_reorganize_backfill().start(
        scope="library", artist=None, album_id=None, scope_label="library"
    )
    assert sync_client.post("/api/disk-sync").status_code == 409


def test_start_409_while_already_syncing(sync_client: TestClient) -> None:
    from app.disk_sync_jobs.registry import get_disk_sync_registry

    get_disk_sync_registry().start()
    assert sync_client.post("/api/disk-sync").status_code == 409


def test_stop_endpoint(sync_client: TestClient) -> None:
    from app.disk_sync_jobs.registry import get_disk_sync_registry

    get_disk_sync_registry().start()
    r = sync_client.post("/api/disk-sync/stop")
    assert r.status_code == 200
