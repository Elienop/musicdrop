"""API tests for the Trash management endpoints (list / restore / empty)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.beets.config_editor import _settings
from app.beets.trash import resolve_trash_origins_dir
from app.beets.trash_origins import write_trash_origin
from app.models.trash import EmptyResult


def _trash_dir(client: TestClient) -> Path:
    """The active Trash dir (read off the list endpoint's response)."""
    return Path(client.get("/api/trash").json()["trash_path"])


def _origins_dir(client: TestClient) -> Path:
    """The active origin store, resolved exactly as the routes resolve it.

    Deliberately NOT read off a response field: the origins path is not on the
    wire (adding it would be a contract change for something no UI shows), so a
    test that needs it has to resolve it the way ``app/api/trash.py`` does.
    """
    app: Any = client.app  # TestClient.app is typed as a bare ASGI callable
    return resolve_trash_origins_dir(_settings(app), app.state.beets_library)


def test_get_trash_empty(client: TestClient) -> None:
    r = client.get("/api/trash")
    assert r.status_code == 200
    body = r.json()
    assert body["albums"] == []
    assert isinstance(body["trash_path"], str)
    assert body["trash_path"]


def test_list_trash_reports_the_move_back_a_real_record_offers(
    client: TestClient, tmp_path: Path
) -> None:
    """The one argument the ROUTE contributes: which music dir the record is judged against.

    ``restore_mode`` / ``restore_note`` / ``origin`` were exercised only below
    the API, and ``list_trash`` supplies the value that decides all three —
    whether a recorded origin is still inside the library. Point it anywhere
    else and every row degrades to ``"import"`` carrying the "not inside the
    current music library" note, with every unit test still green and the UI
    quietly telling the user their exact restore is gone.

    Since the record moved to a sibling store the route supplies TWO such values
    — the music dir AND the origins dir — and forgetting either degrades every
    row the same silent way, so this covers both at once.

    The record is written by the real writer, into the store the route itself
    resolves, so the row is produced end to end rather than from a hand-built
    model.
    """
    trash = _trash_dir(client)
    entry = trash / "Weird Folder"
    entry.mkdir(parents=True)
    (entry / "cover.jpg").write_bytes(b"\x00")
    origin = tmp_path / "music" / "Weird Folder"
    write_trash_origin(_origins_dir(client), entry.name, origin=str(origin), moved="folder")

    r = client.get("/api/trash")

    assert r.status_code == 200
    (row,) = r.json()["albums"]
    assert row["restore_mode"] == "move_back"
    assert row["restore_note"] is None  # nothing to warn about on an exact restore
    assert row["origin"] == str(origin)


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


def test_empty_overlong_folder_404_not_500(client: TestClient) -> None:
    """A >255-byte folder name must hit the same 404 as any other unknown
    folder. Path.exists() raises OSError(ENAMETOOLONG) on such a component
    (it only swallows ENOENT/ENOTDIR/EBADF/ELOOP), which used to 500 the
    endpoint from inside the resolver's not-found check. A sibling is seeded so
    the base dir exists — the kernel only reports ENAMETOOLONG once every
    leading component resolved (a missing base dir answers ENOENT instead)."""
    trash = _trash_dir(client)
    (trash / "Album").mkdir(parents=True)
    r = client.delete("/api/trash", params={"folder": "x" * 300})
    assert r.status_code == 404
    assert (trash / "Album").exists()  # the overlong name must not drag it down


def test_restore_overlong_folder_404_not_500(client: TestClient) -> None:
    trash = _trash_dir(client)
    (trash / "A").mkdir(parents=True)
    r = client.post("/api/trash/restore", json={"folder": "x" * 300})
    assert r.status_code == 404
    assert (trash / "A").exists()


def test_restore_503_when_the_music_share_is_unavailable(
    client: TestClient, tmp_path: Path
) -> None:
    """A move-back writes INTO the music library, so a dropped share is a 503 —
    the same answer delete gives — and not the blanket 500. The guard fires
    before anything leaves Trash, so the folder is still there afterwards."""
    trash = _trash_dir(client)
    entry = trash / "Dummy"
    entry.mkdir(parents=True)
    (entry / "cover.jpg").write_bytes(b"\x00")
    write_trash_origin(
        _origins_dir(client), entry.name, origin=str(tmp_path / "music" / "Dummy"), moved="folder"
    )
    shutil.rmtree(tmp_path / "music")

    r = client.post("/api/trash/restore", json={"folder": "Dummy"})

    assert r.status_code == 503
    assert (entry / "cover.jpg").exists()


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

    def spy(path: str, *, origins_dir: Path) -> EmptyResult:
        lock = getattr(app.state, "beets_swap_lock", None)
        seen["locked"] = lock is not None and lock.locked()
        return real_empty_one(path, origins_dir=origins_dir)

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

    def spy(trash_dir: Path, *, origins_dir: Path) -> EmptyResult:
        lock = getattr(app.state, "beets_swap_lock", None)
        seen["locked"] = lock is not None and lock.locked()
        return real_empty_all(trash_dir, origins_dir=origins_dir)

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
