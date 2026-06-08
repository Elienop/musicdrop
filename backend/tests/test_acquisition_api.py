"""GET /api/acquisition/status — informational queue probe + lifespan wiring.

The status endpoint is wired to ``app.state.acquisition_queue``, which the app
lifespan constructs/starts after attaching the library and stops before closing
it. Under the lifespan-less ``client`` fixture the queue is absent, so the
endpoint must fall back to an idle status rather than 500 (it is polled often).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def _write_config(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (tmp_path / "config.yaml").write_text(
        f"directory: {music}\nlibrary: library.db\nplugins:\n  - musicbrainz\n"
    )


def test_acquisition_status_idle_via_lifespan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path))
    app.dependency_overrides.clear()
    with TestClient(app) as client:  # context-manager form runs the lifespan
        assert getattr(app.state, "acquisition_queue", None) is not None
        assert getattr(app.state, "inbox_dir", None) == tmp_path.resolve() / "inbox"
        resp = client.get("/api/acquisition/status")
    assert resp.status_code == 200
    assert resp.json() == {
        "phase": "idle",
        "queued": 0,
        "current": None,
        "processed": 0,
        "set_aside": 0,
        "failed": 0,
        "error": None,
    }


def test_acquisition_status_lazy_fallback_without_lifespan(client: TestClient) -> None:
    # The lifespan-less `client` fixture sets no acquisition_queue on app.state;
    # remove any stale one a prior lifespan test left so the fallback is exercised.
    prior = getattr(app.state, "acquisition_queue", None)
    if prior is not None:
        del app.state.acquisition_queue
    try:
        resp = client.get("/api/acquisition/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["phase"] == "idle"
        assert body["queued"] == 0
        assert body["current"] is None
    finally:
        if prior is not None:
            app.state.acquisition_queue = prior
