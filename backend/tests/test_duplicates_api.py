"""End-to-end tests for the duplicates API (GET report + POST resolve)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def test_get_duplicates_strict(client: TestClient) -> None:
    r = client.get("/api/duplicates", params={"mode": "strict"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "strict"
    assert isinstance(body["groups"], list)
    assert body["group_count"] == len(body["groups"])


def test_get_duplicates_defaults_to_strict(client: TestClient) -> None:
    r = client.get("/api/duplicates")
    assert r.status_code == 200
    assert r.json()["mode"] == "strict"


def test_resolve_409_while_import_active(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force an active import so the gate fires (mirrors the config-Apply gate).
    from app.import_jobs.registry import get_registry

    monkeypatch.setattr(get_registry(), "has_active_job", lambda: True)
    r = client.post(
        "/api/duplicates/resolve",
        json={"mode": "strict", "keep_album_id": 1, "remove_album_ids": [2]},
    )
    assert r.status_code == 409
    assert "import" in r.json()["detail"].lower()


def test_resolve_409_on_stale_group(client: TestClient) -> None:
    # No such group in the seeded client library → StaleGroupError → 409.
    r = client.post(
        "/api/duplicates/resolve",
        json={"mode": "strict", "keep_album_id": 999999, "remove_album_ids": [888888]},
    )
    assert r.status_code == 409


def test_resolve_all_409_while_import_active(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.import_jobs.registry import get_registry

    monkeypatch.setattr(get_registry(), "has_active_job", lambda: True)
    r = client.post(
        "/api/duplicates/resolve-all",
        json={"mode": "strict", "groups": [{"keep_album_id": 1, "remove_album_ids": [2]}]},
    )
    assert r.status_code == 409
    assert "import" in r.json()["detail"].lower()


def test_resolve_all_skips_stale_groups_with_200(client: TestClient) -> None:
    # Bogus group against the seeded library: it drifts -> skipped, none
    # resolved, but the batch itself is a normal 200.
    r = client.post(
        "/api/duplicates/resolve-all",
        json={
            "mode": "strict",
            "groups": [{"keep_album_id": 999999, "remove_album_ids": [888888]}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["group_count"] == 0
    assert body["moved_count"] == 0
    assert len(body["skipped_stale"]) == 1
