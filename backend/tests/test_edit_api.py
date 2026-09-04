"""Route tests for POST /api/albums/{id}/edit(/preview)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from beets.library import Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.beets.library import _require_id
from app.main import app
from tests.conftest import beets_dir_for, make_test_handle


@pytest.fixture
def edit_client(edit_lib: Library, tmp_path: Path) -> Iterator[TestClient]:
    handle = make_test_handle(edit_lib, beets_dir_for(tmp_path))
    app.dependency_overrides[get_library] = lambda: handle
    app.state.beets_library = handle
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _album_id(lib: Library) -> int:
    return _require_id(next(iter(lib.albums())).id)


def test_preview_returns_diff(edit_client: TestClient, edit_lib: Library) -> None:
    aid = _album_id(edit_lib)
    r = edit_client.post(
        f"/api/albums/{aid}/edit/preview", json={"album": {"title": "In Rainbows (R)"}}
    )
    assert r.status_code == 200
    body = r.json()
    assert "title" in body["changed_fields"]
    assert body["album_after"]["title"] == "In Rainbows (R)"


def test_preview_returns_move_refusals(
    edit_client: TestClient, edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal partition reaches the wire: a collision-bound rename is absent
    from move_plan and present in move_refusals, with the reason attached."""
    import beets.ui

    monkeypatch.setattr(beets.ui, "should_move", lambda _opt: True)
    aid = _album_id(edit_lib)
    album = edit_lib.get_album(aid)
    assert album is not None
    items = sorted(album.items(), key=lambda i: int(i.track))
    mover = _require_id(items[2].id)

    # Track 3 edited into track 2's identity -> both resolve to one file name.
    r = edit_client.post(
        f"/api/albums/{aid}/edit/preview",
        json={"tracks": [{"item_id": mover, "title": "Bodysnatchers", "track": 2}]},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["move_enabled"] is True
    assert body["move_plan"] == []
    assert [row["item_id"] for row in body["move_refusals"]] == [mover]
    refusal = body["move_refusals"][0]
    assert refusal["new_path"].endswith("02 Bodysnatchers.flac")
    assert "02 Bodysnatchers.flac" in refusal["detail"]


def test_edit_applies_and_returns_album(edit_client: TestClient, edit_lib: Library) -> None:
    aid = _album_id(edit_lib)
    r = edit_client.post(f"/api/albums/{aid}/edit", json={"album": {"title": "In Rainbows (R)"}})
    assert r.status_code == 200
    body = r.json()
    assert body["album"]["title"] == "In Rainbows (R)"
    assert body["write_failures"] == 0


def test_edit_unknown_album_404(edit_client: TestClient) -> None:
    r = edit_client.post("/api/albums/999999/edit", json={"album": {"title": "x"}})
    assert r.status_code == 404


def test_edit_foreign_track_422(edit_client: TestClient, edit_lib: Library) -> None:
    aid = _album_id(edit_lib)
    r = edit_client.post(
        f"/api/albums/{aid}/edit", json={"tracks": [{"item_id": 424242, "title": "x"}]}
    )
    assert r.status_code == 422


def test_edit_bad_year_type_422(edit_client: TestClient, edit_lib: Library) -> None:
    aid = _album_id(edit_lib)
    r = edit_client.post(f"/api/albums/{aid}/edit", json={"album": {"year": "abc"}})
    assert r.status_code == 422


def test_edit_foreign_track_no_fields_422(edit_client: TestClient, edit_lib: Library) -> None:
    """A foreign item_id with no field edits must still 422, not 200."""
    aid = _album_id(edit_lib)
    r = edit_client.post(f"/api/albums/{aid}/edit", json={"tracks": [{"item_id": 424242}]})
    assert r.status_code == 422


def test_edit_year_out_of_range_422(edit_client: TestClient, edit_lib: Library) -> None:
    aid = _album_id(edit_lib)
    r = edit_client.post(f"/api/albums/{aid}/edit", json={"album": {"year": 10000}})
    assert r.status_code == 422


def test_edit_409_while_import_active(
    edit_client: TestClient, edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.import_jobs.registry import get_registry

    monkeypatch.setattr(get_registry(), "has_active_job", lambda: True)
    aid = _album_id(edit_lib)
    r = edit_client.post(f"/api/albums/{aid}/edit", json={"album": {"title": "x"}})
    assert r.status_code == 409
    assert "in progress" in r.json()["detail"].lower()
