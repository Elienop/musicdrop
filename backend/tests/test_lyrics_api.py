from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest
from beets.library import Library
from beets.util.lyrics import Lyrics
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


class _FakeBackend:
    def __init__(self, result: Lyrics | None) -> None:
        self._result = result

    def fetch(self, artist: str, title: str, album: str, length: int) -> Lyrics | None:
        return self._result


class _FakePlugin:
    backends: ClassVar[list[_FakeBackend]] = [_FakeBackend(Lyrics("api lyrics", "lrclib", "u"))]


def test_fetch_album_lyrics_endpoint(
    lyrics_client: TestClient, edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.beets import lyrics as lyrics_mod

    monkeypatch.setattr(lyrics_mod, "_make_lyrics_plugin", lambda: _FakePlugin())
    r = lyrics_client.post(f"/api/albums/{_aid(edit_lib)}/lyrics/fetch")
    assert r.status_code == 200
    body = r.json()
    assert body["fetched"] == 3
    assert body["writes_enabled"] is True
    assert len(body["items"]) == 3


def test_fetch_album_lyrics_unknown_album_404(lyrics_client: TestClient) -> None:
    r = lyrics_client.post("/api/albums/999999/lyrics/fetch")
    assert r.status_code == 404


def test_fetch_album_lyrics_409_while_import_active(
    lyrics_client: TestClient, edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.import_jobs.registry import get_registry

    monkeypatch.setattr(get_registry(), "has_active_job", lambda: True)
    r = lyrics_client.post(f"/api/albums/{_aid(edit_lib)}/lyrics/fetch")
    assert r.status_code == 409
    assert "in progress" in r.json()["detail"].lower()


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

    def fake_start_backfill(reg: object, lib: object, *, delay: float, write: bool) -> None:
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
