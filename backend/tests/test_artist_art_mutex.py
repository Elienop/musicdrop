from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from beets.library import Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.artist_art_jobs.registry import reset_artist_art_backfill
from app.beets.library import _require_id
from app.main import app
from tests.conftest import beets_dir_for, make_test_handle

PNG = Path(__file__).parent / "fixtures" / "cover.png"


@pytest.fixture
def cover_client(edit_lib: Library, tmp_path: Path) -> Iterator[TestClient]:
    handle = make_test_handle(edit_lib, beets_dir_for(tmp_path))
    app.dependency_overrides[get_library] = lambda: handle
    app.state.beets_library = handle
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_cover_install_409_when_artist_art_running(
    cover_client: TestClient, edit_lib: Library
) -> None:
    # reuse the cover test fixtures; start an artist-art job, then cover must 409
    reg = reset_artist_art_backfill()
    reg.start(force=False)  # occupy the slot
    aid = _require_id(next(iter(edit_lib.albums())).id)
    r = cover_client.post(
        f"/api/albums/{aid}/cover",
        files={"file": ("c.png", PNG.read_bytes(), "image/png")},
    )
    assert r.status_code == 409


def test_lyrics_backfill_409_when_artist_art_running() -> None:
    # Symmetry: the lyrics backfill must also refuse while an artist-art job runs
    # (the gate is checked before any app.state read, so no lifespan needed).
    reg = reset_artist_art_backfill()
    reg.start(force=False)
    r = TestClient(app).post("/api/lyrics/backfill")
    assert r.status_code == 409
