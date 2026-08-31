# backend/tests/test_reorganize_api.py
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from beets.library import Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.main import app
from app.models.reorganize import ReorganizeOutcome, ReorganizePhase
from app.reorganize_jobs.registry import ReorganizeRegistry, get_reorganize_backfill
from tests.conftest import make_test_handle


@pytest.fixture
def reorg_client(reorganize_lib: Library, tmp_path: Path) -> Iterator[TestClient]:
    handle = make_test_handle(reorganize_lib, tmp_path)
    app.dependency_overrides[get_library] = lambda: handle
    app.state.beets_library = handle
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_preview_library(reorg_client: TestClient) -> None:
    resp = reorg_client.get("/api/reorganize/preview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope_label"] == "library"
    assert body["total"] == 4
    assert body["will_move"] == 3
    assert body["already_in_place"] == 1


def test_preview_artist(reorg_client: TestClient) -> None:
    resp = reorg_client.get("/api/reorganize/preview", params={"artist": "Radiohead"})
    assert resp.status_code == 200
    assert resp.json()["scope_label"] == "Radiohead"
    assert resp.json()["will_move"] == 1


def test_preview_album_404(reorg_client: TestClient) -> None:
    assert reorg_client.get("/api/albums/999999/reorganize/preview").status_code == 404


def _fake_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the daemon-thread spawn with a synchronous no-op sweep.

    The real ``start_backfill`` launches a background thread that calls into beets
    (reading config like ``timeout``); if it is still running when the autouse
    reset fixtures tear the beets globals down it raises a stray
    ``confuse.NotFoundError``. Driving the registry to ``done`` synchronously
    (the same pattern as ``test_lyrics_api``) keeps these tests deterministic.
    """
    import app.api.reorganize as reorganize_api

    def fake_start_backfill(reg: object, handle: object, **kwargs: object) -> None:
        reg.set_total(0)  # type: ignore[attr-defined]  # fake reg is the real registry
        reg.finish("done")  # type: ignore[attr-defined]

    monkeypatch.setattr(reorganize_api, "start_backfill", fake_start_backfill)


def test_start_passes_the_overridden_playlists_dir_to_the_worker(
    reorg_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route must resolve get_playlists_dir via Depends, never a direct
    call — a direct call escapes app.dependency_overrides, handing the worker
    the settings-derived REAL playlist store while every test override
    silently misses it."""
    import app.api.reorganize as reorganize_api
    from app.playlists.store import get_playlists_dir

    sentinel = tmp_path / "override-playlists"
    app.dependency_overrides[get_playlists_dir] = lambda: sentinel

    received: list[object] = []

    def fake_start_backfill(reg: object, handle: object, **kwargs: object) -> None:
        received.append(kwargs["playlists_dir"])
        reg.set_total(0)  # type: ignore[attr-defined]  # fake reg is the real registry
        reg.finish("done")  # type: ignore[attr-defined]

    monkeypatch.setattr(reorganize_api, "start_backfill", fake_start_backfill)
    assert reorg_client.post("/api/reorganize").status_code == 200
    assert received == [sentinel]


def test_start_passes_the_trash_origin_store_to_the_worker(
    reorg_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orphan sweep's husks are the rows the origin record exists for.

    An audio-free art/booklet folder cannot be imported at all, so an exact
    restore is its ONLY exit from Trash. The sweep is the one mover on a
    background thread, wired through four hops, and it is the easiest place for
    the second dir to be dropped: without it the pass is skipped entirely (both
    or neither, by design), so a route that forgets it stops collecting husks
    rather than collecting them unrestorably.
    """
    import app.api.reorganize as reorganize_api

    received: dict[str, object] = {}

    def fake_start_backfill(reg: object, handle: object, **kwargs: object) -> None:
        received.update(kwargs)
        reg.set_total(0)  # type: ignore[attr-defined]  # fake reg is the real registry
        reg.finish("done")  # type: ignore[attr-defined]

    monkeypatch.setattr(reorganize_api, "start_backfill", fake_start_backfill)
    assert reorg_client.post("/api/reorganize").status_code == 200

    trash = received["trash_dir"]
    origins = received["trash_origins_dir"]
    assert isinstance(trash, Path)
    assert isinstance(origins, Path)
    assert origins != trash
    assert not origins.is_relative_to(trash), "the store must be a sibling, not a child"
    # ...and the sweep must be told not to trash the store it is writing into.
    assert origins in received["ignore_dirs"]  # type: ignore[operator]  # tuple of Path


def test_status_idle_then_start(reorg_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_sweep(monkeypatch)
    assert reorg_client.get("/api/reorganize/status").json()["phase"] == "idle"
    started = reorg_client.post("/api/reorganize")
    assert started.status_code == 200
    assert started.json()["phase"] in ("running", "done")
    assert started.json()["scope_label"] == "library"


def test_start_album_scope_sets_album_id(
    reorg_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_sweep(monkeypatch)
    album_id = reorg_client.get("/api/albums", params={"limit": 50}).json()["items"][0]["id"]
    resp = reorg_client.post(f"/api/albums/{album_id}/reorganize")
    assert resp.status_code == 200
    assert resp.json()["album_id"] == album_id


def test_stop_returns_status(reorg_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_sweep(monkeypatch)
    reorg_client.post("/api/reorganize")
    resp = reorg_client.post("/api/reorganize/stop")
    assert resp.status_code == 200
    assert resp.json()["phase"] in ("running", "stopped", "done")


def test_preview_lists_orphan_husks(reorg_client: TestClient, reorganize_lib: Library) -> None:
    import os
    from pathlib import Path

    music_dir = Path(os.fsdecode(reorganize_lib.directory))
    husk = music_dir / "Ghost Husk"
    husk.mkdir(parents=True, exist_ok=True)
    (husk / "cover.jpg").write_bytes(b"x")

    r = reorg_client.get("/api/reorganize/preview")
    assert r.status_code == 200
    assert "Ghost Husk" in [o["name"] for o in r.json()["orphans"]]


def test_status_includes_orphans_trashed(reorg_client: TestClient) -> None:
    r = reorg_client.get("/api/reorganize/status")
    assert r.status_code == 200
    assert "orphans_trashed" in r.json()


def test_status_includes_failures(reorg_client: TestClient) -> None:
    r = reorg_client.get("/api/reorganize/status")
    assert r.status_code == 200
    assert r.json()["failures"] == []


def _seed_terminal_job(phase: ReorganizePhase = "failed") -> ReorganizeRegistry:
    """Drive the LIVE registry to a terminal phase carrying one failure row.

    The registry is a process-global single slot (reset around every test by the
    autouse conftest fixture), so the routes' ``Depends(get_reorganize_backfill)``
    hands back this very instance.
    """
    reg = get_reorganize_backfill()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    reg.record(ReorganizeOutcome(status="failed", label="A — B", error="boom"))
    if phase == "failed":
        reg.fail("boom")
    else:
        reg.finish(phase)
    return reg


def test_dismiss_clears_a_terminal_jobs_failures(reorg_client: TestClient) -> None:
    _seed_terminal_job()
    assert reorg_client.get("/api/reorganize/status").json()["failures"] != []

    resp = reorg_client.post("/api/reorganize/dismiss")
    assert resp.status_code == 200
    assert resp.json()["phase"] == "idle"
    assert resp.json()["failures"] == []

    after = reorg_client.get("/api/reorganize/status").json()
    assert after["phase"] == "idle"
    assert after["failures"] == []


def test_dismiss_while_running_is_refused_and_the_job_survives(reorg_client: TestClient) -> None:
    reg = get_reorganize_backfill()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    reg.record(ReorganizeOutcome(status="failed", label="A — B", error="boom"))

    resp = reorg_client.post("/api/reorganize/dismiss")
    assert resp.status_code == 409

    after = reorg_client.get("/api/reorganize/status").json()
    assert after["phase"] == "running"
    assert len(after["failures"]) == 1


def test_dismiss_with_no_job_is_idempotent(reorg_client: TestClient) -> None:
    first = reorg_client.post("/api/reorganize/dismiss")
    assert first.status_code == 200
    assert first.json()["phase"] == "idle"
    second = reorg_client.post("/api/reorganize/dismiss")
    assert second.status_code == 200
    assert second.json()["phase"] == "idle"


def test_dismiss_is_not_gated_by_another_library_job(reorg_client: TestClient) -> None:
    # Dismiss touches the in-memory registry only — never the library — so the
    # busy gate that guards start must NOT hold it hostage.
    from app.lyrics_jobs.registry import get_lyrics_backfill

    _seed_terminal_job()
    lyrics = get_lyrics_backfill()
    lyrics.start(writes_enabled=False)
    try:
        resp = reorg_client.post("/api/reorganize/dismiss")
    finally:
        lyrics.finish("done")
    assert resp.status_code == 200
    assert resp.json()["phase"] == "idle"


def test_reading_status_and_preview_never_clears_the_failure(reorg_client: TestClient) -> None:
    # Clearing is an explicit user action: a clean preview is evidence, not consent.
    _seed_terminal_job()
    assert reorg_client.get("/api/reorganize/preview").status_code == 200
    first = reorg_client.get("/api/reorganize/status").json()
    second = reorg_client.get("/api/reorganize/status").json()
    assert first["failures"] == second["failures"] != []
    assert second["phase"] == "failed"


@pytest.mark.parametrize("phase", ["done", "stopped", "failed"])
def test_status_carries_finished_at_for_a_terminal_job(
    reorg_client: TestClient, phase: ReorganizePhase
) -> None:
    before = datetime.now(tz=None).astimezone()
    _seed_terminal_job(phase)
    body = reorg_client.get("/api/reorganize/status").json()
    assert body["phase"] == phase
    stamped = datetime.fromisoformat(body["finished_at"])
    assert stamped.utcoffset() == timedelta(0)  # aware AND UTC on the wire
    assert before <= stamped <= datetime.now(tz=None).astimezone()


def test_idle_status_invents_no_finished_at(reorg_client: TestClient) -> None:
    body = reorg_client.get("/api/reorganize/status").json()
    assert body["phase"] == "idle"
    assert body["finished_at"] is None


def test_running_status_invents_no_finished_at(reorg_client: TestClient) -> None:
    get_reorganize_backfill().start(
        scope="library", artist=None, album_id=None, scope_label="library"
    )
    body = reorg_client.get("/api/reorganize/status").json()
    assert body["phase"] == "running"
    assert body["finished_at"] is None


def test_preview_does_not_list_playlists_export_dir(
    reorg_client: TestClient, reorganize_lib: Library
) -> None:
    import os
    from pathlib import Path

    music_dir = Path(os.fsdecode(reorganize_lib.directory))
    (music_dir / ".playlists").mkdir(parents=True, exist_ok=True)
    (music_dir / ".playlists" / "p1.m3u8").write_text("#EXTM3U\n")

    r = reorg_client.get("/api/reorganize/preview")
    assert r.status_code == 200
    assert ".playlists" not in [o["name"] for o in r.json()["orphans"]]
