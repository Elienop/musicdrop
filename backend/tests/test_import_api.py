import time

from fastapi.testclient import TestClient

from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import reset_registry
from app.import_jobs.runner import InLibraryCopyError
from app.main import app
from app.models.import_api import (
    ActiveImportStatus,
    ImportAlbumStatus,
    ImportAlbumSummary,
    ImportJobState,
    ImportPhase,
    ImportProgress,
    StartImportRequest,
    StartImportResponse,
)
from app.models.import_models import (
    AlbumChange,
    AlbumOutcome,
    AlbumOutcomeStatus,
    Candidate,
    ParkedAlbum,
    Recommendation,
)


def test_import_phase_values() -> None:
    assert [p.value for p in ImportPhase] == [
        "scanning",
        "reviewing",
        "applying",
        "done",
        "failed",
    ]


def test_album_status_values() -> None:
    assert [s.value for s in ImportAlbumStatus] == [
        "needs_review",
        "needs_dup_resolution",
        "decided",
        "applied",
        "skipped",
    ]


def test_start_request_defaults_options_to_none() -> None:
    req = StartImportRequest(path="/music/incoming")
    assert req.path == "/music/incoming"
    assert req.options is None


def test_start_request_accepts_typed_options() -> None:
    from app.models.import_models import ImportOptions

    req = StartImportRequest.model_validate(
        {"path": "/m", "options": {"operation": "move", "unattended": True}}
    )
    assert isinstance(req.options, ImportOptions)
    assert req.options.operation == "move"
    assert req.options.unattended is True


def test_start_response_carries_job_id() -> None:
    resp = StartImportResponse(job_id="abc123")
    assert resp.job_id == "abc123"


def test_job_state_round_trips() -> None:
    state = ImportJobState(
        job_id="j1",
        phase=ImportPhase.reviewing,
        progress=ImportProgress(applied=1, needs_review=1, skipped=0),
        albums=[
            ImportAlbumSummary(
                index=0,
                folder="/music/incoming/Radiohead - OK Computer",
                artist="Radiohead",
                album="OK Computer",
                recommendation=Recommendation.strong,
                confidence=99.0,
                status=ImportAlbumStatus.applied,
            ),
            ImportAlbumSummary(
                index=1,
                folder="/music/incoming/Unknown",
                artist="Radiohead",
                album="OK Computer",
                recommendation=Recommendation.medium,
                confidence=75.5,
                status=ImportAlbumStatus.needs_review,
            ),
        ],
        summary=None,
        error=None,
        set_aside=1,
    )
    dumped = state.model_dump(mode="json")
    assert dumped["phase"] == "reviewing"
    assert dumped["progress"] == {"applied": 1, "needs_review": 1, "skipped": 0}
    assert dumped["albums"][0]["status"] == "applied"
    assert dumped["albums"][1]["status"] == "needs_review"
    assert dumped["summary"] is None
    assert dumped["error"] is None
    assert dumped["origin"] == "manual"  # defaulted
    assert dumped["set_aside"] == 1


def test_active_status_defaults_origin_manual() -> None:
    # origin + needs_review_count must be optional/defaulted so the FE
    # {active:false} fallback (no job) type-checks against the same model.
    s = ActiveImportStatus(active=False)
    assert s.origin == "manual"
    assert s.needs_review_count == 0


def test_job_state_exposes_origin_and_set_aside() -> None:
    # Drive the REAL drain path: a job whose bridge holds one needs_review and
    # one needs_dup_resolution outcome -> set_aside counts both (2). origin is
    # carried off the job (defaults manual).
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob, ImportJobRegistry

    reg = ImportJobRegistry()
    job = ImportJob(id="set-aside-job", bridge=ImportBridge())
    reg._job = job  # white-box: install the job in the single slot for state()
    job.bridge.note_outcome(
        AlbumOutcome(
            album_index=0,
            folder="/in/0",
            artist="A",
            album="X",
            recommendation=Recommendation.medium,
            confidence=50.0,
            status=AlbumOutcomeStatus.needs_review,
        )
    )
    job.bridge.note_outcome(
        AlbumOutcome(
            album_index=1,
            folder="/in/1",
            artist="A",
            album="Y",
            recommendation=Recommendation.strong,
            confidence=0.0,
            status=AlbumOutcomeStatus.needs_dup_resolution,
        )
    )

    state = reg.state("set-aside-job")
    assert state.origin == "manual"
    assert state.set_aside == 2  # needs_review + needs_dup_resolution


def test_import_options_sweep_defaults_false_and_parses() -> None:
    from app.models.import_models import ImportOptions

    assert ImportOptions().sweep is False
    req = StartImportRequest.model_validate({"path": "/library", "options": {"sweep": True}})
    assert req.options is not None
    assert req.options.sweep is True
    assert req.options.unattended is False  # the session ORs sweep in; no contract mutation


def test_sweep_status_defaults() -> None:
    from app.models.import_api import SweepStatus

    s = SweepStatus()
    assert (s.processed, s.auto_applied, s.banked, s.skipped_known) == (0, 0, 0, 0)
    assert s.current_folder is None
    assert s.paused is False


def test_job_state_sweep_block_round_trips() -> None:
    from app.models.import_api import SweepStatus

    state = ImportJobState(
        job_id="s1",
        phase=ImportPhase.done,
        progress=ImportProgress(applied=0, needs_review=0, skipped=0),
        albums=[],
        summary=None,
        error=None,
        origin="sweep",
        set_aside=0,
        sweep=SweepStatus(processed=3, auto_applied=2, banked=1, current_folder="/library/x"),
    )
    dumped = state.model_dump(mode="json")
    assert dumped["origin"] == "sweep"
    assert dumped["sweep"]["processed"] == 3
    assert dumped["sweep"]["paused"] is False
    # Non-sweep jobs default the block to None (existing constructors unchanged).
    assert (
        ImportJobState.model_validate({**dumped, "origin": "manual", "sweep": None}).sweep is None
    )


def test_active_status_sweep_block_defaults_none() -> None:
    assert ActiveImportStatus(active=False).sweep is None


def test_start_import_blank_path_is_422() -> None:
    # An all-whitespace path fails validation (strip + min_length=1) -> 422.
    resp = TestClient(app).post("/api/import", json={"path": "   "})
    assert resp.status_code in (400, 422)


def test_start_import_409_while_library_op_holds_swap_lock() -> None:
    """A held beets_swap_lock (config Apply / duplicate resolve in flight) refuses
    a new import — closing the import-start direction of the strictly-serial
    invariant so two threads never mutate beets at once.
    """
    reset_registry(runner=FakeImportRunner(parked=[]))

    class _LockedLock:
        def locked(self) -> bool:
            return True

    prior = getattr(app.state, "beets_swap_lock", None)
    app.state.beets_swap_lock = _LockedLock()
    try:
        resp = TestClient(app).post("/api/import", json={"path": "/music/incoming"})
    finally:
        if prior is None:
            del app.state.beets_swap_lock
        else:
            app.state.beets_swap_lock = prior
    assert resp.status_code == 409
    assert "library operation" in resp.json()["detail"].lower()


def test_start_import_in_library_copy_is_422() -> None:
    runner = FakeImportRunner()
    runner.validate_error = InLibraryCopyError(
        "This folder is inside your music library; a copy-import would "
        "duplicate its files. Choose move instead."
    )
    reset_registry(runner=runner)
    response = TestClient(app).post(
        "/api/import",
        json={"path": "/library/Artist", "options": {"operation": "copy"}},
    )
    assert response.status_code == 422
    assert "inside your music library" in response.json()["detail"]


def test_get_unknown_job_is_404() -> None:
    resp = TestClient(app).get("/api/import/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Import job not found"


def _api_candidate(rec: Recommendation, *, confidence: float = 75.5) -> Candidate:
    after = AlbumChange(
        artist="Radiohead",
        album="OK Computer",
        year=1997,
        label="Parlophone",
        country="GB",
        media="CD",
    )
    before = AlbumChange(
        artist="Radiohead", album="OK Computr", year=None, label=None, country=None, media=None
    )
    return Candidate(
        confidence=confidence,
        recommendation=rec,
        data_source="MusicBrainz",
        data_url="https://mb/a1",
        cover_after_url="https://coverartarchive.org/release/a1/front-500",
        has_current_art=False,
        changed_fields=["album"],
        album_before=before,
        album_after=after,
        tracks=[],
        missing=[],
        unmatched=[],
        options=[],
    )


def _api_parked(index: int, rec: Recommendation) -> ParkedAlbum:
    return ParkedAlbum(
        album_index=index, folder=f"/music/incoming/album{index}", candidate=_api_candidate(rec)
    )


def _api_applied(index: int) -> AlbumOutcome:
    return AlbumOutcome(
        album_index=index,
        folder=f"/music/incoming/album{index}",
        artist="Radiohead",
        album="OK Computer",
        recommendation=Recommendation.strong,
        confidence=99.0,
        status=AlbumOutcomeStatus.applied,
    )


def _client_with_fake(
    parked: list[ParkedAlbum] | None = None,
    applied: list[AlbumOutcome] | None = None,
    fail_with: str | None = None,
) -> TestClient:
    reset_registry(runner=FakeImportRunner(parked=parked, applied=applied, fail_with=fail_with))
    return TestClient(app)


def _poll(client: TestClient, job_id: str, predicate, attempts: int = 200):  # type: ignore[no-untyped-def]  # test-local poll: predicate is an inline lambda
    for _ in range(attempts):
        state = client.get(f"/api/import/{job_id}").json()
        if predicate(state):
            return state
        time.sleep(0.01)
    return client.get(f"/api/import/{job_id}").json()


def _poll_candidate(client: TestClient, job_id: str, index: int, attempts: int = 200):  # type: ignore[no-untyped-def]  # test-local poll
    """Wait for the parked candidate at ``index`` to be retrievable (HTTP 200).

    The ``needs_review`` outcome is drained onto the feed slightly before the
    candidate itself is parked (``registry._drain_locked`` drains outcomes, then
    parked albums), so polling the feed for the album's presence does NOT
    guarantee the candidate endpoint is ready — there is a sub-millisecond window
    where the feed shows the album but ``row.parked`` is still ``None`` (-> 404).
    Poll the actual endpoint to remove that race.
    """
    url = f"/api/import/{job_id}/albums/{index}"
    for _ in range(attempts):
        resp = client.get(url)
        if resp.status_code == 200:
            return resp
        time.sleep(0.01)
    return client.get(url)


def test_start_returns_202_and_job_id() -> None:
    client = _client_with_fake(parked=[_api_parked(0, Recommendation.medium)])
    resp = client.post("/api/import", json={"path": "/music/incoming"})
    assert resp.status_code == 202
    assert resp.json()["job_id"]


def test_active_probe_false_when_no_job() -> None:
    # Fresh registry with no started job: inactive, and no resume target.
    reset_registry(runner=FakeImportRunner(parked=[]))
    client = TestClient(app)
    resp = client.get("/api/imports/active")
    assert resp.status_code == 200
    # Idle: defaulted origin/needs_review_count keep the {active:false} shape stable.
    assert resp.json() == {
        "active": False,
        "job_id": None,
        "origin": "manual",
        "needs_review_count": 0,
        "sweep": None,
    }


def test_active_probe_true_while_import_runs() -> None:
    # An import parked at index 0 (reviewing phase) is active per the registry's
    # _ACTIVE_PHASES set — the Apply button must be gated until it ends, and the
    # probe must carry the job_id so the import Start screen can offer Resume.
    client = _client_with_fake(parked=[_api_parked(0, Recommendation.medium)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    resp = client.get("/api/imports/active")
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is True
    assert body["job_id"] == job_id
    assert body["origin"] == "manual"
    # needs_review_count is the live set-aside tally; present + non-negative (its
    # exact value races the worker thread emitting the parked album's outcome).
    assert body["needs_review_count"] >= 0


def test_second_concurrent_import_is_409() -> None:
    client = _client_with_fake(parked=[_api_parked(0, Recommendation.medium)])
    first = client.post("/api/import", json={"path": "/music/incoming"})
    assert first.status_code == 202
    second = client.post("/api/import", json={"path": "/music/other"})
    assert second.status_code == 409


def test_feed_shows_applied_then_the_current_needs_review() -> None:
    client = _client_with_fake(
        applied=[_api_applied(0)], parked=[_api_parked(1, Recommendation.medium)]
    )
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    state = _poll(client, job_id, lambda s: len(s["albums"]) == 2)
    assert state["phase"] == "reviewing"
    by_index = {a["index"]: a for a in state["albums"]}
    assert by_index[0]["status"] == "applied"
    assert by_index[1]["status"] == "needs_review"
    assert by_index[1]["album"] == "OK Computer"
    assert state["progress"] == {"applied": 1, "needs_review": 1, "skipped": 0}


def test_get_album_returns_full_candidate() -> None:
    client = _client_with_fake(parked=[_api_parked(0, Recommendation.medium)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll(client, job_id, lambda s: len(s["albums"]) == 1)
    resp = _poll_candidate(client, job_id, 0)  # wait for the parked candidate, not feed presence
    assert resp.status_code == 200
    body = resp.json()
    assert body["album_after"]["album"] == "OK Computer"
    assert body["album_before"]["album"] == "OK Computr"
    assert body["data_url"] == "https://mb/a1"
    assert body["changed_fields"] == ["album"]


def test_get_album_unknown_index_is_404() -> None:
    client = _client_with_fake(parked=[_api_parked(0, Recommendation.medium)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll(client, job_id, lambda s: len(s["albums"]) == 1)
    assert client.get(f"/api/import/{job_id}/albums/99").status_code == 404


def test_choice_apply_drives_job_to_done_with_truthful_summary() -> None:
    client = _client_with_fake(
        applied=[_api_applied(0)], parked=[_api_parked(1, Recommendation.medium)]
    )
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll(client, job_id, lambda s: len(s["albums"]) == 2)

    resp = client.post(
        f"/api/import/{job_id}/albums/1/choice", json={"action": "apply", "candidate_index": None}
    )
    assert resp.status_code == 204

    state = _poll(client, job_id, lambda s: s["phase"] == "done")
    assert state["phase"] == "done"
    by_index = {a["index"]: a for a in state["albums"]}
    assert by_index[0]["status"] == "applied"
    assert by_index[1]["status"] == "decided"
    assert "2 imported" in state["summary"]
    assert "0 skipped" in state["summary"]


def test_choice_unknown_index_is_404() -> None:
    client = _client_with_fake(parked=[_api_parked(0, Recommendation.medium)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll(client, job_id, lambda s: len(s["albums"]) == 1)
    resp = client.post(f"/api/import/{job_id}/albums/99/choice", json={"action": "skip"})
    assert resp.status_code == 404


def test_second_choice_after_advance_is_404() -> None:
    # Once the worker consumes the first reply, ImportBridge.park POPS the slot,
    # so a second push_choice for that index raises KeyError -> 404. (A true 409
    # needs two pushes RACING the same unconsumed slot — not reachable over
    # sequential HTTP; the 409 mapping is unit-tested in the registry.)
    client = _client_with_fake(
        parked=[_api_parked(0, Recommendation.medium), _api_parked(1, Recommendation.low)]
    )
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll(client, job_id, lambda s: any(a["index"] == 0 for a in s["albums"]))
    first = client.post(f"/api/import/{job_id}/albums/0/choice", json={"action": "skip"})
    assert first.status_code == 204
    _poll(client, job_id, lambda s: any(a["index"] == 1 for a in s["albums"]))
    second = client.post(f"/api/import/{job_id}/albums/0/choice", json={"action": "skip"})
    assert second.status_code == 404


def test_abort_choice_ends_job_cleanly() -> None:
    client = _client_with_fake(parked=[_api_parked(0, Recommendation.medium)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    _poll(client, job_id, lambda s: len(s["albums"]) == 1)
    resp = client.post(f"/api/import/{job_id}/albums/0/choice", json={"action": "abort"})
    assert resp.status_code == 204
    state = _poll(client, job_id, lambda s: s["phase"] in ("done", "failed"))
    assert state["phase"] == "done"


def test_worker_crash_marks_failed_never_500() -> None:
    client = _client_with_fake(fail_with="lookup exploded")
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    state = _poll(client, job_id, lambda s: s["phase"] == "failed")
    assert state["phase"] == "failed"
    assert state["error"] == "lookup exploded"


def test_feed_row_carries_album_id_when_present() -> None:
    follow_up = _api_applied(0).model_copy(update={"album_id": 7})
    client = _client_with_fake(applied=[_api_applied(0), follow_up])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    state = _poll(client, job_id, lambda s: s["phase"] == "done")
    assert state["albums"][0]["album_id"] == 7


def test_feed_row_album_id_defaults_to_null() -> None:
    client = _client_with_fake(applied=[_api_applied(0)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    state = _poll(client, job_id, lambda s: s["phase"] == "done")
    assert state["albums"][0]["album_id"] is None


def test_pause_unknown_job_is_404() -> None:
    reset_registry(runner=FakeImportRunner())
    resp = TestClient(app).post("/api/import/does-not-exist/pause")
    assert resp.status_code == 404


def test_pause_non_sweep_job_is_409() -> None:
    client = _client_with_fake(parked=[_api_parked(0, Recommendation.medium)])
    job_id = client.post("/api/import", json={"path": "/music/incoming"}).json()["job_id"]
    resp = client.post(f"/api/import/{job_id}/pause")
    assert resp.status_code == 409
    assert "sweep" in resp.json()["detail"].lower()


def test_pause_finished_sweep_is_409() -> None:
    reset_registry(runner=FakeImportRunner())  # nothing canned: finishes at once
    client = TestClient(app)
    job_id = client.post(
        "/api/import", json={"path": "/library", "options": {"sweep": True}}
    ).json()["job_id"]
    _poll(client, job_id, lambda s: s["phase"] == "done")
    assert client.post(f"/api/import/{job_id}/pause").status_code == 409


def test_sweep_start_pause_and_summary_flow() -> None:
    # The fake parks its album, which keeps the worker blocked - a stable
    # window to observe the active sweep, pause it, then release the worker
    # through the existing choice endpoint (sweep jobs have no feed rows, but
    # the bridge reply slot is real).
    runner = FakeImportRunner(parked=[_api_parked(0, Recommendation.medium)])
    reset_registry(runner=runner)
    client = TestClient(app)

    resp = client.post("/api/import", json={"path": "/library", "options": {"sweep": True}})
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert runner.received_options is not None
    assert runner.received_options.sweep is True

    state = _poll(client, job_id, lambda s: (s.get("sweep") or {}).get("banked", 0) >= 1)
    assert state["origin"] == "sweep"
    assert state["albums"] == []  # counters, never the O(n) feed
    assert state["sweep"]["processed"] == 1

    probe = client.get("/api/imports/active").json()
    assert probe["active"] is True
    assert probe["origin"] == "sweep"
    assert probe["sweep"] is not None

    pause = client.post(f"/api/import/{job_id}/pause")
    assert pause.status_code == 204
    assert client.get(f"/api/import/{job_id}").json()["sweep"]["paused"] is True
    # Idempotent while the sweep is still active.
    assert client.post(f"/api/import/{job_id}/pause").status_code == 204

    release = client.post(f"/api/import/{job_id}/albums/0/choice", json={"action": "skip"})
    assert release.status_code == 204
    state = _poll(client, job_id, lambda s: s["phase"] == "done")
    assert "paused" in (state["summary"] or "")
