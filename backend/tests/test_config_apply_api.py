"""End-to-end tests for ``POST /api/config/apply``.

Covers the one-click in-process reload: the handler holds an ``asyncio.Lock``
on ``app.state.beets_swap_lock`` and offloads the blocking rebuild
(``reset_beets_globals`` + ``setup_beets``) to FastAPI's threadpool, then
atomically swaps ``app.state.beets_library`` with the new handle. The 409
import-gate check uses ``get_registry()`` (the live binding — the autouse
``reset_import_registry`` fixture in ``conftest.py`` swaps the module global
between tests, so the handler must NOT import the name eagerly).

The 500 branch monkeypatches ``app.beets.config_editor.setup_beets`` (the name
the handler captured at import time) — patching the source module would not
affect the already-bound symbol.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def test_apply_returns_snapshot_with_apply_pending_false(
    client: TestClient, beets_library_config_path: Path
) -> None:
    # Touch the file so apply_pending becomes true; the post-Apply snapshot
    # must report apply_pending=False because setup_beets() captured the
    # bumped mtime as the new baseline.
    time.sleep(0.01)
    new_ts = time.time() + 1
    os.utime(beets_library_config_path, (new_ts, new_ts))
    snap_before = client.get("/api/config").json()
    assert snap_before["apply_pending"] is True

    r = client.post("/api/config/apply")
    assert r.status_code == 200
    assert r.json()["apply_pending"] is False


def test_apply_409_when_import_active(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.import_jobs.registry import get_registry

    monkeypatch.setattr(get_registry(), "has_active_job", lambda: True)
    r = client.post("/api/config/apply")
    assert r.status_code == 409
    assert "import" in r.json()["detail"].lower()


def test_apply_500_when_setup_beets_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.beets import config_editor

    def _boom(_dir: str) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(config_editor, "setup_beets", _boom)
    r = client.post("/api/config/apply")
    assert r.status_code == 500
    detail = r.json()["detail"]
    # detail is the dict we set in the handler; recovery hint must be present
    # so the operator knows the on-disk save is still good after a restart.
    assert "recovery" in str(detail).lower() or "restart" in str(detail).lower()
