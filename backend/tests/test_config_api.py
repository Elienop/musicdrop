"""Tests for the read-only Config view endpoint (``GET /api/config``).

The router reads ``request.app.state.beets_library`` directly (no FastAPI
dependency to override), so the ``client`` fixture in ``conftest.py`` wires
``app.state.beets_library`` to a real ``setup_beets()`` handle before yielding
the TestClient. Using a real handle (not ``make_test_handle``) is required
because ``build_config_snapshot`` calls ``handle.config_path.stat()`` — the
sentinel placeholder would raise.
"""

import os
import time
from pathlib import Path

from fastapi.testclient import TestClient


def test_get_config_returns_snapshot(client: TestClient) -> None:
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    for key in (
        "yaml_text",
        "config_path",
        "loaded_at",
        "file_modified_at",
        "restart_required",
    ):
        assert key in body
    assert isinstance(body["yaml_text"], str)
    assert body["yaml_text"]  # non-empty
    assert body["restart_required"] is False


def test_get_config_reflects_mtime_change(
    client: TestClient, beets_library_config_path: Path
) -> None:
    """Second GET after a touch should show restart_required + new mtime,
    but yaml_text should be unchanged (we serve the in-memory snapshot)."""
    first = client.get("/api/config").json()
    time.sleep(0.01)  # ensure st_mtime advances by at least the FS granularity
    os.utime(
        beets_library_config_path,
        (time.time() + 5, time.time() + 5),
    )
    second = client.get("/api/config").json()
    assert second["restart_required"] is True
    assert second["yaml_text"] == first["yaml_text"]
    assert second["file_modified_at"] != first["file_modified_at"]
