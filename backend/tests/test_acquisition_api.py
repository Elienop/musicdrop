"""GET /api/acquisition/status — informational queue probe + lifespan wiring.

The status endpoint is wired to ``app.state.acquisition_queue``, which the app
lifespan constructs/starts after attaching the library and stops before closing
it. Under the lifespan-less ``client`` fixture the queue is absent, so the
endpoint must fall back to an idle status rather than 500 (it is polled often).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

if TYPE_CHECKING:
    from beets.library import Library


def _write_config(tmp_path: Path) -> Path:
    """Write a hermetic beets config and return the BEETSDIR it lives in.

    A SIBLING of the music dir, never its parent: ``app.beets.store_layout``
    refuses a beets data directory that contains the music library, and the real
    lifespan these tests boot runs that check.
    """
    music = tmp_path / "music"
    music.mkdir()
    beets = tmp_path / "beets"
    beets.mkdir()
    (beets / "config.yaml").write_text(
        f"directory: {music}\nlibrary: library.db\nplugins:\n  - musicbrainz\n"
    )
    return beets


def test_acquisition_status_idle_via_lifespan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    app.dependency_overrides.clear()
    with TestClient(app) as client:  # context-manager form runs the lifespan
        assert getattr(app.state, "acquisition_queue", None) is not None
        assert getattr(app.state, "inbox_dir", None) == (tmp_path / "beets").resolve() / "inbox"
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
        "inbox_pending": 0,
    }


def test_lifespan_waits_for_in_flight_import_before_closing_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shutdown stops the inbox drain but cannot join the beets worker thread, so
    # an import still owning the single slot must be given a bounded moment to
    # release it BEFORE close_library tears down the SQLite connection. Drive a
    # real lifespan, report the slot busy for two teardown polls, then free it,
    # and assert close_library only ran after the slot reported idle.
    import app.import_jobs.registry as reg_mod
    from app.beets.library import close_library as real_close

    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    app.dependency_overrides.clear()

    poll_count = 0
    closed_at_poll: int | None = None

    def fake_has_active_job() -> bool:
        nonlocal poll_count
        poll_count += 1
        return poll_count <= 2  # busy for the first two teardown polls

    def recording_close(lib: Library) -> None:
        nonlocal closed_at_poll
        closed_at_poll = poll_count
        real_close(lib)

    # Patch the live global registry instance the lifespan reads at runtime; the
    # parked (un-fed) drain never calls has_active_job, so the teardown loop is
    # the only caller and the poll counter is deterministic.
    monkeypatch.setattr(reg_mod.registry, "has_active_job", fake_has_active_job)
    monkeypatch.setattr("app.main.close_library", recording_close)

    with TestClient(app):  # context-manager form runs (and tears down) the lifespan
        pass

    # The slot reported busy twice then idle on the third poll; close_library
    # must have waited for that idle poll rather than racing the worker.
    assert poll_count >= 3
    assert closed_at_poll == poll_count


def test_acquisition_status_lazy_fallback_without_lifespan(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The lifespan-less `client` fixture sets no acquisition_queue on app.state;
    # remove any stale one a prior lifespan test left so the fallback is exercised.
    if getattr(app.state, "acquisition_queue", None) is not None:
        monkeypatch.delattr(app.state, "acquisition_queue")
    resp = client.get("/api/acquisition/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["phase"] == "idle"
    assert body["queued"] == 0
    assert body["current"] is None
