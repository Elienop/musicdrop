import threading

import pytest

from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import ImportJobRegistry
from app.models.import_api import ImportAlbumStatus, ImportPhase
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
