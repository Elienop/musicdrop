"""API tests for the Trash management endpoints (list / restore / empty)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _trash_dir(client: TestClient) -> Path:
    """The active Trash dir (read off the list endpoint's response)."""
    return Path(client.get("/api/trash").json()["trash_path"])


def test_get_trash_empty(client: TestClient) -> None:
    r = client.get("/api/trash")
    assert r.status_code == 200
    body = r.json()
    assert body["albums"] == []
    assert isinstance(body["trash_path"], str) and body["trash_path"]


def test_empty_one_removes_folder(client: TestClient) -> None:
    trash = _trash_dir(client)
    (trash / "Album").mkdir(parents=True)
    r = client.delete("/api/trash", params={"folder": "Album"})
    assert r.status_code == 200
    assert r.json()["removed"] == 1
    assert not (trash / "Album").exists()


def test_empty_all_removes_seeded_folders(client: TestClient) -> None:
    trash = _trash_dir(client)
    (trash / "A").mkdir(parents=True)
    (trash / "B").mkdir(parents=True)
    r = client.delete("/api/trash/all")
    assert r.status_code == 200
    assert r.json()["removed"] == 2
    assert list(trash.iterdir()) == []


def test_empty_rejects_path_traversal_404(client: TestClient) -> None:
    r = client.delete("/api/trash", params={"folder": "../escape"})
    assert r.status_code == 404


def test_empty_unknown_folder_404(client: TestClient) -> None:
    r = client.delete("/api/trash", params={"folder": "does-not-exist"})
    assert r.status_code == 404


def test_restore_409_when_a_library_job_is_active(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Active:
        def has_active_job(self) -> bool:
            return True

    monkeypatch.setattr("app.api.trash.get_registry", lambda: _Active())
    r = client.post("/api/trash/restore", json={"folder": "whatever"})
    assert r.status_code == 409
