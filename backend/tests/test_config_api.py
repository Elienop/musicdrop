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
        "mtime_ns",
        "sha256",
        "apply_pending",
    ):
        assert key in body
    assert isinstance(body["yaml_text"], str)
    assert body["yaml_text"]  # non-empty
    assert body["apply_pending"] is False
    assert isinstance(body["mtime_ns"], int)
    assert body["mtime_ns"] > 0
    assert isinstance(body["sha256"], str)
    assert len(body["sha256"]) == 64


def test_get_config_reflects_mtime_change(
    client: TestClient, beets_library_config_path: Path
) -> None:
    """Second GET after a touch should show apply_pending + new mtime,
    but yaml_text should be unchanged (we serve the in-memory snapshot)."""
    first = client.get("/api/config").json()
    time.sleep(0.01)  # ensure st_mtime advances by at least the FS granularity
    os.utime(
        beets_library_config_path,
        (time.time() + 5, time.time() + 5),
    )
    second = client.get("/api/config").json()
    assert second["apply_pending"] is True
    assert second["yaml_text"] == first["yaml_text"]
    assert second["file_modified_at"] != first["file_modified_at"]
    # mtime_ns refreshes; sha256 is unchanged because content is identical.
    assert second["mtime_ns"] != first["mtime_ns"]
    assert second["sha256"] == first["sha256"]
