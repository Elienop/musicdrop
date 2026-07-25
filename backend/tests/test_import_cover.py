import time

import pytest
from fastapi.testclient import TestClient

from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import reset_registry
from app.main import app
from app.models.import_models import (
    AlbumChange,
    Candidate,
    ParkedAlbum,
    Recommendation,
)


def _candidate() -> Candidate:
    album = AlbumChange(
        artist="Radiohead", album="OK Computer", year=1997, label=None, country=None, media=None
    )
    return Candidate(
        confidence=75.5,
        recommendation=Recommendation.medium,
        data_source="MusicBrainz",
        data_url="https://mb/a1",
        cover_after_url="https://coverartarchive.org/release/a1/front-500",
        has_current_art=True,
        changed_fields=["album"],
        album_before=album,
        album_after=album,
        tracks=[],
        missing=[],
        unmatched=[],
        options=[],
    )


def _parked(index: int) -> ParkedAlbum:
    return ParkedAlbum(
        album_index=index, folder=f"/music/incoming/album{index}", candidate=_candidate()
    )


def _client_with_fake(
    parked: list[ParkedAlbum] | None = None,
    art_sources: dict[int, str] | None = None,
) -> TestClient:
    reset_registry(runner=FakeImportRunner(parked=parked, art_sources=art_sources))
    return TestClient(app)


def _poll(client: TestClient, job_id: str, predicate, attempts: int = 200):  # type: ignore[no-untyped-def]  # test-local poll: predicate is an inline lambda
    for _ in range(attempts):
        state = client.get(f"/api/import/{job_id}").json()
        if predicate(state):
            return state
        time.sleep(0.01)
    return client.get(f"/api/import/{job_id}").json()


def test_cover_404_when_no_parked_album() -> None:
    # A parked album at index 0, but request a different (unparked) index.
    client = _client_with_fake(parked=[_parked(0)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll(client, job_id, lambda s: len(s["albums"]) == 1)
    r = client.get(f"/api/import/{job_id}/albums/99/cover")
    assert r.status_code == 404


def test_cover_404_when_parked_album_has_no_art_source() -> None:
    # Parked at 0 but no art_source recorded -> 404 (no current art).
    client = _client_with_fake(parked=[_parked(0)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll(client, job_id, lambda s: len(s["albums"]) == 1)
    r = client.get(f"/api/import/{job_id}/albums/0/cover")
    assert r.status_code == 404


def test_cover_404_for_unknown_job() -> None:
    reset_registry(runner=FakeImportRunner())
    client = TestClient(app)
    r = client.get("/api/import/nope/albums/0/cover")
    assert r.status_code == 404


def test_cover_streams_embedded_art(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.import_jobs.registry.embedded_art",
        lambda p: (b"PNGDATA", "image/png"),
    )
    client = _client_with_fake(parked=[_parked(0)], art_sources={0: "/fake/album0/track.flac"})
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll(client, job_id, lambda s: len(s["albums"]) == 1)
    r = client.get(f"/api/import/{job_id}/albums/0/cover")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert r.content == b"PNGDATA"


def test_cover_offloads_the_tag_read_to_the_threadpool(monkeypatch: pytest.MonkeyPatch) -> None:
    # candidate_cover parses the parked album's first audio file for embedded art
    # (HDD/NAS source) — it must be offloaded, not run on the event loop.
    from unittest.mock import Mock

    from fastapi.concurrency import run_in_threadpool as _real

    import app.api.import_ as import_mod

    monkeypatch.setattr(
        "app.import_jobs.registry.embedded_art",
        lambda p: (b"PNGDATA", "image/png"),
    )
    spy = Mock(side_effect=lambda fn, *a, **k: _real(fn, *a, **k))
    monkeypatch.setattr(import_mod, "run_in_threadpool", spy, raising=False)
    client = _client_with_fake(parked=[_parked(0)], art_sources={0: "/fake/album0/track.flac"})
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll(client, job_id, lambda s: len(s["albums"]) == 1)
    r = client.get(f"/api/import/{job_id}/albums/0/cover")
    assert r.status_code == 200
    assert "candidate_cover" in [getattr(c.args[0], "__name__", "") for c in spy.call_args_list]
