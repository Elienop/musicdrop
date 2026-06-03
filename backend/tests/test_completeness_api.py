from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from beets.library import Item, Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.main import app
from tests.conftest import make_test_handle


def _make_lib(tmp_path: Path, *, mb_albumid: str, trackids: list[str]) -> Library:
    music = tmp_path / "music"
    base = music / "Radiohead" / "In Rainbows"
    base.mkdir(parents=True, exist_ok=True)
    lib = Library(
        str(tmp_path / "library.db"),
        directory=str(music),
        path_formats=[("default", "$albumartist/$album/$track $title")],
    )
    items = []
    for i, tid in enumerate(trackids, start=1):
        f = base / f"{i:02d} Track {i}.mp3"
        f.write_bytes(b"\x00")
        it = Item(
            album="In Rainbows",
            albumartist="Radiohead",
            artist="Radiohead",
            title=f"Track {i}",
            track=i,
            disc=1,
        )
        it.path = os.fsencode(str(f))
        if tid:
            it.mb_trackid = tid
        items.append(it)
    al = lib.add_album(items)
    if mb_albumid:
        al["mb_albumid"] = mb_albumid
    al.store()
    return lib


@pytest.fixture
def missing_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from app.beets import completeness as comp
    from app.beets.completeness import clear_release_cache

    clear_release_cache()
    lib = _make_lib(tmp_path, mb_albumid="rel-1", trackids=["t1", "t2"])

    release = SimpleNamespace(
        tracks=[
            SimpleNamespace(
                track_id=f"t{i}", index=i, medium=1, title=f"Track {i}", length=float(180 + i)
            )
            for i in range(1, 5)
        ]
    )

    class _Source:
        def album_for_id(self, mbid: str) -> Any:
            return release

    monkeypatch.setattr(
        comp.metadata_plugins,  # type: ignore[attr-defined]  # re-exported beets symbol
        "get_metadata_source",
        lambda name: _Source(),
    )

    handle = make_test_handle(lib, tmp_path)
    app.dependency_overrides[get_library] = lambda: handle
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _aid(client: TestClient) -> int:
    # The seeded album is the only one; its id is 1 in a fresh hermetic db, but
    # resolve via the list endpoint to stay robust.
    items = client.get("/api/albums").json()["items"]
    return int(items[0]["id"])


def test_missing_endpoint_returns_report(missing_client: TestClient) -> None:
    aid = _aid(missing_client)
    r = missing_client.get(f"/api/albums/{aid}/missing")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["total"] == 4
    assert body["present_count"] == 2
    assert [m["mb_trackid"] for m in body["missing"]] == ["t3", "t4"]


def test_missing_endpoint_unknown_album_404(missing_client: TestClient) -> None:
    r = missing_client.get("/api/albums/999999/missing")
    assert r.status_code == 404
