# backend/tests/test_reorganize_mutex.py
from collections.abc import Iterator
from pathlib import Path

import pytest
from beets.library import Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.main import app
from app.reorganize_jobs.registry import get_reorganize_backfill
from tests.conftest import make_test_handle


@pytest.fixture
def reorg_client(reorganize_lib: Library, tmp_path: Path) -> Iterator[TestClient]:
    handle = make_test_handle(reorganize_lib, tmp_path)
    app.dependency_overrides[get_library] = lambda: handle
    app.state.beets_library = handle
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_lyrics_backfill_409_while_reorganize_runs(reorg_client: TestClient) -> None:
    # Pin a reorganize job in 'running' so the slot is occupied.
    get_reorganize_backfill().start(artist=None, album_id=None, scope_label="library")
    resp = reorg_client.post("/api/lyrics/backfill")
    assert resp.status_code == 409


def test_reorganize_409_while_lyrics_backfill_runs(reorg_client: TestClient) -> None:
    from app.lyrics_jobs.registry import get_lyrics_backfill

    get_lyrics_backfill().start(writes_enabled=False)
    resp = reorg_client.post("/api/reorganize")
    assert resp.status_code == 409


def test_reorganize_409_while_artist_art_runs(reorg_client: TestClient) -> None:
    from app.artist_art_jobs.registry import get_artist_art_backfill

    get_artist_art_backfill().start(force=False)
    assert reorg_client.post("/api/reorganize").status_code == 409
