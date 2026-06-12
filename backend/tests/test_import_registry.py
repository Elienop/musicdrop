import threading

import pytest

from app.beets.import_session import InLibraryCopyError
from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import ImportJobRegistry
from app.models.import_api import ImportAlbumStatus, ImportPhase, SweepStatus
from app.models.import_models import (
    AlbumChange,
    AlbumOutcome,
    AlbumOutcomeStatus,
    Candidate,
    ImportAction,
    ImportChoice,
    ImportOptions,
    ParkedAlbum,
    Recommendation,
)


def _candidate(rec: Recommendation, *, confidence: float = 75.5) -> Candidate:
    album = AlbumChange(
        artist="Radiohead", album="OK Computer", year=1997, label=None, country=None, media=None
    )
    return Candidate(
        confidence=confidence,
        recommendation=rec,
        data_source="MusicBrainz",
        data_url="https://mb/a1",
        cover_after_url="https://coverartarchive.org/release/a1/front-500",
        has_current_art=False,
        changed_fields=["album"],
        album_before=album,
        album_after=album,
        tracks=[],
        missing=[],
        unmatched=[],
        options=[],
    )


def _parked(index: int, rec: Recommendation) -> ParkedAlbum:
    return ParkedAlbum(
        album_index=index, folder=f"/music/incoming/album{index}", candidate=_candidate(rec)
    )


def _applied_outcome(index: int) -> AlbumOutcome:
    return AlbumOutcome(
        album_index=index,
        folder=f"/music/incoming/album{index}",
        artist="Radiohead",
        album="OK Computer",
        recommendation=Recommendation.strong,
        confidence=99.0,
        status=AlbumOutcomeStatus.applied,
    )


def _poll(fn, want, attempts: int = 200) -> None:  # type: ignore[no-untyped-def]  # test-local poll: fn/want are inline callables
    ev = threading.Event()
    for _ in range(attempts):
        if want(fn()):
            return
        ev.wait(0.01)
    raise TimeoutError("condition not met within poll budget")


def test_start_returns_job_and_marks_active() -> None:
    registry = ImportJobRegistry(
        runner=FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    )
    job_id = registry.start("/music/incoming")
    assert job_id
    assert registry.get(job_id) is not None
    assert registry.has_active_job() is True


def test_active_job_id_tracks_started_job() -> None:
    registry = ImportJobRegistry(
        runner=FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    )
    assert registry.active_job_id() is None
    job_id = registry.start("/music/incoming")
    assert registry.active_job_id() == job_id


def test_second_start_while_active_raises() -> None:
    registry = ImportJobRegistry(
        runner=FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    )
    registry.start("/music/incoming")
    with pytest.raises(RuntimeError):
        registry.start("/music/other")


def test_drain_builds_feed_with_applied_then_the_current_parked() -> None:
    fake = FakeImportRunner(
        applied=[_applied_outcome(0)], parked=[_parked(1, Recommendation.medium)]
    )
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")

    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 2)
    rows = {r.index: r for r in registry.state(job_id).albums}
    assert rows[0].status is ImportAlbumStatus.applied
    assert rows[1].status is ImportAlbumStatus.needs_review
    assert registry.state(job_id).phase is ImportPhase.reviewing
    assert registry.state(job_id).progress.applied == 1
    assert registry.state(job_id).progress.needs_review == 1
    assert registry.state(job_id).progress.skipped == 0


def test_record_choice_marks_decided_and_unblocks_worker() -> None:
    fake = FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")

    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 1)
    registry.record_choice(job_id, 0, ImportChoice(action=ImportAction.apply))

    _poll(lambda: registry.state(job_id).phase, lambda p: p is ImportPhase.done)
    state = registry.state(job_id)
    assert state.phase is ImportPhase.done
    assert state.albums[0].status is ImportAlbumStatus.decided
    assert state.summary is not None


def test_unknown_index_choice_raises_keyerror() -> None:
    fake = FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 1)
    with pytest.raises(KeyError):
        registry.record_choice(job_id, 99, ImportChoice(action=ImportAction.apply))


def test_record_choice_duplicate_raises_runtimeerror() -> None:
    # The 409 path: two choices RACE the same still-unconsumed reply slot. Forced
    # deterministically — the worker is blocked in bridge.park's reply.get(); two
    # synchronous push_choice calls (GIL held) fill the maxsize=1 reply queue, so
    # the 2nd raises RuntimeError before the worker drains it. Proves
    # record_choice PROPAGATES the bridge's RuntimeError (router maps it to 409).
    fake = FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 1)
    registry.record_choice(job_id, 0, ImportChoice(action=ImportAction.skip))
    with pytest.raises(RuntimeError):
        registry.record_choice(job_id, 0, ImportChoice(action=ImportAction.skip))


def test_worker_error_marks_job_failed() -> None:
    fake = FakeImportRunner(parked=[], fail_with="boom")
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).phase, lambda p: p is ImportPhase.failed)
    state = registry.state(job_id)
    assert state.phase is ImportPhase.failed
    assert state.error == "boom"


@pytest.mark.parametrize(
    ("action", "imported", "skipped"),
    [
        (ImportAction.apply, 1, 0),
        (ImportAction.asis, 1, 0),
        (ImportAction.astracks, 1, 0),
        (ImportAction.skip, 0, 1),
    ],
)
def test_decided_action_buckets_imported_vs_skipped(
    action: ImportAction, imported: int, skipped: int
) -> None:
    # Guards _APPLY_ACTIONS membership: apply/asis/astracks count as imported,
    # everything else (skip, and abort via the same not-in-apply branch) as
    # skipped. Without this, dropping asis/astracks from the set is a silent
    # mis-count.
    fake = FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 1)
    registry.record_choice(job_id, 0, ImportChoice(action=action))
    _poll(lambda: registry.state(job_id).phase, lambda p: p is ImportPhase.done)
    state = registry.state(job_id)
    assert state.progress.applied == imported
    assert state.progress.skipped == skipped


def test_summary_counts_applied_and_decided_truthfully() -> None:
    fake = FakeImportRunner(
        applied=[_applied_outcome(0)], parked=[_parked(1, Recommendation.medium)]
    )
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 2)
    registry.record_choice(job_id, 1, ImportChoice(action=ImportAction.apply))
    _poll(lambda: registry.state(job_id).phase, lambda p: p is ImportPhase.done)
    summary = registry.state(job_id).summary
    assert summary is not None
    assert "2 imported" in summary
    assert "0 skipped" in summary


def test_stale_on_finish_is_ignored() -> None:
    # A finish callback carrying a stale (replaced) job id must NOT mutate the
    # current job — the single-slot replace-on-new-start guard.
    registry = ImportJobRegistry(
        runner=FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    )
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 1)
    registry._on_finish("some-other-stale-id")
    state = registry.state(job_id)
    assert state.phase is not ImportPhase.done
    assert state.summary is None


def test_summary_counts_skipped_truthfully() -> None:
    fake = FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 1)
    registry.record_choice(job_id, 0, ImportChoice(action=ImportAction.skip))
    _poll(lambda: registry.state(job_id).phase, lambda p: p is ImportPhase.done)
    summary = registry.state(job_id).summary
    assert summary is not None
    assert "0 imported" in summary
    assert "1 skipped" in summary


def test_progress_skipped_mirrors_summary_skipped() -> None:
    # progress.skipped is the LIVE mirror of the done-summary's skipped count:
    # a parked album the user resolved with skip counts toward both, and never
    # toward progress.applied.
    fake = FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 1)
    registry.record_choice(job_id, 0, ImportChoice(action=ImportAction.skip))
    _poll(lambda: registry.state(job_id).phase, lambda p: p is ImportPhase.done)
    state = registry.state(job_id)
    assert state.progress.skipped == 1
    assert state.progress.applied == 0
    assert state.summary is not None
    assert "1 skipped" in state.summary


def test_progress_applied_matches_summary_imported() -> None:
    # A decided-skip must not inflate progress.applied (it is not imported).
    fake = FakeImportRunner(
        applied=[_applied_outcome(0)], parked=[_parked(1, Recommendation.medium)]
    )
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 2)
    registry.record_choice(job_id, 1, ImportChoice(action=ImportAction.skip))
    _poll(lambda: registry.state(job_id).phase, lambda p: p is ImportPhase.done)
    state = registry.state(job_id)
    assert state.progress.applied == 1  # only the auto-applied album
    assert state.summary is not None
    assert "1 imported" in state.summary
    assert "1 skipped" in state.summary


def test_start_forwards_options_to_runner() -> None:
    fake = FakeImportRunner(applied=[_applied_outcome(0)])
    reg = ImportJobRegistry(runner=fake)
    job_id = reg.start("/music/incoming", options=ImportOptions(operation="move", unattended=True))
    _poll(lambda: fake.received_options, lambda o: o is not None)
    assert fake.received_options == ImportOptions(operation="move", unattended=True)
    assert reg.get(job_id) is not None


def test_start_without_options_is_none() -> None:
    fake = FakeImportRunner(applied=[_applied_outcome(0)])
    reg = ImportJobRegistry(runner=fake)
    job_id = reg.start("/music/incoming")
    _poll(lambda: reg.get(job_id) is not None, lambda x: x)
    assert fake.received_options is None


def test_unattended_strong_then_duplicate_is_set_aside_not_imported() -> None:
    # White-box the unattended collision the feed-drain must survive: choose_match
    # auto-applies a strong match (applied outcome for index 0), then
    # resolve_duplicate emits needs_dup_resolution for the SAME index and SKIPs
    # WITHOUT parking (no dup prompt on the bridge -> the park-duplicate flip never
    # runs). The later set-aside outcome must upgrade the existing row so the album
    # counts as set-aside, not the SKIPped album being mis-reported as imported.
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry()
    job = ImportJob(id="dup-unattended", bridge=ImportBridge())
    reg._job = job  # white-box: install the job in the single slot for state()
    job.bridge.note_outcome(_applied_outcome(0))
    job.bridge.note_outcome(
        AlbumOutcome(
            album_index=0,
            folder="/music/incoming/album0",
            artist="Radiohead",
            album="OK Computer",
            recommendation=Recommendation.strong,
            confidence=0.0,
            status=AlbumOutcomeStatus.needs_dup_resolution,
        )
    )

    state = reg.state("dup-unattended")
    assert state.albums[0].status is ImportAlbumStatus.needs_dup_resolution
    assert state.set_aside == 1
    assert state.progress.applied == 0  # _is_imported() is False
    assert reg._is_imported(job.albums[0]) is False


def test_active_status_tolerates_slot_vanishing_during_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # TOCTOU: active_status snapshots the active job_id under the lock, releases
    # it, then drains. If the slot finished AND a fresh import claimed it in that
    # window, the captured id no longer resolves and drain() raises KeyError. The
    # frequently-polled probe must report idle, not propagate a 500.
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob
    from app.models.import_api import ImportAlbumSummary

    reg = ImportJobRegistry()
    reg._job = ImportJob(id="old", bridge=ImportBridge(), phase=ImportPhase.reviewing)

    def vanishing_drain(job_id: str) -> list[ImportAlbumSummary]:
        raise KeyError(job_id)  # the captured slot no longer resolves

    monkeypatch.setattr(reg, "drain", vanishing_drain)
    status = reg.active_status()
    assert status.active is False
    assert status.job_id is None


def _applied_follow_up(index: int, album_id: int) -> AlbumOutcome:
    """The follow-up outcome the session flushes once beets assigns the id."""
    return _applied_outcome(index).model_copy(update={"album_id": album_id})


def test_follow_up_outcome_attaches_album_id_without_new_row() -> None:
    registry = ImportJobRegistry(
        runner=FakeImportRunner(applied=[_applied_outcome(0), _applied_follow_up(0, 7)])
    )
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).phase, lambda p: p is ImportPhase.done)
    state = registry.state(job_id)
    assert len(state.albums) == 1  # the follow-up updated the row, no second row
    assert state.albums[0].status is ImportAlbumStatus.applied
    assert state.albums[0].album_id == 7
    assert state.progress.applied == 1  # counted once


def test_start_validate_failure_takes_no_slot() -> None:
    runner = FakeImportRunner()
    runner.validate_error = InLibraryCopyError("refused")
    reg = ImportJobRegistry(runner=runner)
    with pytest.raises(InLibraryCopyError):
        reg.start("/library/Artist", options=ImportOptions(operation="copy"))
    # The failed validation must not have consumed the single slot:
    assert reg.active_status().active is False
    # The runner was never run for the refused start (validate fail-fasts).
    assert runner.received_options is None
    # And a subsequent valid start succeeds.
    runner.validate_error = None
    job_id = reg.start("/downloads/Artist")
    assert job_id


def test_start_passes_path_and_options_to_validate() -> None:
    runner = FakeImportRunner()
    reg = ImportJobRegistry(runner=runner)
    opts = ImportOptions(operation="move")
    reg.start("/downloads/Artist", options=opts)
    assert runner.validate_calls == [("/downloads/Artist", opts)]


def _sweep_outcome(
    index: int, status: AlbumOutcomeStatus, album_id: int | None = None
) -> AlbumOutcome:
    return AlbumOutcome(
        album_index=index,
        folder=f"/library/album{index}",
        artist="A",
        album="B",
        recommendation=Recommendation.medium,
        confidence=50.0,
        status=status,
        album_id=album_id,
    )


def _install_sweep_job(reg: ImportJobRegistry, job_id: str = "sweep-job"):  # type: ignore[no-untyped-def]  # test-local helper returns the white-box ImportJob
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    job = ImportJob(id=job_id, bridge=ImportBridge(), origin="sweep", sweep=SweepStatus())
    reg._job = job  # white-box: install in the single slot (established pattern)
    return job


def test_sweep_options_set_origin_and_sweep_block() -> None:
    fake = FakeImportRunner(applied=[])
    reg = ImportJobRegistry(runner=fake)
    job_id = reg.start("/library", options=ImportOptions(sweep=True))
    _poll(lambda: reg.state(job_id).phase, lambda p: p is ImportPhase.done)
    state = reg.state(job_id)
    assert state.origin == "sweep"
    assert state.sweep is not None
    assert state.summary == "swept 0, auto-applied 0, banked 0"


def test_manual_job_has_no_sweep_block() -> None:
    fake = FakeImportRunner(applied=[_applied_outcome(0)])
    reg = ImportJobRegistry(runner=fake)
    job_id = reg.start("/music/incoming")
    _poll(lambda: reg.state(job_id).phase, lambda p: p is ImportPhase.done)
    state = reg.state(job_id)
    assert state.origin == "manual"
    assert state.sweep is None


def test_sweep_drain_updates_counters_not_feed_rows() -> None:
    reg = ImportJobRegistry()
    job = _install_sweep_job(reg)
    job.bridge.note_outcome(_sweep_outcome(0, AlbumOutcomeStatus.applied))  # strong auto
    job.bridge.note_outcome(_sweep_outcome(1, AlbumOutcomeStatus.needs_review))  # banked
    job.bridge.note_outcome(_sweep_outcome(2, AlbumOutcomeStatus.skipped))  # banked no_match
    job.bridge.note_outcome(_sweep_outcome(0, AlbumOutcomeStatus.applied, album_id=42))  # follow-up
    job.bridge.note_outcome(_sweep_outcome(3, AlbumOutcomeStatus.applied))  # strong auto...
    job.bridge.note_outcome(
        _sweep_outcome(3, AlbumOutcomeStatus.needs_dup_resolution)
    )  # ...rescinded into the bank as a duplicate

    state = reg.state("sweep-job")
    assert state.sweep is not None
    assert state.sweep.processed == 4  # one per initial outcome (indexes 0-3)
    assert state.sweep.auto_applied == 1  # ONLY the follow-up (album 0 landed)
    assert state.sweep.banked == 3  # uncertain + no_match + duplicate
    assert state.sweep.current_folder == "/library/album3"
    assert state.albums == []  # counters, never the O(n) feed
    assert job.albums == {}  # nothing accumulated on the job either


def test_sweep_skipped_known_tracks_bridge_counter() -> None:
    reg = ImportJobRegistry()
    job = _install_sweep_job(reg)
    job.bridge.note_known_skip()
    job.bridge.note_known_skip()
    state = reg.state("sweep-job")
    assert state.sweep is not None
    assert state.sweep.skipped_known == 2


def test_request_pause_flags_sweep_job_and_is_idempotent() -> None:
    reg = ImportJobRegistry()
    job = _install_sweep_job(reg)
    reg.request_pause("sweep-job")
    assert job.bridge.pause_requested() is True
    assert job.sweep is not None and job.sweep.paused is True
    reg.request_pause("sweep-job")  # second pause while active: no raise


def test_request_pause_unknown_job_raises_keyerror() -> None:
    reg = ImportJobRegistry()
    with pytest.raises(KeyError):
        reg.request_pause("nope")


def test_request_pause_non_sweep_raises_runtimeerror() -> None:
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry()
    reg._job = ImportJob(id="manual-job", bridge=ImportBridge())
    with pytest.raises(RuntimeError):
        reg.request_pause("manual-job")


def test_request_pause_finished_sweep_raises_runtimeerror() -> None:
    reg = ImportJobRegistry()
    job = _install_sweep_job(reg)
    job.phase = ImportPhase.done
    with pytest.raises(RuntimeError):
        reg.request_pause("sweep-job")


def test_sweep_summary_reports_counters_and_pause() -> None:
    reg = ImportJobRegistry()
    job = _install_sweep_job(reg)
    job.bridge.note_outcome(_sweep_outcome(0, AlbumOutcomeStatus.applied))
    job.bridge.note_outcome(_sweep_outcome(0, AlbumOutcomeStatus.applied, album_id=7))
    job.bridge.note_outcome(_sweep_outcome(1, AlbumOutcomeStatus.needs_review))
    job.bridge.note_known_skip()
    reg.request_pause("sweep-job")
    reg._on_finish("sweep-job")
    state = reg.state("sweep-job")
    assert state.phase is ImportPhase.done
    assert state.summary == (
        "swept 2, auto-applied 1, banked 1, skipped 1 already imported - paused"
    )


def test_active_status_carries_sweep_block() -> None:
    reg = ImportJobRegistry()
    _install_sweep_job(reg)  # phase defaults to scanning (active)
    status = reg.active_status()
    assert status.active is True
    assert status.origin == "sweep"
    assert status.sweep is not None
