"""API tests for /api/bank — thin-router behavior over the store."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.bank import store
from app.config import settings
from app.models.bank import BankDecision
from app.models.import_models import DuplicatePrompt, IncomingAlbum


@pytest.fixture
def bank_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    return tmp_path / "beets" / "bank"


def _seed(bank_dir: Path, *, folder: str = "/library/A/B") -> str:
    return store.create_item(
        bank_dir, folder=folder, source="sweep", reason="no_match", fingerprint="f" * 64
    ).id


def _seed_dup(bank_dir: Path, *, folder: str) -> str:
    prompt = DuplicatePrompt(
        album_index=0,
        incoming=IncomingAlbum(
            album_artist="A",
            album="B",
            year=None,
            track_count=1,
            format=None,
            bitrate_kbps=None,
            folder=folder,
            has_current_art=False,
        ),
        existing=[],
    )
    return store.create_item(
        bank_dir,
        folder=folder,
        source="sweep",
        reason="needs_dup_resolution",
        fingerprint="f" * 64,
        duplicate=prompt,
    ).id


def test_list_empty(client: TestClient, bank_dir: Path) -> None:
    response = client.get("/api/bank")
    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "total": 0,
        "total_all": 0,
        "offset": 0,
        "limit": 50,
    }


def test_list_and_detail(client: TestClient, bank_dir: Path) -> None:
    item_id = _seed(bank_dir)
    listing = client.get("/api/bank").json()
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == item_id
    assert "parked" not in listing["items"][0]  # summaries exclude payloads
    detail = client.get(f"/api/bank/{item_id}")
    assert detail.status_code == 200
    assert detail.json()["parked"] is None


def test_list_view_active_excludes_resolved_rows(client: TestClient, bank_dir: Path) -> None:
    pending = _seed(bank_dir)
    done = _seed(bank_dir, folder="/library/A/C")
    store.decide_item(bank_dir, done, BankDecision(action="asis"))
    store.set_status(bank_dir, done, "applying")
    store.set_status(bank_dir, done, "done", album_id=1)
    ignored = _seed(bank_dir, folder="/library/A/D")
    store.decide_item(bank_dir, ignored, BankDecision(action="ignore"))

    listing = client.get("/api/bank", params={"view": "active"}).json()
    assert listing["total"] == 1
    assert [row["id"] for row in listing["items"]] == [pending]


def test_list_status_filter_beats_view_active(client: TestClient, bank_dir: Path) -> None:
    _seed(bank_dir)
    ignored = _seed(bank_dir, folder="/library/A/C")
    store.decide_item(bank_dir, ignored, BankDecision(action="ignore"))
    listing = client.get("/api/bank", params={"view": "active", "status": "ignored"}).json()
    assert listing["total"] == 1
    assert [row["id"] for row in listing["items"]] == [ignored]


def test_list_default_view_stays_all(client: TestClient, bank_dir: Path) -> None:
    _seed(bank_dir)
    ignored = _seed(bank_dir, folder="/library/A/C")
    store.decide_item(bank_dir, ignored, BankDecision(action="ignore"))
    listing = client.get("/api/bank").json()
    assert listing["total"] == 2


def test_list_rejects_unknown_view(client: TestClient, bank_dir: Path) -> None:
    assert client.get("/api/bank", params={"view": "resolved"}).status_code == 422


def test_list_reason_filter(client: TestClient, bank_dir: Path) -> None:
    _seed(bank_dir, folder="/library/A/nomatch")  # reason=no_match
    dup_id = _seed_dup(bank_dir, folder="/library/A/dup")
    listing = client.get("/api/bank", params={"reason": "needs_dup_resolution"}).json()
    assert [row["id"] for row in listing["items"]] == [dup_id]
    assert listing["total"] == 1
    assert listing["total_all"] == 2  # the reason filter never narrows total_all


def test_total_all_survives_an_empty_active_view(client: TestClient, bank_dir: Path) -> None:
    # Resolve BOTH rows so the default needs-attention view is empty; total_all
    # still reports the resolved history so the Review page keeps its section.
    first = _seed(bank_dir)
    second = _seed(bank_dir, folder="/library/A/C")
    store.decide_item(bank_dir, first, BankDecision(action="ignore"))
    store.decide_item(bank_dir, second, BankDecision(action="ignore"))
    listing = client.get("/api/bank", params={"view": "active"}).json()
    assert listing["items"] == []
    assert listing["total"] == 0
    assert listing["total_all"] == 2


def test_detail_404(client: TestClient, bank_dir: Path) -> None:
    assert client.get("/api/bank/" + "0" * 32).status_code == 404
    assert client.get("/api/bank/not-an-id").status_code == 404


def test_decision_queues(client: TestClient, bank_dir: Path) -> None:
    item_id = _seed(bank_dir)
    response = client.post(
        f"/api/bank/{item_id}/decision",
        json={"action": "apply", "candidate_index": 1},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "queued"


def test_decision_conflict_is_409(client: TestClient, bank_dir: Path) -> None:
    item_id = _seed(bank_dir)
    client.post(f"/api/bank/{item_id}/decision", json={"action": "ignore"})
    response = client.post(f"/api/bank/{item_id}/decision", json={"action": "asis"})
    assert response.status_code == 409


def test_invalid_decision_body_is_422(client: TestClient, bank_dir: Path) -> None:
    item_id = _seed(bank_dir)
    response = client.post(f"/api/bank/{item_id}/decision", json={"action": "duplicate"})
    assert response.status_code == 422  # duplicate without duplicate_action


def test_delete(client: TestClient, bank_dir: Path) -> None:
    item_id = _seed(bank_dir)
    assert client.delete(f"/api/bank/{item_id}").status_code == 204
    assert client.delete(f"/api/bank/{item_id}").status_code == 404


def test_bulk_ignore(client: TestClient, bank_dir: Path) -> None:
    first = _seed(bank_dir)
    second = _seed(bank_dir, folder="/library/A/C")
    response = client.post("/api/bank/bulk-ignore", json={"ids": [first, second, "x"]})
    assert response.status_code == 200
    assert response.json() == {"ignored": 2}


def test_bulk_delete(client: TestClient, bank_dir: Path) -> None:
    first = _seed(bank_dir)
    second = _seed(bank_dir, folder="/library/A/C")
    # An applying row is skipped, not deleted (it carries its decision).
    applying = _seed(bank_dir, folder="/library/A/D")
    store.decide_item(bank_dir, applying, BankDecision(action="asis"))
    store.set_status(bank_dir, applying, "applying")
    response = client.post("/api/bank/bulk-delete", json={"ids": [first, second, applying, "x"]})
    assert response.status_code == 200
    assert response.json() == {"deleted": 2}
    assert store.get_item(bank_dir, first) is None
    assert store.get_item(bank_dir, second) is None
    survivor = store.get_item(bank_dir, applying)
    assert survivor is not None
    assert survivor.status == "applying"


def test_bulk_delete_goes_on_past_an_id_no_row_can_have(client: TestClient, bank_dir: Path) -> None:
    """A lone surrogate is a legal JSON string but not UTF-8, so SQLite cannot
    bind it. The store answers such an id as absent before any query, so the
    batch goes on and both real rows are removed — no input aborts it mid-way."""
    first = _seed(bank_dir)
    second = _seed(bank_dir, folder="/library/A/C")
    body = f'{{"ids": ["{first}", "\\udcf6", "{second}"]}}'
    response = client.post(
        "/api/bank/bulk-delete", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 200
    assert response.json() == {"deleted": 2}
    assert store.count_items(bank_dir) == 0


@pytest.mark.parametrize("offset", [2**63 - 1, 2**63, 10**20])
def test_list_offset_past_sqlite_integers_is_an_empty_page(
    client: TestClient, bank_dir: Path, offset: int
) -> None:
    """``offset`` has no upper bound in the contract. Past SQLite's 64-bit
    integers it gets the empty page any other offset past the end gets, not a 500."""
    _seed(bank_dir)
    response = client.get("/api/bank", params={"offset": offset})
    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "total": 1,
        "total_all": 1,
        "offset": offset,
        "limit": 50,
    }


def test_decision_pokes_apply_runner_when_wired(client: TestClient, bank_dir: Path) -> None:
    from app.main import app

    class _Recorder:
        def __init__(self) -> None:
            self.poked = 0

        def poke(self) -> None:
            self.poked += 1

    recorder = _Recorder()
    app.state.bank_apply_runner = recorder
    try:
        queued_id = _seed(bank_dir)
        response = client.post(f"/api/bank/{queued_id}/decision", json={"action": "asis"})
        assert response.status_code == 200
        assert response.json()["status"] == "queued"
        assert recorder.poked == 1
        # ignore resolves in the store - nothing for the runner to do.
        ignored_id = _seed(bank_dir, folder="/library/A/C")
        response = client.post(f"/api/bank/{ignored_id}/decision", json={"action": "ignore"})
        assert response.status_code == 200
        assert recorder.poked == 1
    finally:
        del app.state.bank_apply_runner


def test_decision_without_runner_still_works(client: TestClient, bank_dir: Path) -> None:
    # The lifespan-less client fixture has no runner on app.state: the poke is
    # best-effort and the decision endpoint must not care.
    item_id = _seed(bank_dir)
    response = client.post(f"/api/bank/{item_id}/decision", json={"action": "asis"})
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
