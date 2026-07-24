import threading
from pathlib import Path

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
    ImportSearch,
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


def _needs_review_outcome(index: int) -> AlbumOutcome:
    return AlbumOutcome(
        album_index=index,
        folder=f"/music/incoming/album{index}",
        artist="Radiohead",
        album="OK Computer",
        recommendation=Recommendation.medium,
        confidence=75.5,
        status=AlbumOutcomeStatus.needs_review,
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
    # Index 0 auto-applies AND its follow-up landing id arrives (a real applied
    # album lands), so it counts imported; index 1 is parked for review.
    fake = FakeImportRunner(
        applied=[_applied_outcome(0), _applied_follow_up(0, 5)],
        parked=[_parked(1, Recommendation.medium)],
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


def test_search_choice_does_not_mark_row_decided() -> None:
    # A search is a re-lookup request, not a decision: the row must stay
    # needs_review (the real worker re-parks it in place). The fake just unblocks
    # on the pushed choice, so the job finishes with the row still needs_review.
    fake = FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 1)

    registry.record_choice(
        job_id,
        0,
        ImportChoice(action=ImportAction.search, search=ImportSearch(release_id="rel-1")),
    )

    _poll(lambda: registry.state(job_id).phase, lambda p: p is ImportPhase.done)
    # NOT decided — a search never resolves the park.
    assert registry.state(job_id).albums[0].status is ImportAlbumStatus.needs_review


def test_needs_review_reemit_refreshes_feed_confidence() -> None:
    # A search re-park emits a fresh needs_review outcome for an album already in
    # the feed with the re-looked-up release's confidence; the feed row must show
    # the new value, not the stale first-match one. Modelled as a first
    # needs_review (75%) for index 0, then a second (88%) for the same index.
    first = AlbumOutcome(
        album_index=0,
        folder="/music/incoming/album0",
        artist="Radiohead",
        album="OK Computer",
        recommendation=Recommendation.medium,
        confidence=75.0,
        status=AlbumOutcomeStatus.needs_review,
    )
    reparked = ParkedAlbum(
        album_index=0,
        folder="/music/incoming/album0",
        candidate=_candidate(Recommendation.medium, confidence=88.0),
    )
    fake = FakeImportRunner(applied=[first], parked=[reparked])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")

    # The re-emit (88%) replaces the first outcome's 75% in the feed row.
    _poll(
        lambda: registry.state(job_id).albums,
        lambda rows: len(rows) == 1 and rows[0].confidence == 88.0,
    )


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
    ("action", "imported", "skipped", "not_landed"),
    [
        # No follow-up id arrives (the fake models a decided album that beets
        # never task.add'd): apply/asis land nothing -> not_landed, not imported.
        (ImportAction.apply, 0, 0, 1),
        (ImportAction.asis, 0, 0, 1),
        # astracks is exempt from the landing veto (singletons carry no id).
        (ImportAction.astracks, 1, 0, 0),
        (ImportAction.skip, 0, 1, 0),
    ],
)
def test_decided_action_buckets_imported_vs_skipped(
    action: ImportAction, imported: int, skipped: int, not_landed: int
) -> None:
    # Guards _APPLY_ACTIONS membership AND the landing veto: astracks counts as
    # imported without an id; skip is skipped; apply/asis WITHOUT a landing id
    # count as neither (did-not-land) — the truthful bucketing.
    fake = FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 1)
    registry.record_choice(job_id, 0, ImportChoice(action=action))
    _poll(lambda: registry.state(job_id).phase, lambda p: p is ImportPhase.done)
    state = registry.state(job_id)
    assert state.progress.applied == imported
    assert state.progress.skipped == skipped
    assert state.progress.not_landed == not_landed


def test_summary_counts_applied_and_decided_truthfully() -> None:
    # Mixed truthful outcome: index 0 auto-applies and its landing id arrives
    # (imported); index 1 is decided apply but no landing id ever comes (the
    # session died before task.add) -> it did not land, not imported.
    fake = FakeImportRunner(
        applied=[_applied_outcome(0), _applied_follow_up(0, 4)],
        parked=[_parked(1, Recommendation.medium)],
    )
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 2)
    registry.record_choice(job_id, 1, ImportChoice(action=ImportAction.apply))
    _poll(lambda: registry.state(job_id).phase, lambda p: p is ImportPhase.done)
    summary = registry.state(job_id).summary
    assert summary is not None
    assert "1 imported" in summary
    assert "0 skipped" in summary
    assert "1 did not land" in summary


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
    # Index 0 auto-applies with its landing id (imported); index 1 is skipped.
    fake = FakeImportRunner(
        applied=[_applied_outcome(0), _applied_follow_up(0, 3)],
        parked=[_parked(1, Recommendation.medium)],
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


def test_applied_idless_row_counts_as_applied_mid_run() -> None:
    # Mid-run a strong auto-apply emits an `applied` outcome whose library album
    # id only arrives at the NEXT choose_match (or run() end). During the whole
    # beets move stage the row is `applied` but idless. On a NON-terminal job the
    # applied count must optimistically include it (progress.applied == 1) and the
    # not_landed veto must not fire yet (not_landed == 0) — otherwise the UI reads
    # "0 albums imported" while a folder is actively landing.
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry()
    job = ImportJob(id="mid-run", bridge=ImportBridge(), phase=ImportPhase.reviewing)
    reg._job = job  # white-box: install the job in the single slot for state()
    job.bridge.note_outcome(_applied_outcome(0))  # applied, album_id still None

    state = reg.state("mid-run")
    assert state.phase is ImportPhase.reviewing  # still non-terminal
    assert state.albums[0].status is ImportAlbumStatus.applied
    assert state.albums[0].album_id is None  # id not yet flushed
    assert state.progress.applied == 1  # counted optimistically mid-run
    assert state.progress.not_landed == 0  # veto stays quiet pre-terminal


def test_applied_idless_row_drops_to_not_landed_at_terminal() -> None:
    # The SAME applied-but-idless row on a TERMINAL job is the crash-before-landing
    # case: every follow-up id has flushed, so a still-idless applied row genuinely
    # never task.add'd. The terminal guard must fire — the row falls out of applied
    # and into not_landed.
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry()
    job = ImportJob(id="never-landed", bridge=ImportBridge(), phase=ImportPhase.done)
    reg._job = job  # white-box: install the job in the single slot for state()
    job.bridge.note_outcome(_applied_outcome(0))  # applied, album_id never arrived

    state = reg.state("never-landed")
    assert state.phase is ImportPhase.done  # terminal
    assert state.progress.applied == 0  # veto fires: no longer counted as applied
    assert state.progress.not_landed == 1  # dropped into the not-landed bucket


def test_applied_landed_row_counts_as_applied_at_terminal() -> None:
    # Regression guard: an applied row WITH a flushed album id on a terminal job
    # stays counted as applied (the veto only targets idless rows).
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry()
    job = ImportJob(id="landed", bridge=ImportBridge(), phase=ImportPhase.done)
    reg._job = job  # white-box: install the job in the single slot for state()
    job.bridge.note_outcome(_applied_outcome(0))  # initial applied outcome
    job.bridge.note_outcome(_applied_follow_up(0, 5))  # follow-up carrying the id

    state = reg.state("landed")
    assert state.phase is ImportPhase.done  # terminal
    assert state.albums[0].album_id == 5
    assert state.progress.applied == 1  # landed row still counted
    assert state.progress.not_landed == 0


def test_drain_buffers_a_parked_popped_before_its_row_exists() -> None:
    # M1 race: a drain's outcome pass can run BEFORE the worker emits an album's
    # needs_review outcome, while its parked pass runs AFTER the worker parked.
    # The one-shot queue then hands the parked to a drain with no feed row yet.
    # It must be BUFFERED (never discarded) and attached on the next drain once
    # the outcome creates the row — else the worker blocks in park() forever,
    # GET candidate 404s, and the single import slot is wedged until restart.
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    bridge = ImportBridge()
    reg = ImportJobRegistry()
    reg._job = ImportJob(id="race", bridge=bridge, phase=ImportPhase.reviewing)
    job = reg._job

    parked = _parked(0, Recommendation.medium)
    bridge._out.put(parked)  # parked reaches the queue with no outcome yet -> no row
    reg.drain("race")  # drain #1: popped before the row exists
    assert job.albums.get(0) is None  # no row yet
    assert job.pending_parked.get(0) is parked  # buffered, NOT discarded

    bridge.note_outcome(_needs_review_outcome(0))  # the worker's outcome finally lands
    reg.drain("race")  # drain #2: outcome creates the row + replay attaches the parked
    row = job.albums.get(0)
    assert row is not None
    assert row.parked is parked  # attached -> candidate renders, slot not wedged
    assert 0 not in job.pending_parked  # buffer cleared


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


def test_start_runner_run_failure_frees_the_slot() -> None:
    # The registry claims the single slot UNDER the lock, then calls runner.run
    # OUTSIDE it to spawn the worker. If that spawn raises synchronously (the OS
    # refusing a new thread, a session-build error), the slot must NOT stay
    # wedged: the exception propagates unchanged AND the slot is freed so a
    # later import can start. Without the fix the failed job stays installed
    # with phase=scanning (an active phase) and every future import 409s
    # forever — the whole import gate deadlocks until a process restart.
    runner = FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    runner.run_error = RuntimeError("can't start new thread")
    reg = ImportJobRegistry(runner=runner)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        reg.start("/music/incoming")
    # The synchronous spawn failure must have released the single slot.
    assert reg.has_active_job() is False
    # ...and a subsequent start with a working runner succeeds (returns an id).
    runner.run_error = None
    job_id = reg.start("/music/incoming")
    assert job_id
    assert reg.has_active_job() is True


def test_start_passes_path_and_options_to_validate() -> None:
    runner = FakeImportRunner()
    reg = ImportJobRegistry(runner=runner)
    opts = ImportOptions(operation="move")
    reg.start("/downloads/Artist", options=opts)
    # A bare string is the single-folder shorthand — normalized to a one-element
    # path list, which is what the runner contract takes.
    assert runner.validate_calls == [(["/downloads/Artist"], opts)]


def test_start_accepts_a_list_of_source_folders() -> None:
    # I1: the inbox review hands over the SETTLED folders individually rather
    # than importing their shared parent, so start() must carry a path LIST
    # through validate + run (beets takes each as its own toppath).
    runner = FakeImportRunner()
    reg = ImportJobRegistry(runner=runner)
    reg.start(["/inbox/A", "/inbox/B"], options=ImportOptions(operation="move"))
    assert runner.validate_calls[0][0] == ["/inbox/A", "/inbox/B"]
    assert runner.received_paths == ["/inbox/A", "/inbox/B"]


def test_start_refuses_when_any_list_member_fails_validation() -> None:
    # One bad member refuses the WHOLE start — a partial import would leave the
    # caller believing every folder was handled, and the slot must stay free.
    runner = FakeImportRunner()
    runner.validate_error = InLibraryCopyError("refused")
    reg = ImportJobRegistry(runner=runner)
    with pytest.raises(InLibraryCopyError):
        reg.start(["/inbox/A", "/library/Artist"], options=ImportOptions(operation="copy"))
    assert reg.has_active_job() is False


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


def test_attach_library_threads_bank_dir_to_resolved_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.import_jobs.registry as registry_mod

    captured: dict[str, object] = {}

    class _FakeRunner:
        def __init__(self, lib: object, trash_dir: object = None, bank_dir: object = None) -> None:
            captured["lib"] = lib
            captured["trash_dir"] = trash_dir
            captured["bank_dir"] = bank_dir

    monkeypatch.setattr(registry_mod, "BeetsImportRunner", _FakeRunner)
    reg = ImportJobRegistry()
    lib = object()
    reg.attach_library(lib, Path("/t"), bank_dir=Path("/b"))
    reg._resolve_runner()
    assert captured == {"lib": lib, "trash_dir": Path("/t"), "bank_dir": Path("/b")}


def test_start_forwards_directive_and_bank_apply_origin() -> None:
    from app.models.bank import BankApplyDirective

    fake = FakeImportRunner(applied=[_applied_outcome(0)])
    reg = ImportJobRegistry(runner=fake)
    directive = BankApplyDirective(action="apply", search_id="rel-1")
    job_id = reg.start("/library/Radiohead/OK Computer", origin="bank_apply", directive=directive)
    _poll(lambda: reg.state(job_id).phase, lambda p: p is ImportPhase.done)
    assert fake.received_directive == directive
    state = reg.state(job_id)
    assert state.origin == "bank_apply"
    assert state.sweep is None  # an apply is a feed job, never a counter job


def test_start_defaults_directive_none() -> None:
    fake = FakeImportRunner(applied=[_applied_outcome(0)])
    reg = ImportJobRegistry(runner=fake)
    job_id = reg.start("/music/incoming")
    _poll(lambda: reg.state(job_id).phase, lambda p: p is ImportPhase.done)
    assert fake.received_directive is None
    assert reg.state(job_id).origin == "manual"


def test_request_pause_refuses_bank_apply_job() -> None:
    # A stale sweep-pause can never reach an apply (fresh bridge per job), and
    # a live pause request against a bank_apply job is refused outright.
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry()
    reg._job = ImportJob(id="apply-job", bridge=ImportBridge(), origin="bank_apply")
    with pytest.raises(RuntimeError):
        reg.request_pause("apply-job")


# ----- landing veto: an album counts as imported only when it actually landed -----


def _decided_row(
    action: ImportAction, *, album_id: int | None = None
) -> "object":  # returns a _FeedAlbum
    """A parked-then-decided feed row (status=decided) carrying an optional
    landing album_id — for white-box helper matrix tests."""
    from app.import_jobs.registry import _FeedAlbum

    outcome = _applied_outcome(0).model_copy(update={"album_id": album_id})
    return _FeedAlbum(outcome=outcome, status=ImportAlbumStatus.decided, decided_action=action)


def _dup_row(action: "object", *, album_id: int | None = None) -> "object":
    """A duplicate-resolved feed row carrying an optional landing album_id."""
    from app.import_jobs.registry import _FeedAlbum

    outcome = _applied_outcome(0).model_copy(update={"album_id": album_id})
    return _FeedAlbum(
        outcome=outcome,
        status=ImportAlbumStatus.decided,
        duplicate_action=action,  # type: ignore[arg-type]  # DuplicateAction, imported at call site
    )


def test_decided_apply_without_landing_id_does_not_count_imported() -> None:
    # End-to-end through the public drain/state/summarize path: a parked album
    # the user resolved apply but for which NO follow-up album_id ever arrived
    # (the session died before beets ran task.add) must NOT be counted imported
    # on a terminal job — it is flagged did_not_land and surfaced in the summary.
    fake = FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    reg = ImportJobRegistry(runner=fake)
    job_id = reg.start("/music/incoming")
    _poll(lambda: reg.state(job_id).albums, lambda rows: len(rows) == 1)
    reg.record_choice(job_id, 0, ImportChoice(action=ImportAction.apply))
    _poll(lambda: reg.state(job_id).phase, lambda p: p is ImportPhase.done)
    state = reg.state(job_id)
    assert state.progress.applied == 0
    assert state.progress.not_landed == 1
    assert state.albums[0].did_not_land is True
    assert state.summary is not None
    assert state.summary.endswith(", 1 did not land")


def test_applied_with_landing_id_counts_imported_and_not_flagged() -> None:
    # The truthful positive: an auto-applied album whose follow-up album_id
    # arrived DID land — counted imported, never flagged, no "did not land" text.
    fake = FakeImportRunner(applied=[_applied_outcome(0), _applied_follow_up(0, 7)])
    reg = ImportJobRegistry(runner=fake)
    job_id = reg.start("/music/incoming")
    _poll(lambda: reg.state(job_id).phase, lambda p: p is ImportPhase.done)
    state = reg.state(job_id)
    assert state.progress.applied == 1
    assert state.progress.not_landed == 0
    assert state.albums[0].did_not_land is False
    assert state.summary is not None
    assert "did not land" not in state.summary


def test_astracks_without_landing_id_still_counts_imported() -> None:
    # astracks is exempt: items re-pipeline as singletons, so no Album row (and
    # thus no album_id) is ever created — yet the tracks DID land.
    fake = FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    reg = ImportJobRegistry(runner=fake)
    job_id = reg.start("/music/incoming")
    _poll(lambda: reg.state(job_id).albums, lambda rows: len(rows) == 1)
    reg.record_choice(job_id, 0, ImportChoice(action=ImportAction.astracks))
    _poll(lambda: reg.state(job_id).phase, lambda p: p is ImportPhase.done)
    state = reg.state(job_id)
    assert state.progress.applied == 1
    assert state.progress.not_landed == 0
    assert state.albums[0].did_not_land is False


def test_did_not_land_helper_matrix() -> None:
    # White-box the _did_not_land / _is_imported verdicts across every landing
    # action, with and without a follow-up album_id.
    from app.models.import_models import DuplicateAction

    # apply / asis with no id -> did not land, not imported.
    for action in (ImportAction.apply, ImportAction.asis):
        row = _decided_row(action)
        assert ImportJobRegistry._did_not_land(row) is True  # type: ignore[arg-type]
        assert ImportJobRegistry._is_imported(row) is False  # type: ignore[arg-type]
    # ...but WITH an id they landed.
    for action in (ImportAction.apply, ImportAction.asis):
        row = _decided_row(action, album_id=9)
        assert ImportJobRegistry._did_not_land(row) is False  # type: ignore[arg-type]
        assert ImportJobRegistry._is_imported(row) is True  # type: ignore[arg-type]
    # astracks is exempt even with no id.
    row = _decided_row(ImportAction.astracks)
    assert ImportJobRegistry._did_not_land(row) is False  # type: ignore[arg-type]
    assert ImportJobRegistry._is_imported(row) is True  # type: ignore[arg-type]
    # duplicate replace / keep_both with no id -> did not land, not imported.
    for dup in (DuplicateAction.replace, DuplicateAction.keep_both):
        row = _dup_row(dup)
        assert ImportJobRegistry._did_not_land(row) is True  # type: ignore[arg-type]
        assert ImportJobRegistry._is_imported(row) is False  # type: ignore[arg-type]
    # duplicate merge is exempt (it lands under the merged task's own row).
    row = _dup_row(DuplicateAction.merge)
    assert ImportJobRegistry._did_not_land(row) is False  # type: ignore[arg-type]
    assert ImportJobRegistry._is_imported(row) is True  # type: ignore[arg-type]


def test_did_not_land_is_terminal_gated_mid_run() -> None:
    # Mid-run a decided-apply row's follow-up id can simply trail by one drain,
    # so the "did not land" verdict must NEVER surface on a non-terminal job:
    # the summary flag and the not_landed counter both stay quiet until done.
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry()
    job = ImportJob(id="mid", bridge=ImportBridge(), phase=ImportPhase.reviewing)
    job.albums[0] = _decided_row(ImportAction.apply)  # type: ignore[assignment]
    reg._job = job
    state = reg.state("mid")
    assert state.albums[0].did_not_land is False
    assert state.progress.not_landed == 0


# ----- bank as-tracks apply: an applied outcome that can never carry an id -----


def _applied_row(*, album_id: int | None = None) -> "object":
    """An auto-applied feed row (status=applied, no dup action) — the exact
    shape a bank astracks apply emits: the directive drives an `applied`
    outcome, yet its singletons re-pipeline and never form an Album row."""
    from app.import_jobs.registry import _FeedAlbum

    outcome = _applied_outcome(0).model_copy(update={"album_id": album_id})
    return _FeedAlbum(outcome=outcome, status=ImportAlbumStatus.applied)


def test_astracks_directive_exempts_applied_no_id_row() -> None:
    # A bank astracks apply's applied/no-id row: exempt from the landing veto
    # ONLY when the job carries the astracks directive.
    row = _applied_row()
    assert ImportJobRegistry._did_not_land(row, astracks_directive=True) is False  # type: ignore[arg-type]
    assert ImportJobRegistry._is_imported(row, astracks_directive=True) is True  # type: ignore[arg-type]
    # Regression guard: the SAME shape WITHOUT the directive is the real
    # crash-before-landing case -> still flagged, still not imported.
    assert ImportJobRegistry._did_not_land(row) is True  # type: ignore[arg-type]
    assert ImportJobRegistry._is_imported(row) is False  # type: ignore[arg-type]


def test_bank_astracks_apply_applied_row_not_flagged_did_not_land() -> None:
    # End-to-end: a bank astracks apply emits an `applied` outcome with no
    # album id (its singletons re-pipeline; no Album row is ever created). At
    # terminal the row must count imported, NOT be flagged did_not_land.
    from app.models.bank import BankApplyDirective

    fake = FakeImportRunner(applied=[_applied_outcome(0)])
    reg = ImportJobRegistry(runner=fake)
    job_id = reg.start(
        "/library/Radiohead/OK Computer",
        origin="bank_apply",
        directive=BankApplyDirective(action="astracks"),
    )
    _poll(lambda: reg.state(job_id).phase, lambda p: p is ImportPhase.done)
    state = reg.state(job_id)
    assert state.albums[0].did_not_land is False
    assert state.progress.applied == 1
    assert state.progress.not_landed == 0
    assert state.summary == "1 imported, 0 skipped"


def test_start_sets_directive_astracks_flag_from_astracks_directive() -> None:
    from app.models.bank import BankApplyDirective

    fake = FakeImportRunner(applied=[_applied_outcome(0)])
    reg = ImportJobRegistry(runner=fake)
    job_id = reg.start(
        "/library/A/B", origin="bank_apply", directive=BankApplyDirective(action="astracks")
    )
    job = reg.get(job_id)
    assert job is not None
    assert job.directive_astracks is True


def test_start_leaves_directive_astracks_false_for_apply_directive() -> None:
    from app.models.bank import BankApplyDirective

    fake = FakeImportRunner(applied=[_applied_outcome(0)])
    reg = ImportJobRegistry(runner=fake)
    job_id = reg.start(
        "/library/A/B",
        origin="bank_apply",
        directive=BankApplyDirective(action="apply", search_id="rel-1"),
    )
    job = reg.get(job_id)
    assert job is not None
    assert job.directive_astracks is False
