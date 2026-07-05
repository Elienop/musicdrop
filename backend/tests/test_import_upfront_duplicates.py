"""GET /api/import/{job}/albums/{index}/duplicates — the live up-front check."""

import threading

from beets.library import Item, Library
from fastapi.testclient import TestClient

from app.beets.library import LibraryHandle
from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import reset_registry
from app.models.import_models import (
    AlbumChange,
    Candidate,
    CandidateOption,
    ParkedAlbum,
    Recommendation,
)


def _candidate(*, artist: str, album: str, year: int, release_id: str | None) -> Candidate:
    change = AlbumChange(
        artist=artist, album=album, year=year, label=None, country=None, media=None
    )
    return Candidate(
        confidence=80.0,
        recommendation=Recommendation.medium,
        data_source="MusicBrainz",
        data_url=None,
        cover_after_url=None,
        has_current_art=False,
        changed_fields=[],
        album_before=change,
        album_after=change,
        tracks=[],
        missing=[],
        unmatched=[],
        options=[
            CandidateOption(
                index=0,
                confidence=80.0,
                data_source="MusicBrainz",
                disambiguation=None,
                release_id=release_id,
                album_artist=artist,
                album=album,
                year=year,
            )
        ],
    )


def _parked(
    index: int,
    *,
    artist: str,
    album: str,
    year: int = 1995,
    release_id: str | None = None,
    folder: str = "/inbox/x",
) -> ParkedAlbum:
    return ParkedAlbum(
        album_index=index,
        folder=folder,
        candidate=_candidate(artist=artist, album=album, year=year, release_id=release_id),
    )


def _add_album(lib: Library, *, artist: str, album: str, mb: str | None, n: int) -> int:
    items = [
        Item(album=album, albumartist=artist, title=f"t{i}", track=i + 1, mb_albumid=mb or "")
        for i in range(n)
    ]
    al = lib.add_album(items)  # adds items too
    al.store()
    return int(al.id)


def _start_and_wait(client: TestClient, parked: list[ParkedAlbum]) -> str:
    reset_registry(runner=FakeImportRunner(parked=parked))
    job_id: str = client.post("/api/import", json={"path": "/inbox/x"}).json()["job_id"]
    index = parked[0].album_index
    for _ in range(200):
        if client.get(f"/api/import/{job_id}/albums/{index}").status_code == 200:
            return job_id
        threading.Event().wait(0.01)
    raise TimeoutError("candidate never parked")


def test_live_upfront_check_flags_a_collision(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    _add_album(beets_library.lib, artist="2Pac", album="Me Against the World", mb="mb-1", n=15)
    job_id = _start_and_wait(
        client, [_parked(0, artist="2Pac", album="Me Against the World", release_id="mb-1")]
    )
    r = client.get(f"/api/import/{job_id}/albums/0/duplicates", params={"candidate_index": 0})
    assert r.status_code == 200
    existing = r.json()["existing"]
    assert len(existing) == 1
    assert existing[0]["album_artist"] == "2Pac"
    assert len(existing[0]["tracks"]) == 15


def test_live_upfront_check_empty_when_clean(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    job_id = _start_and_wait(client, [_parked(0, artist="Nobody", album="Nothing")])
    r = client.get(f"/api/import/{job_id}/albums/0/duplicates")
    assert r.status_code == 200
    assert r.json()["existing"] == []


def test_live_upfront_check_404_when_not_parked(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    job_id = _start_and_wait(client, [_parked(1, artist="X", album="Y")])
    assert client.get(f"/api/import/{job_id}/albums/0/duplicates").status_code == 404
    assert client.get("/api/import/nope/albums/0/duplicates").status_code == 404


def test_live_upfront_check_out_of_range_candidate_falls_back_to_top(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    _add_album(beets_library.lib, artist="2Pac", album="Me Against the World", mb="mb-1", n=2)
    job_id = _start_and_wait(
        client, [_parked(0, artist="2Pac", album="Me Against the World", release_id="mb-1")]
    )
    r = client.get(f"/api/import/{job_id}/albums/0/duplicates", params={"candidate_index": 99})
    assert r.status_code == 200
    assert len(r.json()["existing"]) == 1
