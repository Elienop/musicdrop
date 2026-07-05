"""POST /api/bank/{id}/search — endpoint wiring, gates, and the stale flip.

research_folder is stubbed at the router's import site; folders/fingerprints
are real so the stale gate is exercised for real.
"""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api.bank as bank_api
from app.bank import store
from app.bank.fingerprint import folder_fingerprint
from app.beets.research import ResearchResult
from app.config import settings
from app.models.import_models import (
    AlbumChange,
    Candidate,
    CandidateOption,
    ParkedAlbum,
    Recommendation,
)


@pytest.fixture()
def bank_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    return tmp_path / "beets" / "bank"


def _folder(tmp_path: Path, name: str = "drop") -> Path:
    folder = tmp_path / name
    folder.mkdir()
    (folder / "01 track.mp3").write_bytes(b"x" * 32)
    return folder


def _candidate(*, revision: int = 0) -> Candidate:
    change = AlbumChange(artist="A", album="B", year=2000, label=None, country=None, media=None)
    return Candidate(
        confidence=90.0,
        recommendation=Recommendation.strong,
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
                confidence=90.0,
                data_source="MusicBrainz",
                disambiguation=None,
                release_id="mb-9",
            )
        ],
        search_revision=revision,
    )


def _result() -> ResearchResult:
    return ResearchResult(
        candidate=_candidate(),
        artist="A",
        album="B",
        recommendation=Recommendation.strong,
        confidence=90.0,
    )


def _seed_no_match(bank_dir: Path, folder: Path) -> str:
    return store.create_item(
        bank_dir,
        folder=str(folder),
        source="sweep",
        reason="no_match",
        fingerprint=folder_fingerprint(folder),
    ).id


BODY = {"release_id": "https://musicbrainz.org/release/mb-9"}


def test_search_rescues_a_no_match_row(
    client: TestClient, bank_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _folder(tmp_path)
    item_id = _seed_no_match(bank_dir, folder)
    seen: dict[str, Any] = {}

    def fake_research(folder_arg: str, search: Any) -> ResearchResult:
        seen["folder"] = folder_arg
        seen["release_id"] = search.release_id
        return _result()

    monkeypatch.setattr(bank_api, "research_folder", fake_research)
    r = client.post(f"/api/bank/{item_id}/search", json=BODY)
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert body["item"]["reason"] == "needs_review"
    assert body["item"]["parked"]["candidate"]["search_revision"] == 1  # 0 -> 1
    assert body["item"]["artist"] == "A"
    assert seen["folder"] == str(folder)
    assert seen["release_id"] == BODY["release_id"]


def test_search_bumps_revision_on_a_candidate_row(
    client: TestClient, bank_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _folder(tmp_path)
    item_id = store.create_item(
        bank_dir,
        folder=str(folder),
        source="sweep",
        reason="needs_review",
        fingerprint=folder_fingerprint(folder),
        parked=ParkedAlbum(album_index=3, folder=str(folder), candidate=_candidate(revision=2)),
    ).id
    monkeypatch.setattr(bank_api, "research_folder", lambda f, s: _result())
    body = client.post(f"/api/bank/{item_id}/search", json=BODY).json()
    assert body["item"]["parked"]["candidate"]["search_revision"] == 3
    assert body["item"]["parked"]["album_index"] == 3  # preserved


def test_search_no_hit_leaves_the_row_untouched(
    client: TestClient, bank_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _folder(tmp_path)
    item_id = _seed_no_match(bank_dir, folder)
    monkeypatch.setattr(bank_api, "research_folder", lambda f, s: None)
    r = client.post(f"/api/bank/{item_id}/search", json=BODY)
    assert r.status_code == 200
    assert r.json()["found"] is False
    reread = store.get_item(bank_api.get_bank_dir(), item_id)
    assert reread is not None and reread.reason == "no_match" and reread.parked is None


def test_search_flips_stale_on_fingerprint_mismatch(
    client: TestClient, bank_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _folder(tmp_path)
    item_id = _seed_no_match(bank_dir, folder)
    (folder / "02 extra.mp3").write_bytes(b"y")  # folder changed after banking
    called = {"n": 0}

    def fake_research(f: Any, s: Any) -> ResearchResult:
        called["n"] += 1  # must never fire: the stale gate short-circuits first
        return _result()

    monkeypatch.setattr(bank_api, "research_folder", fake_research)
    r = client.post(f"/api/bank/{item_id}/search", json=BODY)
    assert r.status_code == 409
    assert called["n"] == 0  # never searched a changed folder
    reread = store.get_item(bank_api.get_bank_dir(), item_id)
    assert reread is not None and reread.status == "stale"


def test_search_flips_stale_when_the_folder_is_gone(
    client: TestClient, bank_dir: Path, tmp_path: Path
) -> None:
    folder = _folder(tmp_path)
    item_id = _seed_no_match(bank_dir, folder)
    (folder / "01 track.mp3").unlink()
    folder.rmdir()
    r = client.post(f"/api/bank/{item_id}/search", json=BODY)
    assert r.status_code == 409
    reread = store.get_item(bank_api.get_bank_dir(), item_id)
    assert reread is not None and reread.status == "stale"


@pytest.mark.parametrize("action", ["asis"])
def test_search_409_on_a_queued_row(
    client: TestClient, bank_dir: Path, tmp_path: Path, action: str
) -> None:
    from app.models.bank import BankDecision

    folder = _folder(tmp_path)
    item_id = _seed_no_match(bank_dir, folder)
    store.decide_item(bank_api.get_bank_dir(), item_id, BankDecision(action="asis"))
    assert client.post(f"/api/bank/{item_id}/search", json=BODY).status_code == 409


def test_search_409_on_a_dup_resolution_row(
    client: TestClient, bank_dir: Path, tmp_path: Path
) -> None:
    from app.models.import_models import DuplicatePrompt, IncomingAlbum

    folder = _folder(tmp_path)
    item_id = store.create_item(
        bank_dir,
        folder=str(folder),
        source="sweep",
        reason="needs_dup_resolution",
        fingerprint=folder_fingerprint(folder),
        duplicate=DuplicatePrompt(
            album_index=0,
            incoming=IncomingAlbum(
                album_artist="A",
                album="B",
                year=None,
                track_count=1,
                format=None,
                bitrate_kbps=None,
                folder=str(folder),
                has_current_art=False,
            ),
            existing=[],
        ),
    ).id
    assert client.post(f"/api/bank/{item_id}/search", json=BODY).status_code == 409


def test_search_404_when_row_missing(client: TestClient, bank_dir: Path) -> None:
    assert client.post("/api/bank/" + "0" * 32 + "/search", json=BODY).status_code == 404


def test_search_422_on_an_empty_search(client: TestClient, bank_dir: Path, tmp_path: Path) -> None:
    item_id = _seed_no_match(bank_dir, _folder(tmp_path))
    assert client.post(f"/api/bank/{item_id}/search", json={}).status_code == 422
