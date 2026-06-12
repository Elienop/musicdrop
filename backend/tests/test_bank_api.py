"""API tests for /api/bank — thin-router behavior over the store."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.bank import store
from app.config import settings


@pytest.fixture()
def bank_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    return tmp_path / "beets" / "bank"


def _seed(bank_dir: Path, *, folder: str = "/library/A/B") -> str:
    return store.create_item(
        bank_dir, folder=folder, source="sweep", reason="no_match", fingerprint="f" * 64
    ).id


def test_list_empty(client: TestClient, bank_dir: Path) -> None:
    response = client.get("/api/bank")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 50}


def test_list_and_detail(client: TestClient, bank_dir: Path) -> None:
    item_id = _seed(bank_dir)
    listing = client.get("/api/bank").json()
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == item_id
    assert "parked" not in listing["items"][0]  # summaries exclude payloads
    detail = client.get(f"/api/bank/{item_id}")
    assert detail.status_code == 200
    assert detail.json()["parked"] is None


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
