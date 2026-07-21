"""API tests for the Trash management endpoints (list / restore / empty)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.models.trash import EmptyResult


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
    from app.import_jobs.registry import get_registry

    monkeypatch.setattr(get_registry(), "has_active_job", lambda: True)
    r = client.post("/api/trash/restore", json={"folder": "whatever"})
    assert r.status_code == 409


class _Locked:
    """Stub for a held beets swap lock (``asyncio.Lock`` in production)."""

    @staticmethod
    def locked() -> bool:
        return True


def test_empty_one_409_while_swap_lock_held(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restore holds the beets swap lock WITHOUT registering as a library job,
    so the job-only gate let empty-trash proceed and ``rmtree`` the very folder the
    restore was mid-move on — irreversible loss. The gate must also see the lock."""
    from app.main import app

    trash = _trash_dir(client)
    (trash / "Album").mkdir(parents=True)
    monkeypatch.setattr(app.state, "beets_swap_lock", _Locked(), raising=False)
    r = client.delete("/api/trash", params={"folder": "Album"})
    assert r.status_code == 409
    assert (trash / "Album").exists()  # NOT deleted out from under the restore


def test_empty_all_409_while_swap_lock_held(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty-all must likewise refuse while a restore holds the swap lock."""
    from app.main import app

    trash = _trash_dir(client)
    (trash / "A").mkdir(parents=True)
    monkeypatch.setattr(app.state, "beets_swap_lock", _Locked(), raising=False)
    r = client.delete("/api/trash/all")
    assert r.status_code == 409
    assert (trash / "A").exists()


def test_empty_one_holds_swap_lock_during_removal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty must HOLD the swap lock while it rmtrees, so a Restore starting
    mid-Empty sees it (raise_if_library_busy) and 409s — this closes the
    symmetric Empty→Restore race (a Restore move-importing the folder Empty is
    concurrently deleting). Job-only exclusion left Empty invisible."""
    import app.api.trash as trash_mod
    from app.beets.trash_manage import empty_one as real_empty_one
    from app.main import app

    trash = _trash_dir(client)
    (trash / "Album").mkdir(parents=True)
    seen: dict[str, bool] = {}

    def spy(path: str) -> EmptyResult:
        lock = getattr(app.state, "beets_swap_lock", None)
        seen["locked"] = lock is not None and lock.locked()
        return real_empty_one(path)

    monkeypatch.setattr(trash_mod, "empty_one", spy)
    r = client.delete("/api/trash", params={"folder": "Album"})
    assert r.status_code == 200
    assert seen.get("locked") is True  # the swap lock was held across the rmtree


def test_empty_all_holds_swap_lock_during_removal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty-all likewise holds the swap lock across its removals."""
    import app.api.trash as trash_mod
    from app.beets.trash_manage import empty_all as real_empty_all
    from app.main import app

    trash = _trash_dir(client)
    (trash / "A").mkdir(parents=True)
    seen: dict[str, bool] = {}

    def spy(trash_dir: Path) -> EmptyResult:
        lock = getattr(app.state, "beets_swap_lock", None)
        seen["locked"] = lock is not None and lock.locked()
        return real_empty_all(trash_dir)

    monkeypatch.setattr(trash_mod, "empty_all", spy)
    r = client.delete("/api/trash/all")
    assert r.status_code == 200
    assert seen.get("locked") is True


def test_restore_409_while_swap_lock_held(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The symmetric guarantee that makes Empty holding the lock effective:
    Restore refuses (409) while the swap lock is held, so it can never start
    move-importing during an in-flight Empty (or config Apply / resolve)."""
    from app.main import app

    monkeypatch.setattr(app.state, "beets_swap_lock", _Locked(), raising=False)
    r = client.post("/api/trash/restore", json={"folder": "whatever"})
    assert r.status_code == 409
