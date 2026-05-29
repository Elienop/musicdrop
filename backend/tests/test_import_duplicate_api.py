"""Registry feed + API for duplicate-on-import prompts (hermetic, FakeImportRunner)."""

from __future__ import annotations

import threading

import pytest

from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import ImportJobRegistry
from app.models.import_api import ImportAlbumStatus, ImportPhase
from app.models.import_models import (
    DuplicateAction,
    DuplicateDecision,
    DuplicatePrompt,
    ExistingAlbum,
    IncomingAlbum,
)


def _prompt(index: int) -> DuplicatePrompt:
    return DuplicatePrompt(
        album_index=index,
        incoming=IncomingAlbum(
            album_artist="Radiohead",
            album="In Rainbows",
            year=2007,
            track_count=10,
            format="FLAC",
            bitrate_kbps=900,
            folder=f"/incoming/album{index}",
            has_current_art=False,
        ),
        existing=[
            ExistingAlbum(
                album_id=1,
                album_artist="Radiohead",
                album="In Rainbows",
                year=2007,
                track_count=9,
                format="MP3",
                bitrate_kbps=320,
                folder="/music/Radiohead/In Rainbows",
            )
        ],
    )


def _poll(fn, want, attempts: int = 200) -> None:  # type: ignore[no-untyped-def]  # test-local poll: fn/want are inline callables
    ev = threading.Event()
    for _ in range(attempts):
        if want(fn()):
            return
        ev.wait(0.01)
    raise TimeoutError("condition not met within poll budget")


def test_drain_flips_row_to_needs_dup_resolution() -> None:
    fake = FakeImportRunner(duplicates=[_prompt(0)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")

    _poll(
        lambda: registry.state(job_id).albums,
        lambda rows: bool(rows) and rows[0].status is ImportAlbumStatus.needs_dup_resolution,
    )
    assert registry.state(job_id).phase is ImportPhase.reviewing
    prompt = registry.duplicate_prompt(job_id, 0)
    assert prompt.existing[0].album_id == 1


def test_record_duplicate_decision_unblocks_and_marks() -> None:
    fake = FakeImportRunner(duplicates=[_prompt(0)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 1)

    registry.record_duplicate_decision(job_id, 0, DuplicateDecision(action=DuplicateAction.replace))
    _poll(lambda: registry.state(job_id).phase, lambda p: p is ImportPhase.done)
    state = registry.state(job_id)
    assert state.albums[0].status is ImportAlbumStatus.decided
    assert state.progress.applied == 1  # replace counts as imported
    assert state.progress.skipped == 0


def test_skip_new_counts_as_skipped() -> None:
    fake = FakeImportRunner(duplicates=[_prompt(0)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 1)
    registry.record_duplicate_decision(
        job_id, 0, DuplicateDecision(action=DuplicateAction.skip_new)
    )
    _poll(lambda: registry.state(job_id).phase, lambda p: p is ImportPhase.done)
    state = registry.state(job_id)
    assert state.progress.skipped == 1
    assert state.progress.applied == 0


def test_unknown_duplicate_index_raises_keyerror() -> None:
    fake = FakeImportRunner(duplicates=[_prompt(0)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 1)
    with pytest.raises(KeyError):
        registry.duplicate_prompt(job_id, 99)


def _client(duplicates: list[DuplicatePrompt]):  # type: ignore[no-untyped-def]  # test-local TestClient factory
    from fastapi.testclient import TestClient

    from app.import_jobs.registry import reset_registry
    from app.main import app

    reset_registry(runner=FakeImportRunner(duplicates=duplicates))
    return TestClient(app)


def _poll_client(client, job_id, predicate, attempts: int = 200):  # type: ignore[no-untyped-def]  # test-local poll
    for _ in range(attempts):
        state = client.get(f"/api/import/{job_id}").json()
        if predicate(state):
            return state
        threading.Event().wait(0.01)
    raise TimeoutError("condition not met")


def test_get_duplicate_returns_prompt_then_decision_204() -> None:
    client = _client([_prompt(0)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll_client(
        client, job_id, lambda s: any(a["status"] == "needs_dup_resolution" for a in s["albums"])
    )

    got = client.get(f"/api/import/{job_id}/albums/0/duplicate")
    assert got.status_code == 200
    assert got.json()["existing"][0]["album_id"] == 1

    resp = client.post(f"/api/import/{job_id}/albums/0/duplicate", json={"action": "replace"})
    assert resp.status_code == 204


def test_get_duplicate_404_when_not_parked() -> None:
    client = _client([_prompt(0)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll_client(client, job_id, lambda s: len(s["albums"]) == 1)
    resp = client.get(f"/api/import/{job_id}/albums/99/duplicate")
    assert resp.status_code == 404


def test_post_duplicate_decision_409_on_second() -> None:
    client = _client([_prompt(0)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll_client(
        client, job_id, lambda s: any(a["status"] == "needs_dup_resolution" for a in s["albums"])
    )
    first = client.post(f"/api/import/{job_id}/albums/0/duplicate", json={"action": "skip_new"})
    assert first.status_code == 204
    second = client.post(f"/api/import/{job_id}/albums/0/duplicate", json={"action": "skip_new"})
    # Slot already advanced (404) or a racing second decision (409); both are
    # "no longer awaiting" — the FE swallows them.
    assert second.status_code in (404, 409)
