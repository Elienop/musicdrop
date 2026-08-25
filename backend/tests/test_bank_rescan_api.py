"""POST /api/bank/{id}/rescan — wiring, gates, fingerprint refresh, stale rescue."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api.bank as bank_api
from app.bank import store
from app.bank.fingerprint import folder_fingerprint
from app.beets.research import RescanOutcome, ResearchResult
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


def _outcome(found: bool = True) -> RescanOutcome:
    if not found:
        return RescanOutcome(
            result=None,
            cur_artist="CurA",
            cur_album="CurB",
            recommendation=Recommendation.none,
        )
    return RescanOutcome(
        result=ResearchResult(
            candidate=_candidate(),
            artist="A",
            album="B",
            recommendation=Recommendation.strong,
            confidence=90.0,
        ),
        cur_artist="CurA",
        cur_album="CurB",
        recommendation=Recommendation.strong,
    )


def _seed(bank_dir: Path, folder: Path, **kwargs: Any) -> str:
    defaults: dict[str, Any] = {
        "folder": str(folder),
        "source": "sweep",
        "reason": "no_match",
        "fingerprint": folder_fingerprint(folder),
    }
    defaults.update(kwargs)
    return store.create_item(bank_dir, **defaults).id


def test_rescan_rescues_a_stale_row_and_refreshes_the_fingerprint(
    client: TestClient, bank_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _folder(tmp_path)
    item_id = _seed(bank_dir, folder, fingerprint="f" * 64)  # stale-looking fp
    store.set_status(bank_api.get_bank_dir(), item_id, "stale", error="changed")
    monkeypatch.setattr(bank_api, "rescan_folder", lambda f: _outcome())
    r = client.post(f"/api/bank/{item_id}/rescan")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "needs_review"
    assert body["reason"] == "needs_review"
    assert body["error"] is None
    assert body["fingerprint"] == folder_fingerprint(folder)  # refreshed to REAL disk state
    assert body["parked"]["candidate"]["search_revision"] == 1


def test_rescan_no_candidates_becomes_no_match_row(
    client: TestClient, bank_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _folder(tmp_path)
    item_id = store.create_item(
        bank_dir,
        folder=str(folder),
        source="sweep",
        reason="needs_review",
        fingerprint=folder_fingerprint(folder),
        parked=ParkedAlbum(album_index=2, folder=str(folder), candidate=_candidate(revision=4)),
    ).id
    monkeypatch.setattr(bank_api, "rescan_folder", lambda f: _outcome(found=False))
    body = client.post(f"/api/bank/{item_id}/rescan").json()
    assert body["reason"] == "no_match"
    assert body["parked"] is None
    assert body["artist"] == "CurA"
    assert body["album"] == "CurB"
    assert body["confidence"] == 0.0


def test_rescan_preserves_album_index_and_bumps_revision(
    client: TestClient, bank_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _folder(tmp_path)
    item_id = store.create_item(
        bank_dir,
        folder=str(folder),
        source="sweep",
        reason="needs_review",
        fingerprint=folder_fingerprint(folder),
        parked=ParkedAlbum(album_index=2, folder=str(folder), candidate=_candidate(revision=4)),
    ).id
    monkeypatch.setattr(bank_api, "rescan_folder", lambda f: _outcome())
    body = client.post(f"/api/bank/{item_id}/rescan").json()
    assert body["parked"]["album_index"] == 2
    assert body["parked"]["candidate"]["search_revision"] == 5


def test_rescan_409_when_folder_gone(client: TestClient, bank_dir: Path, tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    item_id = _seed(bank_dir, folder)
    (folder / "01 track.mp3").unlink()
    folder.rmdir()
    r = client.post(f"/api/bank/{item_id}/rescan")
    assert r.status_code == 409
    assert "no longer exists" in r.json()["detail"]


def test_rescan_409_when_no_audio_remains(
    client: TestClient, bank_dir: Path, tmp_path: Path
) -> None:
    # REAL adapter path: only a non-media file left -> NoAudioFilesError (no network).
    folder = _folder(tmp_path)
    (folder / "01 track.mp3").unlink()
    (folder / "cover.jpg").write_bytes(b"\xff\xd8\xff")
    item_id = store.create_item(
        bank_dir,
        folder=str(folder),
        source="sweep",
        reason="no_match",
        fingerprint=folder_fingerprint(folder),
    ).id
    r = client.post(f"/api/bank/{item_id}/rescan")
    assert r.status_code == 409
    assert "no audio files remain" in r.json()["detail"]
    reread = store.get_item(bank_api.get_bank_dir(), item_id)
    assert reread is not None
    assert reread.status == "needs_review"  # untouched


def test_rescan_409_on_a_queued_row(client: TestClient, bank_dir: Path, tmp_path: Path) -> None:
    from app.models.bank import BankDecision

    folder = _folder(tmp_path)
    item_id = _seed(bank_dir, folder)
    store.decide_item(bank_api.get_bank_dir(), item_id, BankDecision(action="asis"))
    assert client.post(f"/api/bank/{item_id}/rescan").status_code == 409


def test_rescan_404_when_row_missing(client: TestClient, bank_dir: Path) -> None:
    assert client.post("/api/bank/" + "0" * 32 + "/rescan").status_code == 404
