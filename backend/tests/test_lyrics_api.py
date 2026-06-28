from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from beets.library import Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.main import app
from tests.conftest import make_test_handle


@pytest.fixture
def lyrics_client(edit_lib: Library, tmp_path: Path) -> Iterator[TestClient]:
    handle = make_test_handle(edit_lib, tmp_path)
    app.dependency_overrides[get_library] = lambda: handle
    app.state.beets_library = handle
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _aid(lib: Library) -> int:
    return int(next(iter(lib.albums())).id)


def test_album_fetch_starts_scoped_job(
    lyrics_client: TestClient, edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.lyrics_jobs.runner as runner_mod
    from app.lyrics_jobs.registry import LyricsBackfillRegistry, reset_lyrics_backfill

    reset_lyrics_backfill()

    def fake_start_backfill(
        reg: LyricsBackfillRegistry,
        handle: object,
        *,
        delay: float,
        write: bool,
        album_id: int | None = None,
        recheck_misses: bool = False,
        on_complete: object | None = None,
    ) -> None:
        reg.set_total(0)
        reg.finish("done")

    monkeypatch.setattr(runner_mod, "start_backfill", fake_start_backfill)
    r = lyrics_client.post(f"/api/albums/{_aid(edit_lib)}/lyrics/fetch")
    assert r.status_code == 200
    body = r.json()
    assert body["album_id"] == _aid(edit_lib)
    assert body["scope_label"]  # non-empty "artist — album"
    assert body["phase"] in {"running", "done"}
    reset_lyrics_backfill()


def test_album_fetch_unknown_album_404(lyrics_client: TestClient) -> None:
    from app.lyrics_jobs.registry import reset_lyrics_backfill

    reset_lyrics_backfill()
    r = lyrics_client.post("/api/albums/999999/lyrics/fetch")
    assert r.status_code == 404


def test_album_fetch_409_while_import_active(
    lyrics_client: TestClient, edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.import_jobs.registry import get_registry
    from app.lyrics_jobs.registry import reset_lyrics_backfill

    reset_lyrics_backfill()
    monkeypatch.setattr(get_registry(), "has_active_job", lambda: True)
    r = lyrics_client.post(f"/api/albums/{_aid(edit_lib)}/lyrics/fetch")
    assert r.status_code == 409
    reset_lyrics_backfill()


def test_coverage_endpoint(lyrics_client: TestClient, edit_lib: Library) -> None:
    r = lyrics_client.get("/api/lyrics/coverage")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert "percent" in body


def test_backfill_start_status_stop(
    lyrics_client: TestClient, edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.lyrics_jobs.registry import reset_lyrics_backfill

    reset_lyrics_backfill()

    # Make the worker thread deterministic: a no-op sweep that just finishes.
    import app.api.lyrics as lyrics_api

    def fake_start_backfill(
        reg: object,
        handle: object,
        *,
        delay: float,
        write: bool,
        recheck_misses: bool = False,
        on_complete: object | None = None,
    ) -> None:
        reg.set_total(0)  # type: ignore[attr-defined]  # fake reg is the real registry
        reg.finish("done")  # type: ignore[attr-defined]

    monkeypatch.setattr(lyrics_api, "start_backfill", fake_start_backfill)

    start = lyrics_client.post("/api/lyrics/backfill")
    assert start.status_code == 200
    assert start.json()["phase"] in {"running", "done"}

    status = lyrics_client.get("/api/lyrics/backfill")
    assert status.status_code == 200

    stop = lyrics_client.post("/api/lyrics/backfill/stop")
    assert stop.status_code == 200
    reset_lyrics_backfill()


def test_backfill_409_when_import_active(
    lyrics_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.import_jobs.registry import get_registry
    from app.lyrics_jobs.registry import reset_lyrics_backfill

    reset_lyrics_backfill()
    monkeypatch.setattr(get_registry(), "has_active_job", lambda: True)
    r = lyrics_client.post("/api/lyrics/backfill")
    assert r.status_code == 409
    reset_lyrics_backfill()


def test_library_backfill_passes_recheck_misses(
    lyrics_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.api.lyrics as lyrics_api
    from app.lyrics_jobs.registry import reset_lyrics_backfill

    reset_lyrics_backfill()
    seen: dict[str, bool] = {}

    def fake_start_backfill(
        reg: object,
        handle: object,
        *,
        delay: float,
        write: bool,
        recheck_misses: bool = False,
        on_complete: object | None = None,
    ) -> None:
        seen["recheck_misses"] = recheck_misses
        reg.set_total(0)  # type: ignore[attr-defined]
        reg.finish("done")  # type: ignore[attr-defined]

    monkeypatch.setattr(lyrics_api, "start_backfill", fake_start_backfill)
    r = lyrics_client.post("/api/lyrics/backfill?recheck_misses=true")
    assert r.status_code == 200
    assert seen["recheck_misses"] is True
    reset_lyrics_backfill()


def test_album_fetch_forces_recheck_misses(
    lyrics_client: TestClient, edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.lyrics_jobs.runner as runner_mod
    from app.lyrics_jobs.registry import LyricsBackfillRegistry, reset_lyrics_backfill

    reset_lyrics_backfill()
    seen: dict[str, bool] = {}

    def fake_start_backfill(
        reg: LyricsBackfillRegistry,
        handle: object,
        *,
        delay: float,
        write: bool,
        album_id: int | None = None,
        recheck_misses: bool = False,
        on_complete: object | None = None,
    ) -> None:
        seen["recheck_misses"] = recheck_misses
        reg.set_total(0)
        reg.finish("done")

    monkeypatch.setattr(runner_mod, "start_backfill", fake_start_backfill)
    r = lyrics_client.post(f"/api/albums/{_aid(edit_lib)}/lyrics/fetch")
    assert r.status_code == 200
    assert seen["recheck_misses"] is True
    reset_lyrics_backfill()


def test_album_fetch_threads_on_complete(
    lyrics_client: TestClient, edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-album fetch must thread a non-None on_complete callback through to
    start_backfill so other tabs are notified when the scoped fetch finishes."""
    import app.lyrics_jobs.runner as runner_mod
    from app.lyrics_jobs.registry import LyricsBackfillRegistry, reset_lyrics_backfill

    reset_lyrics_backfill()
    captured: dict[str, object] = {}

    def fake_start_backfill(
        reg: LyricsBackfillRegistry,
        handle: object,
        *,
        delay: float,
        write: bool,
        album_id: int | None = None,
        recheck_misses: bool = False,
        on_complete: object | None = None,
    ) -> None:
        captured["on_complete"] = on_complete
        reg.set_total(0)
        reg.finish("done")

    monkeypatch.setattr(runner_mod, "start_backfill", fake_start_backfill)
    r = lyrics_client.post(f"/api/albums/{_aid(edit_lib)}/lyrics/fetch")
    assert r.status_code == 200
    on_complete = captured["on_complete"]
    assert on_complete is not None
    assert callable(on_complete)
    reset_lyrics_backfill()
