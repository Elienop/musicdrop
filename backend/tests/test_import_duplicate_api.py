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


def _is_parked(registry: ImportJobRegistry, job_id: str, index: int) -> bool:
    try:
        registry.parked_duplicate(job_id, index)
    except KeyError:
        return False
    return True


def _poll_parked(registry: ImportJobRegistry, job_id: str, index: int) -> None:
    """Wait until the worker is parked on the duplicate prompt, not just until its row shows.

    The fake notes the row's outcome BEFORE ``park_duplicate`` registers the reply
    slot (``app/import_jobs/fakes.py``), so a decision sent on the row alone can
    find no slot. The prompt is published under the same lock as the slot, the
    race ``_poll_dup_prompt`` guards for the API tests.
    """
    _poll(lambda: _is_parked(registry, job_id, index), bool)


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
    _poll_parked(registry, job_id, 0)

    registry.record_duplicate_decision(job_id, 0, DuplicateDecision(action=DuplicateAction.replace))
    _poll(lambda: registry.state(job_id).phase, lambda p: p is ImportPhase.done)
    state = registry.state(job_id)
    assert state.albums[0].status is ImportAlbumStatus.decided
    # replace is a landing action, but the fake emits no follow-up id (it models
    # a replace beets never task.add'd) -> it did not land, so it is not counted
    # imported. A real replace that landed would carry an album_id (see the
    # helper matrix in test_import_registry).
    assert state.progress.applied == 0
    assert state.progress.not_landed == 1
    assert state.progress.skipped == 0
    assert state.albums[0].did_not_land is True


def test_skip_new_counts_as_skipped() -> None:
    fake = FakeImportRunner(duplicates=[_prompt(0)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll_parked(registry, job_id, 0)
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


def _client(  # type: ignore[no-untyped-def]  # test-local TestClient factory
    duplicates: list[DuplicatePrompt],
    art_sources: dict[int, str] | None = None,
):
    from fastapi.testclient import TestClient

    from app.import_jobs.registry import reset_registry
    from app.main import app

    reset_registry(runner=FakeImportRunner(duplicates=duplicates, art_sources=art_sources))
    return TestClient(app)


def _poll_client(client, job_id, predicate, attempts: int = 200):  # type: ignore[no-untyped-def]  # test-local poll
    for _ in range(attempts):
        state = client.get(f"/api/import/{job_id}").json()
        if predicate(state):
            return state
        threading.Event().wait(0.01)
    raise TimeoutError("condition not met")


def _poll_dup_prompt(client, job_id, index, attempts: int = 200):  # type: ignore[no-untyped-def]  # test-local poll
    """Poll the duplicate prompt itself until it is actionable.

    The feed flips to needs_dup_resolution a beat before the prompt row is
    parked (the registry drains outcomes first, parked rows second — the same
    feed-vs-candidate race test_import_api.py's _poll_candidate guards), so
    feed status alone is not a readiness signal for the duplicate endpoints.
    """
    for _ in range(attempts):
        resp = client.get(f"/api/import/{job_id}/albums/{index}/duplicate")
        if resp.status_code == 200:
            return resp
        threading.Event().wait(0.01)
    raise TimeoutError("duplicate prompt never became actionable")


def test_get_duplicate_returns_prompt_then_decision_204() -> None:
    client = _client([_prompt(0)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll_client(
        client, job_id, lambda s: any(a["status"] == "needs_dup_resolution" for a in s["albums"])
    )

    got = _poll_dup_prompt(client, job_id, 0)
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


def test_get_duplicate_404_after_the_run_is_over() -> None:
    """A stop releases the prompt; the GET must follow the POST into 404.

    Served, it renders a resolve screen for a job with no worker left to answer
    - the cover read on the same row already 404s. The registry method behind
    it stays open for the bank apply runner; the gate is on the route's own
    read (``ImportJobRegistry.parked_duplicate``).
    """
    from app.import_jobs.registry import get_registry

    client = _client([_prompt(0)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    assert _poll_dup_prompt(client, job_id, 0).status_code == 200  # the live control

    assert client.post(f"/api/import/{job_id}/stop").status_code == 204
    _poll_client(client, job_id, lambda s: s["phase"] == "done")

    assert client.get(f"/api/import/{job_id}/albums/0/duplicate").status_code == 404
    assert (
        client.post(
            f"/api/import/{job_id}/albums/0/duplicate", json={"action": "keep_both"}
        ).status_code
        == 404
    )
    # The row still holds the prompt, and the runner's read still answers.
    assert get_registry().duplicate_prompt(job_id, 0).album_index == 0


def test_cover_streams_embedded_art_for_duplicate_row(monkeypatch: pytest.MonkeyPatch) -> None:
    # The "Importing (new)" panel of the duplicate-decision page fetches the
    # current-files cover via GET /albums/{i}/cover. That must serve for a
    # DUPLICATE row (art_source recorded by park_duplicate), not just a parked
    # candidate. Mirrors test_import_cover.test_cover_streams_embedded_art.
    monkeypatch.setattr(
        "app.import_jobs.registry.embedded_art",
        lambda p: (b"PNGDATA", "image/png"),
    )
    client = _client([_prompt(0)], art_sources={0: "/fake/album0/track.flac"})
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll_client(
        client, job_id, lambda s: any(a["status"] == "needs_dup_resolution" for a in s["albums"])
    )
    _poll_dup_prompt(client, job_id, 0)
    r = client.get(f"/api/import/{job_id}/albums/0/cover")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert r.content == b"PNGDATA"


def test_post_duplicate_decision_409_on_second() -> None:
    client = _client([_prompt(0)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll_client(
        client, job_id, lambda s: any(a["status"] == "needs_dup_resolution" for a in s["albums"])
    )
    _poll_dup_prompt(client, job_id, 0)
    first = client.post(f"/api/import/{job_id}/albums/0/duplicate", json={"action": "skip_new"})
    assert first.status_code == 204
    second = client.post(f"/api/import/{job_id}/albums/0/duplicate", json={"action": "skip_new"})
    # Slot already advanced (404) or a racing second decision (409); both are
    # "no longer awaiting" — the FE swallows them.
    assert second.status_code in (404, 409)
