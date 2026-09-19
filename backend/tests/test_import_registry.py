import contextlib
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from app.beets.import_session import ImportAbortError, InLibraryCopyError
from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import ImportJobRegistry
from app.models.import_api import ImportAlbumStatus, ImportJobState, ImportPhase, SweepStatus
from app.models.import_models import (
    AlbumChange,
    AlbumOutcome,
    AlbumOutcomeStatus,
    Candidate,
    DuplicatePrompt,
    ExistingAlbum,
    ImportAction,
    ImportChoice,
    ImportOptions,
    ImportSearch,
    IncomingAlbum,
    ParkedAlbum,
    Recommendation,
)

if TYPE_CHECKING:  # annotation only — every runtime use is a local import, as elsewhere here
    from app.beets.import_session import ImportBridge


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


def _dup_outcome(index: int) -> AlbumOutcome:
    """The needs_dup_resolution outcome the worker emits BEFORE it parks a prompt."""
    return AlbumOutcome(
        album_index=index,
        folder=f"/music/incoming/album{index}",
        artist="Radiohead",
        album="OK Computer",
        recommendation=Recommendation.strong,
        confidence=0.0,
        status=AlbumOutcomeStatus.needs_dup_resolution,
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


def test_the_forgiven_root_record_counts_accepted_starts_not_attempts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The record is about a filing, so a REFUSED start must not write one.

    ``validate`` runs before the slot claim and before the busy check, so a
    record written there counted attempts: three starts, one of them a 409,
    wrote three records while nothing was filed (measured 2026-09-19, security
    seat L-1). The second start below is that 409.
    """
    runner = FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    runner.validate_forgiven = "/music"
    registry = ImportJobRegistry(runner=runner)

    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        registry.start("/music/incoming")
        with pytest.raises(RuntimeError):
            registry.start("/music/other")

    messages = [r.getMessage() for r in caplog.records]
    filing = [m for m in messages if "filing this import there" in m]
    assert len(filing) == 1, messages
    assert "/music" in filing[0]
    # The refused start still reached the predicate — it is the RECORD that is
    # withheld, not the check.
    assert len(runner.validate_calls) == 2


def test_an_unforgiven_start_writes_no_record(caplog: pytest.LogCaptureFixture) -> None:
    """The control: the ordinary arm reports ``None`` and logs nothing."""
    runner = FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    registry = ImportJobRegistry(runner=runner)

    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        registry.start("/music/incoming")

    assert [r.getMessage() for r in caplog.records if "filing this import" in r.getMessage()] == []


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
    choice = ImportChoice(action=ImportAction.apply)
    with pytest.raises(KeyError):
        registry.record_choice(job_id, 99, choice)


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
    choice = ImportChoice(action=ImportAction.skip)
    registry.record_choice(job_id, 0, choice)
    with pytest.raises(RuntimeError):
        registry.record_choice(job_id, 0, choice)


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


def test_mixed_run_counts_the_landed_album_and_owns_the_lost_one() -> None:
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
    progress = registry.state(job_id).progress
    assert progress.applied == 1
    assert progress.skipped == 0
    assert progress.not_landed == 1


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
    # Not merely un-done: the run is still the registry's live job, so the
    # stale callback finished nothing.
    assert registry.active_status().active is True


def test_a_resolved_skip_counts_skipped_and_never_applied() -> None:
    # A parked album the user resolved with skip counts toward progress.skipped
    # and never toward progress.applied.
    fake = FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    registry = ImportJobRegistry(runner=fake)
    job_id = registry.start("/music/incoming")
    _poll(lambda: registry.state(job_id).albums, lambda rows: len(rows) == 1)
    registry.record_choice(job_id, 0, ImportChoice(action=ImportAction.skip))
    _poll(lambda: registry.state(job_id).phase, lambda p: p is ImportPhase.done)
    state = registry.state(job_id)
    assert state.progress.skipped == 1
    assert state.progress.applied == 0


def test_a_decided_skip_does_not_inflate_the_applied_count() -> None:
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
    assert state.progress.skipped == 1


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


_NOTHING_IMPORTED = "Replace failed while moving the old copy to Trash. Nothing was imported."


def test_a_noted_row_is_not_counted_as_imported_mid_run() -> None:
    """A row whose outcome carries a note imported NOTHING, in every phase.

    The attended shape: the user answered the duplicate prompt with Replace, the
    hook refused and answered beets SKIP, and the only emitter of a note
    (``_replace_refused``) does that before ``task.add`` — so no album id can
    ever follow. Measured before the fix: mid-run this row read
    ``progress.applied == 1`` and ``did_not_land=False``, so the page headline
    said "1 album imported" directly above the row's "Nothing was imported." for
    as long as the run lasted; the terminal gate corrected it only at the end.

    The control for the optimistic mid-run count a note does NOT touch is
    ``test_applied_idless_row_counts_as_applied_mid_run`` above — same fixture
    shape, no note.
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob
    from app.models.import_models import DuplicateAction, DuplicateDecision

    reg = ImportJobRegistry()
    job = ImportJob(id="noted", bridge=ImportBridge(), phase=ImportPhase.reviewing)
    reg._job = job  # white-box: install the job in the single slot for state()
    job.bridge.note_outcome(_applied_outcome(0))
    threading.Thread(target=lambda: job.bridge.park_duplicate(_dup_prompt(0)), daemon=True).start()
    _poll(lambda: reg.state("noted").awaiting_decision, lambda v: v is True)
    reg.record_duplicate_decision("noted", 0, DuplicateDecision(action=DuplicateAction.replace))
    job.bridge.note_outcome(
        _applied_outcome(0).model_copy(update={"note": _NOTHING_IMPORTED})
    )  # the refusal's note attaches to the existing row

    state = reg.state("noted")
    assert state.phase is ImportPhase.reviewing  # still running: not the terminal gate answering
    assert state.albums[0].note == _NOTHING_IMPORTED
    assert state.albums[0].album_id is None  # the refusal answered SKIP: no id can follow
    assert state.progress.applied == 0, "a row that imported nothing was counted as imported"
    assert state.progress.not_landed == 1
    assert state.albums[0].did_not_land is True
    assert state.progress.skipped == 0, "it failed; it was not skipped by choice"


def test_a_noted_directive_row_did_not_land_in_both_phases() -> None:
    """The bank-apply shape of the same row, measured: it never reads ``decided``.

    A directive run answers the duplicate itself, so nothing is parked and no
    decision is recorded through the registry: the row reads
    ``needs_dup_resolution`` with ``duplicate_action=None`` and
    ``decided_action=None``. It was therefore never counted as imported — but it
    also read ``did_not_land=False`` and ``not_landed=0`` in BOTH phases, so
    nothing on the wire said the album had not landed.

    It must be counted ONCE. ``ImportPage`` documents the buckets as disjoint (a
    set-aside row "sits in none of the three counters — ``state.set_aside`` is
    its count"), and ``JobFailed`` renders the counts line and a separate
    set-aside sentence, so a row in both reported one album twice. The note wins:
    it landed nothing, and the prompt on the row cannot be answered from the feed
    anyway. The no-note control is
    ``test_a_set_aside_row_without_a_note_counts_as_set_aside``.
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry()
    job = ImportJob(id="directive-noted", bridge=ImportBridge(), phase=ImportPhase.reviewing)
    reg._job = job  # white-box: install the job in the single slot for state()
    job.bridge.note_outcome(_applied_outcome(0))
    job.bridge.note_outcome(_dup_outcome(0))  # the directive run's own upgrade
    job.bridge.note_outcome(_applied_outcome(0).model_copy(update={"note": _NOTHING_IMPORTED}))

    mid = reg.state("directive-noted")
    assert mid.phase is ImportPhase.reviewing
    assert mid.albums[0].status is ImportAlbumStatus.needs_dup_resolution  # the measured shape
    assert mid.progress.applied == 0
    assert mid.progress.not_landed == 1
    assert mid.albums[0].did_not_land is True
    assert mid.progress.skipped == 0
    assert mid.set_aside == 0, "the same album was counted in two buckets"
    assert reg.active_status().needs_review_count == 0, (
        "the probe offered a review decision for an album that imported nothing"
    )

    job.phase = ImportPhase.done
    end = reg.state("directive-noted")
    assert end.progress.not_landed == 1
    assert end.albums[0].did_not_land is True
    assert end.set_aside == 0


def test_a_set_aside_row_without_a_note_counts_as_set_aside() -> None:
    """The control: only a NOTE takes a row out of the set-aside count.

    Same fixture shape as the noted directive row above, minus the note — a
    genuine ``needs_dup_resolution`` row awaiting a decision must still be
    counted by both sites that compute it (``state()`` and the active probe).
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry()
    job = ImportJob(id="set-aside", bridge=ImportBridge(), phase=ImportPhase.reviewing)
    reg._job = job  # white-box: install the job in the single slot for state()
    job.bridge.note_outcome(_applied_outcome(0))
    job.bridge.note_outcome(_dup_outcome(0))

    state = reg.state("set-aside")
    assert state.albums[0].status is ImportAlbumStatus.needs_dup_resolution
    assert state.albums[0].note is None
    assert state.set_aside == 1
    assert state.progress.not_landed == 0
    assert reg.active_status().needs_review_count == 1


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
    options = ImportOptions(operation="copy")
    with pytest.raises(InLibraryCopyError):
        reg.start("/library/Artist", options=options)
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
    options = ImportOptions(operation="copy")
    with pytest.raises(InLibraryCopyError):
        reg.start(["/inbox/A", "/library/Artist"], options=options)
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
    assert (state.sweep.processed, state.sweep.auto_applied, state.sweep.banked) == (0, 0, 0)


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


def test_request_stop_flags_a_sweep_job_and_is_idempotent() -> None:
    reg = ImportJobRegistry()
    job = _install_sweep_job(reg)
    reg.request_stop("sweep-job")
    assert job.bridge.stop_requested() is True
    assert job.stopped is True
    assert job.sweep is not None
    assert job.sweep.stopped is True
    reg.request_stop("sweep-job")  # second stop while active: no raise


def test_request_stop_unknown_job_raises_keyerror() -> None:
    reg = ImportJobRegistry()
    with pytest.raises(KeyError):
        reg.request_stop("nope")


def test_request_stop_accepts_a_manual_job() -> None:
    """The manual run is what Stop this run exists for: one route, every origin.

    (Its predecessor refused everything but a sweep — that refusal is what this
    contract replaces.)
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry()
    job = ImportJob(id="manual-job", bridge=ImportBridge())
    reg._job = job
    reg.request_stop("manual-job")
    assert job.stopped is True
    assert job.bridge.stop_requested() is True
    assert job.sweep is None  # a manual job has no sweep block to flag


def test_request_stop_on_a_finished_job_raises_runtimeerror() -> None:
    reg = ImportJobRegistry()
    job = _install_sweep_job(reg)
    job.phase = ImportPhase.done
    with pytest.raises(RuntimeError):
        reg.request_stop("sweep-job")


def test_a_stop_ends_a_parked_run_and_its_rows_stay_truthful() -> None:
    """B4 + B5, through the registry: album 0 landed, the run parks on album 1,
    and a stop is the only thing that arrives.

    A stop does not route through ``record_choice``, so the row it stopped on
    keeps what the worker gave it — ``needs_review``, counted as set aside. That
    is the existing meaning of "nobody decided this one, the files are still in
    the source", which is exactly what a stop leaves. ``decided`` would claim a
    decision the user never made; ``skipped`` would put it in the bucket of
    albums deliberately passed over.
    """
    from typing import cast

    from app.events.broker import EventBroker

    class _CountingBroker:
        """Duck-typed broker: the registry only calls publish_library_changed."""

        def __init__(self) -> None:
            self.count = 0

        def publish_library_changed(self) -> None:
            self.count += 1

    runner = FakeImportRunner(
        applied=[_applied_outcome(0), _applied_outcome(0).model_copy(update={"album_id": 42})],
        parked=[_parked(1, Recommendation.medium)],
    )
    reg = ImportJobRegistry(runner=runner)
    broker = _CountingBroker()
    reg.attach_event_broker(cast(EventBroker, broker))
    job_id = reg.start("/music/incoming")
    _poll(lambda: reg.state(job_id).awaiting_decision, lambda a: a is True)

    reg.request_stop(job_id)
    _poll(lambda: reg.state(job_id).phase, lambda p: p is ImportPhase.done)

    state = reg.state(job_id)
    assert state.stopped is True
    assert reg.has_active_job() is False  # the single slot is free again
    rows = {row.index: row for row in state.albums}
    assert rows[0].status is ImportAlbumStatus.applied  # what landed stays
    assert rows[0].album_id == 42
    assert rows[1].status is ImportAlbumStatus.needs_review
    assert rows[1].did_not_land is False  # it was never claimed as imported
    # The counts stay disjoint and truthful: one imported, none skipped, one
    # left in the source for a later pass.
    assert (state.progress.applied, state.progress.skipped, state.set_aside) == (1, 0, 1)
    assert state.progress.not_landed == 0
    assert state.awaiting_decision is False  # nobody is waiting on a person
    # A stopped run can have landed albums, so open tabs are told to refetch —
    # the same single event a run that finished on its own emits.
    assert broker.count == 1


def _park_until_released(park: Callable[[], object]) -> None:
    """Park on a worker thread that swallows the stop's abort, as beets' run() does.

    Without the suppression the released worker's ImportAbortError reaches the
    thread hook and pytest turns it into a PytestUnhandledThreadExceptionWarning.
    """
    from beets.importer.session import ImportAbortError

    def target() -> None:
        with contextlib.suppress(ImportAbortError):
            park()

    threading.Thread(target=target, daemon=True).start()


def test_a_finished_job_serves_no_parked_candidate() -> None:
    """A terminal job's stored ParkedAlbum is not a live review screen.

    The choice write already fails there - the worker deleted its reply slot -
    so a served candidate is one whose Apply can only 404. Both reads are gated
    on the phase, with the parked-and-active control first.
    """
    fake = FakeImportRunner(
        parked=[_parked(0, Recommendation.medium)],
        art_sources={0: "/music/incoming/album0/01.flac"},
    )
    reg = ImportJobRegistry(runner=fake)
    job_id = reg.start("/music/incoming")
    _poll(lambda: reg.state(job_id).awaiting_decision, lambda v: v is True)

    # Control: while the worker is parked, every read answers. The cover read
    # returns None (the canned art source is not a real file), which is still
    # the "served" arm — the gate below raises instead.
    assert reg.parked_album(job_id, 0).album_index == 0
    assert reg.candidate(job_id, 0).recommendation is Recommendation.medium
    assert reg.candidate_cover(job_id, 0) is None

    reg.record_choice(job_id, 0, ImportChoice(action=ImportAction.skip))
    _poll(lambda: reg.state(job_id).phase, lambda p: p is ImportPhase.done)

    # The row still holds its ParkedAlbum; the phase is what refuses.
    job = reg.get(job_id)
    assert job is not None
    assert job.albums[0].parked is not None
    for read in (reg.parked_album, reg.candidate, reg.candidate_cover):
        with pytest.raises(KeyError):
            read(job_id, 0)


def test_a_finished_job_serves_no_parked_duplicate() -> None:
    """The duplicate channel's twin of the candidate gate, and its exemption.

    A stop releases the parked prompt, the decision write 404s, and the read
    must follow — otherwise the resolve screen renders live with an Apply that
    cannot land. ``duplicate_prompt`` stays open on the same terminal job: the
    bank apply runner reads it there to store the collision a failed apply
    published (``BankApplyRunner._refresh_stored_duplicate``).
    """
    from app.models.import_models import DuplicateAction, DuplicateDecision

    fake = FakeImportRunner(duplicates=[_dup_prompt(0)])
    reg = ImportJobRegistry(runner=fake)
    job_id = reg.start("/music/incoming")
    _poll(lambda: reg.state(job_id).awaiting_decision, lambda v: v is True)

    # Control: while the worker is parked, the client read answers.
    assert reg.parked_duplicate(job_id, 0).album_index == 0

    reg.request_stop(job_id)
    _poll(lambda: reg.state(job_id).phase, lambda p: p is ImportPhase.done)

    # The row still holds its prompt; the phase is what refuses.
    job = reg.get(job_id)
    assert job is not None
    assert job.albums[0].duplicate is not None
    with pytest.raises(KeyError):
        reg.parked_duplicate(job_id, 0)
    decision = DuplicateDecision(action=DuplicateAction.keep_both)
    with pytest.raises(KeyError):  # ...and the decision was already refused
        reg.record_duplicate_decision(job_id, 0, decision)
    # The runner's exemption: the same prompt, the same terminal job.
    assert reg.duplicate_prompt(job_id, 0).album_index == 0


def test_job_aborted_separates_an_accepted_stop_from_a_raised_abort() -> None:
    """``stopped`` says a stop was accepted; ``job_aborted`` says one was raised.

    The gap is the window after the last abort point, where a stop lands on a
    run that has already placed everything.
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry()
    bridge = ImportBridge()
    reg._job = ImportJob(id="stopping", bridge=bridge, phase=ImportPhase.reviewing)
    assert reg.job_aborted("stopping") is False
    assert reg.job_aborted("no-such-job") is False

    # A stop with nobody parked and no hook left to reach arms the flag and
    # raises nothing.
    reg.request_stop("stopping")
    assert reg._job.stopped is True
    assert reg.job_aborted("stopping") is False

    # A park entered under that stop raises, and the flag follows.
    parked = _parked(0, Recommendation.medium)
    with pytest.raises(ImportAbortError):
        bridge.park(parked)
    assert reg.job_aborted("stopping") is True


def test_state_carries_the_raised_abort_beside_the_accepted_stop() -> None:
    """The done panel reads both: ``stopped`` titles it, ``aborted`` says the
    run was cut short.

    Between them sits the window this field exists for - a stop accepted once
    the last album is past its abort points ends ``done`` with everything
    landed, so the panel may not say the rest stayed in the folder.
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry()
    reg._job = ImportJob(id="landed", bridge=ImportBridge(), phase=ImportPhase.reviewing)
    before = reg.state("landed")
    assert (before.stopped, before.aborted) == (False, False)

    # Accepted with no hook left to reach, then the worker runs out on its own
    # (on_finish sets the phase for a real run). This is the window.
    reg.request_stop("landed")
    reg._job.phase = ImportPhase.done
    whole = reg.state("landed")
    assert (whole.stopped, whole.aborted) == (True, False)

    # The other side: a run still holding an abort point when the stop lands.
    cut = ImportJobRegistry()
    bridge = ImportBridge()
    cut._job = ImportJob(id="cut", bridge=bridge, phase=ImportPhase.reviewing)
    cut.request_stop("cut")
    with pytest.raises(ImportAbortError):
        bridge.park(_parked(0, Recommendation.medium))
    cut._job.phase = ImportPhase.done
    assert (cut.state("cut").stopped, cut.state("cut").aborted) == (True, True)


def test_a_choice_after_a_stop_is_refused_and_the_row_stays_needs_review() -> None:
    """The bridge refuses from the stop on, so no row is marked decided for a
    worker that is already unwinding.

    ``request_stop`` marks the released slot answered; the worker's wake empties
    its queue and only THEN deletes the slot, so in between it looked answerable
    and ``record_choice`` accepted a decision the run can no longer act on. The
    job is still in an ACTIVE phase here - this is not the 404 a choice gets
    once the job is done.
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry()
    bridge = ImportBridge()
    reg._job = ImportJob(id="stopping", bridge=bridge, phase=ImportPhase.reviewing)
    bridge.note_outcome(_needs_review_outcome(0))
    _park_until_released(lambda: bridge.park(_parked(0, Recommendation.medium)))
    _poll(lambda: reg.state("stopping").awaiting_decision, lambda v: v is True)

    reg.request_stop("stopping")

    choice = ImportChoice(action=ImportAction.apply)
    with pytest.raises(KeyError):  # the API maps this to the same 404
        reg.record_choice("stopping", 0, choice)
    state = reg.state("stopping")
    assert state.phase is ImportPhase.reviewing
    assert state.albums[0].status is ImportAlbumStatus.needs_review
    assert state.albums[0].did_not_land is False


def test_a_duplicate_decision_after_a_stop_is_refused() -> None:
    """The duplicate channel's twin, same window, same refusal."""
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob
    from app.models.import_models import DuplicateAction, DuplicateDecision

    reg = ImportJobRegistry()
    bridge = ImportBridge()
    reg._job = ImportJob(id="dup-stopping", bridge=bridge, phase=ImportPhase.reviewing)
    bridge.note_outcome(_dup_outcome(0))
    _park_until_released(lambda: bridge.park_duplicate(_dup_prompt(0)))
    _poll(lambda: reg.state("dup-stopping").awaiting_decision, lambda v: v is True)

    reg.request_stop("dup-stopping")

    decision = DuplicateDecision(action=DuplicateAction.merge)
    with pytest.raises(KeyError):
        reg.record_duplicate_decision("dup-stopping", 0, decision)
    state = reg.state("dup-stopping")
    assert state.albums[0].status is ImportAlbumStatus.needs_dup_resolution
    assert state.albums[0].did_not_land is False


def test_sweep_block_reports_counters_and_the_stop() -> None:
    reg = ImportJobRegistry()
    job = _install_sweep_job(reg)
    job.bridge.note_outcome(_sweep_outcome(0, AlbumOutcomeStatus.applied))
    job.bridge.note_outcome(_sweep_outcome(0, AlbumOutcomeStatus.applied, album_id=7))
    job.bridge.note_outcome(_sweep_outcome(1, AlbumOutcomeStatus.needs_review))
    job.bridge.note_known_skip()
    reg.request_stop("sweep-job")
    reg._on_finish("sweep-job")
    state = reg.state("sweep-job")
    assert state.phase is ImportPhase.done
    assert state.sweep is not None
    # Every counter the sweep earned, plus the stop on both fields it rides:
    # the job-level flag and the sweep block the active probe reads.
    assert state.sweep.processed == 2
    assert state.sweep.auto_applied == 1
    assert state.sweep.banked == 1
    assert state.sweep.skipped_known == 1
    assert state.sweep.stopped is True
    assert state.stopped is True


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
    """Every injected dir reaches the production runner — including playlists_dir,
    which the post-Replace `.m3u8` re-export needs and which must be INJECTED
    (a settings read on the import worker would list the real playlist store)."""
    import app.import_jobs.registry as registry_mod

    captured: dict[str, object] = {}

    class _FakeRunner:
        def __init__(
            self,
            lib: object,
            trash_dir: object = None,
            trash_origins_dir: object = None,
            bank_dir: object = None,
            playlists_dir: object = None,
        ) -> None:
            captured["lib"] = lib
            captured["trash_dir"] = trash_dir
            captured["trash_origins_dir"] = trash_origins_dir
            captured["bank_dir"] = bank_dir
            captured["playlists_dir"] = playlists_dir

    monkeypatch.setattr(registry_mod, "BeetsImportRunner", _FakeRunner)
    reg = ImportJobRegistry()
    lib = object()
    reg.attach_library(
        lib,
        Path("/t"),
        bank_dir=Path("/b"),
        playlists_dir=Path("/p"),
        trash_origins_dir=Path("/o"),
    )
    reg._resolve_runner()
    assert captured == {
        "lib": lib,
        "trash_dir": Path("/t"),
        # Wired as a PAIR with trash_dir: the post-run Replace pass skips
        # entirely unless both reach the session, so a registry that drops this
        # one silently stops trashing replaced copies.
        "trash_origins_dir": Path("/o"),
        "bank_dir": Path("/b"),
        "playlists_dir": Path("/p"),
    }


def test_a_banked_row_applied_after_a_trash_change_uses_the_new_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replace banks a decision, the operator moves ``trash_dir``, then Apply.

    Apply re-attaches the freshly resolved pair
    (``test_config_apply_reattaches_the_trash_origin_store``); this is the other
    half — the runner a LATER ``start`` builds must carry that pair, not the one
    resolved when the row was banked. Pinned at the registry seam rather than
    through a real bank apply: the chain from row to Replace is the apply
    runner's, and only this link was unpinned.
    """
    import app.import_jobs.registry as registry_mod

    seen: list[tuple[object, object]] = []

    class _FakeRunner:
        def __init__(self, lib: object, trash_dir: object = None, *a: object, **kw: object) -> None:
            seen.append((trash_dir, kw.get("trash_origins_dir") or (a[0] if a else None)))

    monkeypatch.setattr(registry_mod, "BeetsImportRunner", _FakeRunner)
    reg = ImportJobRegistry()
    lib = object()
    reg.attach_library(lib, Path("/old/trash"), trash_origins_dir=Path("/old/origins"))
    reg._resolve_runner()  # the row is banked against this pair

    reg.attach_library(lib, Path("/new/trash"), trash_origins_dir=Path("/new/origins"))
    reg._resolve_runner()  # the apply, later

    assert seen == [
        (Path("/old/trash"), Path("/old/origins")),
        (Path("/new/trash"), Path("/new/origins")),
    ]


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


def test_request_stop_accepts_a_bank_apply_job() -> None:
    # Origin stopped being the question: the only refusal left is "no longer
    # active". A bank apply is an import like any other and stops the same way.
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry()
    job = ImportJob(id="apply-job", bridge=ImportBridge(), origin="bank_apply")
    reg._job = job
    reg.request_stop("apply-job")
    assert job.stopped is True


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
    # End-to-end through the public drain/state path: a parked album the user
    # resolved apply but for which NO follow-up album_id ever arrived (the
    # session died before beets ran task.add) must NOT be counted imported on a
    # terminal job — it is flagged did_not_land and counted in not_landed.
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


def test_applied_with_landing_id_counts_imported_and_not_flagged() -> None:
    # The truthful positive: an auto-applied album whose follow-up album_id
    # arrived DID land — counted imported, and never flagged.
    fake = FakeImportRunner(applied=[_applied_outcome(0), _applied_follow_up(0, 7)])
    reg = ImportJobRegistry(runner=fake)
    job_id = reg.start("/music/incoming")
    _poll(lambda: reg.state(job_id).phase, lambda p: p is ImportPhase.done)
    state = reg.state(job_id)
    assert state.progress.applied == 1
    assert state.progress.not_landed == 0
    assert state.albums[0].did_not_land is False


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
    # duplicate merge is exempt only while the row AFTER it landed — that row is
    # the merged task's own (see the next-row tests below).
    row = _dup_row(DuplicateAction.merge)
    assert ImportJobRegistry._did_not_land(row, next_landed=True) is False  # type: ignore[arg-type]
    assert ImportJobRegistry._is_imported(row, next_landed=True) is True  # type: ignore[arg-type]
    for behind in (False, None):
        assert ImportJobRegistry._did_not_land(row, next_landed=behind) is True  # type: ignore[arg-type]
        assert ImportJobRegistry._is_imported(row, next_landed=behind) is False  # type: ignore[arg-type]


def test_the_merge_exemption_asks_the_next_row_not_the_stop() -> None:
    """A merge lands under the MERGED task's row, which is the row after it.

    ``merge`` and ``astracks`` are exempt because neither forms an Album row of
    its own, so an idless row is the normal shape. For merge the exemption is
    only true while something IS behind it: the merged task lands with an id, or
    parks, skips, or is cut short. Neither the stop flag nor the row's position
    answers that — a merged task that parked claims its row before it lands.
    """
    from app.models.import_models import DuplicateAction

    # The row helpers return "object" (_FeedAlbum is registry-private), which is
    # what every arg-type ignore in this block is for — same as the matrix above.
    merged = _dup_row(DuplicateAction.merge)
    for behind in (False, None):
        assert ImportJobRegistry._did_not_land(merged, next_landed=behind) is True  # type: ignore[arg-type]
        assert ImportJobRegistry._is_imported(merged, next_landed=behind) is False  # type: ignore[arg-type]
    # The control: the merged task landed, so the merge did.
    assert ImportJobRegistry._did_not_land(merged, next_landed=True) is False  # type: ignore[arg-type]
    assert ImportJobRegistry._is_imported(merged, next_landed=True) is True  # type: ignore[arg-type]
    # astracks keeps BOTH its exemptions with nothing behind it: the two hooks an
    # expansion's singletons reach hold a stop back until the next album's
    # choose_match
    # (test_an_astracks_expansion_holds_the_stop_until_the_next_albums_hook).
    astracks = _decided_row(ImportAction.astracks)
    assert ImportJobRegistry._did_not_land(astracks, next_landed=None) is False  # type: ignore[arg-type]
    assert ImportJobRegistry._is_imported(astracks, next_landed=None) is True  # type: ignore[arg-type]
    # ...and so does the bank astracks directive's applied/no-id row.
    directive_row = _applied_row()
    directive_verdict = ImportJobRegistry._did_not_land(
        directive_row,  # type: ignore[arg-type]
        astracks_directive=True,
        next_landed=None,
    )
    assert directive_verdict is False
    # What LANDED stays: the merge row's own id outranks the row behind it.
    landed = _dup_row(DuplicateAction.merge, album_id=9)
    assert ImportJobRegistry._did_not_land(landed, next_landed=None) is False  # type: ignore[arg-type]
    assert ImportJobRegistry._is_imported(landed, next_landed=None) is True  # type: ignore[arg-type]
    # A skip-like row is not a landing action, so the not-landed and skipped
    # buckets stay disjoint whatever is behind it.
    for skip_row in (_dup_row(DuplicateAction.skip_new), _decided_row(ImportAction.skip)):
        assert ImportJobRegistry._did_not_land(skip_row, next_landed=None) is False  # type: ignore[arg-type]
        assert ImportJobRegistry._is_skipped(skip_row) is True  # type: ignore[arg-type]


def _stopped_state(
    rows: list["object"], *, stopped: bool = True, phase: ImportPhase = ImportPhase.done
) -> ImportJobState:
    """A terminal job carrying ``rows`` at indexes 0..n-1, read through state()."""
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry()
    job = ImportJob(id="j", bridge=ImportBridge(), phase=phase, stopped=stopped)
    for index, row in enumerate(rows):
        job.albums[index] = row  # type: ignore[assignment]
    reg._job = job
    return reg.state("j")


def _skipped_row() -> "object":
    """A feed row beets SKIPped — what a merged task with zero candidates emits."""
    from app.import_jobs.registry import _FeedAlbum

    outcome = _applied_outcome(1).model_copy(update={"status": AlbumOutcomeStatus.skipped})
    return _FeedAlbum(outcome=outcome, status=ImportAlbumStatus.skipped)


def _needs_review_row() -> "object":
    """A feed row parked for review — what a merged task with a medium match emits."""
    from app.import_jobs.registry import _FeedAlbum

    return _FeedAlbum(outcome=_needs_review_outcome(1), status=ImportAlbumStatus.needs_review)


def test_a_parked_merged_task_leaves_the_merge_row_not_landed() -> None:
    """The merged task parked for review, then the run was stopped — nothing landed.

    Row 1 is the merged task's own: it claimed a feed index at its
    ``choose_match`` and then parked, so the merge row above it is no longer the
    last row. Reading position called this "1 applied" with an empty library.
    """
    from app.models.import_models import DuplicateAction

    state = _stopped_state([_dup_row(DuplicateAction.merge), _needs_review_row()])
    assert (state.progress.applied, state.progress.not_landed) == (0, 1)
    assert state.set_aside == 1
    assert [row.did_not_land for row in state.albums] == [True, False]


def test_a_skipped_merged_task_leaves_the_merge_row_not_landed() -> None:
    """The no-stop twin: the merged task resolved nothing and beets SKIPped it.

    Same verdict, no stop anywhere — which is why the stop flag was the wrong
    key. This half predates the branch.
    """
    from app.models.import_models import DuplicateAction

    state = _stopped_state([_dup_row(DuplicateAction.merge), _skipped_row()], stopped=False)
    assert (state.progress.applied, state.progress.not_landed) == (0, 1)
    assert state.progress.skipped == 1
    assert [row.did_not_land for row in state.albums] == [True, False]


def test_a_crashed_run_reports_its_idless_merge_row_not_landed_either_way() -> None:
    """No row behind the merge: the merged task never reached ``choose_match``.

    The verdict must not depend on whether Stop was clicked before the crash.
    """
    from app.models.import_models import DuplicateAction

    for stopped in (True, False):
        state = _stopped_state(
            [_dup_row(DuplicateAction.merge)], stopped=stopped, phase=ImportPhase.failed
        )
        assert (state.progress.applied, state.progress.not_landed) == (0, 1), stopped
        assert state.albums[0].did_not_land is True, stopped


def test_a_merge_that_landed_before_the_stop_still_counts_imported() -> None:
    """The merged task's own row is proof the merge landed: index 1 carries the id.

    One merged album counts as two applied rows — the merge row and the merged
    task's — which predates this branch.
    """
    from app.models.import_models import DuplicateAction

    state = _stopped_state([_dup_row(DuplicateAction.merge), _applied_row(album_id=9)])
    assert [row.did_not_land for row in state.albums] == [False, False]
    assert (state.progress.applied, state.progress.not_landed) == (2, 0)


def test_a_merged_task_answered_as_tracks_lands_the_merge_above_it() -> None:
    """``next_landed`` is the next row's VERDICT, not "it has an id".

    A merged task answered `as tracks` re-pipelines as singletons and forms no
    Album row, so an id-only reading would call the merge above it not landed.
    """
    from app.models.import_models import DuplicateAction

    state = _stopped_state([_dup_row(DuplicateAction.merge), _decided_row(ImportAction.astracks)])
    assert [row.did_not_land for row in state.albums] == [False, False]
    assert (state.progress.applied, state.progress.not_landed) == (2, 0)


def test_a_stopped_job_still_counts_an_earlier_astracks_album_applied() -> None:
    """The security seat's measured scenario: album 0 answered astracks and landed,
    album 1 still parked, then Stop. The expansion finished, so row 0 is applied.

    Both shapes: the astracks row as the feed's last row (nothing follows it),
    and with a parked row behind it.
    """
    alone = _stopped_state([_decided_row(ImportAction.astracks)])
    assert (alone.progress.applied, alone.progress.not_landed) == (1, 0)
    assert alone.albums[0].did_not_land is False

    pair = _stopped_state([_decided_row(ImportAction.astracks), _needs_review_row()])
    assert (pair.progress.applied, pair.progress.not_landed) == (1, 0)
    assert (pair.progress.needs_review, pair.set_aside) == (1, 1)
    assert [row.did_not_land for row in pair.albums] == [False, False]


def test_did_not_land_is_terminal_gated_mid_run() -> None:
    # Mid-run a decided-apply row's follow-up id can simply trail by one drain,
    # so the "did not land" verdict must NEVER surface on a non-terminal job:
    # the row flag and the not_landed counter both stay quiet until done.
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
    assert state.progress.skipped == 0
    assert state.progress.not_landed == 0


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


# ----- drain invariants pinned one mutant at a time --------------------------
# Each test below exists to kill one specific mutation of
# app/import_jobs/registry.py's _drain_locked passes that SURVIVED the full
# 2363-test suite (adversarial mutation review). See each docstring for the
# exact scenario and the mutant it kills.


def _dup_prompt(index: int) -> DuplicatePrompt:
    """A minimal parked duplicate prompt for index ``index``."""
    return DuplicatePrompt(
        album_index=index,
        incoming=IncomingAlbum(
            album_artist="Radiohead",
            album="OK Computer",
            year=1997,
            track_count=1,
            format=None,
            bitrate_kbps=None,
            folder=f"/music/incoming/album{index}",
            has_current_art=False,
        ),
        existing=[
            ExistingAlbum(
                album_id=1,
                album_artist="Radiohead",
                album="OK Computer",
                year=1997,
                track_count=1,
                format=None,
                bitrate_kbps=None,
                folder="/library/Radiohead/OK Computer",
            )
        ],
    )


def test_drain_replays_pending_parked_before_a_fresh_parked_overrides() -> None:
    """Pending replay must run BEFORE the fresh-parked pass in a drain.

    Scenario: drain #1 popped parked A before index 0's feed row existed and
    buffered it into job.pending_parked. Between the drains the album's
    outcome landed AND a FRESH parked B (different candidate content) reached
    the queue. The pending-replay pass runs first (attaching stale A) and the
    parked drain then overwrites with fresh B — the feed must show B, the
    album the worker is actually parked on.
    Kills: moving _drain_pending_locked AFTER _drain_parked_duplicate_locked
    in _drain_locked (stale A would then clobber the fresh B).
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    bridge = ImportBridge()
    reg = ImportJobRegistry()
    reg._job = ImportJob(id="stale-park", bridge=bridge, phase=ImportPhase.reviewing)
    job = reg._job

    stale_a = _parked(0, Recommendation.medium)
    bridge._out.put(stale_a)  # popped before index 0's outcome exists -> buffered
    reg.drain("stale-park")
    assert job.albums.get(0) is None  # no row yet
    assert job.pending_parked.get(0) is stale_a  # buffered, NOT discarded

    bridge.note_outcome(_needs_review_outcome(0))  # the outcome finally lands
    fresh_b = ParkedAlbum(
        album_index=0,
        folder="/music/incoming/album0-refetched",
        candidate=_candidate(Recommendation.strong),
    )
    bridge._out.put(fresh_b)  # a FRESH park for the same index reaches the queue

    reg.drain("stale-park")  # replay (stale A) must run BEFORE the parked pass
    row = job.albums.get(0)
    assert row is not None
    assert row.parked is fresh_b  # the fresh park wins, not the stale buffer
    assert 0 not in job.pending_parked


def test_needs_review_refresh_preserves_attached_album_id() -> None:
    """A needs_review re-park refresh must keep the attached album_id.

    Scenario: a needs_review row that later received its beets album id
    (the follow-up applied outcome's attach branch) receives a SECOND
    needs_review outcome (a search re-park re-emits with the new match's
    fields). The refresh must replace the row's outcome with the new match
    AND keep the attached album_id.
    Kills: the refresh line's model_copy(update={...}) reduced to a plain
    ``row.outcome = outcome`` (the attached id would silently drop).
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    bridge = ImportBridge()
    reg = ImportJobRegistry()
    reg._job = ImportJob(id="refresh-id", bridge=bridge, phase=ImportPhase.reviewing)
    job = reg._job

    bridge.note_outcome(_needs_review_outcome(0))  # creates the row
    reg.drain("refresh-id")
    row = job.albums.get(0)
    assert row is not None
    assert row.outcome.confidence == 75.5

    bridge.note_outcome(_applied_follow_up(0, 42))  # the attach: the id lands
    reg.drain("refresh-id")
    assert row.outcome.album_id == 42

    re_park = _needs_review_outcome(0).model_copy(
        update={"recommendation": Recommendation.low, "confidence": 40.0}
    )
    bridge.note_outcome(re_park)  # search re-park re-emits a (worse) match
    reg.drain("refresh-id")
    assert row.outcome.recommendation is Recommendation.low  # new match landed
    assert row.outcome.confidence == 40.0
    assert row.outcome.album_id == 42  # AND the attached id survived the refresh


def test_dup_resolution_outcome_upgrades_status_without_refreshing_outcome() -> None:
    """The outcome refresh is scoped to needs_review only.

    Scenario: an existing row receives a needs_dup_resolution outcome. Its
    status must upgrade to needs_dup_resolution but the row must keep its
    ORIGINAL outcome payload — the dup path renders from row.outcome plus
    row.duplicate.
    Kills: widening the needs_review refresh branch to also run for
    needs_dup_resolution (the dup outcome would stomp the original payload).
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    bridge = ImportBridge()
    reg = ImportJobRegistry()
    reg._job = ImportJob(id="dup-scope", bridge=bridge, phase=ImportPhase.reviewing)
    job = reg._job

    bridge.note_outcome(_needs_review_outcome(0))  # creates the row (artist "Radiohead")
    reg.drain("dup-scope")
    row = job.albums.get(0)
    assert row is not None

    dup = _needs_review_outcome(0).model_copy(
        update={
            "status": AlbumOutcomeStatus.needs_dup_resolution,
            "artist": "Different Artist",
            "confidence": 10.0,
        }
    )
    bridge.note_outcome(dup)
    reg.drain("dup-scope")
    assert row.status is ImportAlbumStatus.needs_dup_resolution  # status upgraded
    assert row.outcome.artist == "Radiohead"  # but the ORIGINAL outcome is kept
    assert row.outcome.confidence == 75.5


def test_album_id_attach_keeps_decided_status() -> None:
    """The album-id attach never touches a decided row's status.

    Scenario: a row the user already decided (record_choice marked it
    decided) receives the follow-up applied outcome carrying beets' album
    id. The attach branch must attach the id WITHOUT regressing
    row.status to applied — the decision the feed already reflects
    stays the truth.
    Kills: the attach branch also setting
    ``row.status = ImportAlbumStatus.applied``.
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    bridge = ImportBridge()
    reg = ImportJobRegistry()
    reg._job = ImportJob(id="attach-status", bridge=bridge, phase=ImportPhase.reviewing)
    job = reg._job

    bridge.note_outcome(_needs_review_outcome(0))
    _park_on_a_worker_thread(bridge, _parked(0, Recommendation.medium))
    _poll(lambda: reg.state("attach-status").awaiting_decision, lambda v: v is True)
    reg.record_choice("attach-status", 0, ImportChoice(action=ImportAction.apply))
    row = job.albums.get(0)
    assert row is not None
    assert row.status is ImportAlbumStatus.decided
    assert row.decided_action is ImportAction.apply

    bridge.note_outcome(_applied_follow_up(0, 42))  # the id lands after the decision
    reg.drain("attach-status")
    assert row.outcome.album_id == 42  # attached
    assert row.status is ImportAlbumStatus.decided  # ...without regressing the decision


def test_drain_buffers_a_parked_duplicate_then_replays_it() -> None:
    """A duplicate popped before its row exists is buffered, then replayed.

    Scenario: a duplicate prompt reaches the bridge's duplicate channel with
    NO feed row yet — drain #1 must buffer it into job.pending_duplicate
    (never discard). Once the outcome lands, the next drain's replay must
    attach row.duplicate, set row.art_source (from the bridge's park-time
    record), and flip status to needs_dup_resolution — else the worker
    blocks in park_duplicate() forever and the dup panel 404s, wedging the
    single import slot.
    Kills: (a) the missing-row duplicate branch discarding the prompt
    instead of buffering; (b) dropping the needs_dup_resolution flip in
    the replay.
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    bridge = ImportBridge()
    reg = ImportJobRegistry()
    reg._job = ImportJob(id="dup-race", bridge=bridge, phase=ImportPhase.reviewing)
    job = reg._job

    prompt = _dup_prompt(0)
    art = "/music/incoming/album0/cover.jpg"
    bridge._art_source[0] = art  # park_duplicate records the art source at park time
    bridge._dup_out.put(prompt)  # prompt reaches the channel with no outcome yet
    reg.drain("dup-race")
    assert job.albums.get(0) is None  # no row yet
    assert job.pending_duplicate.get(0) is prompt  # buffered, NOT discarded

    bridge.note_outcome(_applied_outcome(0))  # the album's outcome finally lands
    reg.drain("dup-race")  # replay: attach + art source + status flip
    row = job.albums.get(0)
    assert row is not None
    assert row.duplicate is prompt
    assert row.art_source == art
    assert row.status is ImportAlbumStatus.needs_dup_resolution
    assert 0 not in job.pending_duplicate


def test_buffered_parked_replay_restores_art_source() -> None:
    """The pending-parked replay must set row.art_source, not just row.parked.

    Scenario: the parked was buffered (popped before its row existed) and NO
    fresh park follows — the replay is the ONLY place the row's art source can
    come from, and GET /cover serves from it. Distinct from the ordering test
    above, whose fresh park overwrites art_source and would mask this line.
    Kills: dropping the ``row.art_source = job.bridge.art_source(index)`` line
    in the pending-parked replay.
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    bridge = ImportBridge()
    reg = ImportJobRegistry()
    reg._job = ImportJob(id="replay-art", bridge=bridge, phase=ImportPhase.reviewing)
    job = reg._job

    parked = _parked(0, Recommendation.medium)
    art = "/music/incoming/album0/01.flac"
    bridge._art_source[0] = art  # park() records the art source at park time
    bridge._out.put(parked)  # popped before index 0's outcome exists -> buffered
    reg.drain("replay-art")
    assert job.pending_parked.get(0) is parked  # buffered, NOT discarded

    bridge.note_outcome(_needs_review_outcome(0))  # the outcome finally lands
    reg.drain("replay-art")  # the replay is the only attach path this time
    row = job.albums.get(0)
    assert row is not None
    assert row.parked is parked
    assert row.art_source == art


def test_start_refuses_while_the_attached_library_is_refused() -> None:
    """A refusal recorded at ``attach_library`` stops every import at ``start``.

    Measured on the parent commit: after Apply's backstop 422 an import was
    accepted (202), ran to ``done``, and its files landed under the beets data
    dir. Only the post-import Replace-trash step was neutralised, because the
    registry's store pair was ``None``.

    ``LibraryRefusedError`` is a ``RuntimeError`` so the drain and the two inbox
    routes keep their existing "not now" arms; the routes an operator drives
    catch it first and answer 503.
    """
    from app.import_jobs.registry import ImportJobRegistry, LibraryRefusedError

    runner = FakeImportRunner()
    reg = ImportJobRegistry(runner)
    reg.attach_library(object(), refusal="Apply loaded config.yaml, but T is M.")

    with pytest.raises(LibraryRefusedError, match="but T is M"):
        reg.start("/x")

    assert runner.validate_calls == []  # nothing reached the runner
    assert isinstance(LibraryRefusedError("x"), RuntimeError)

    # A clean attach clears it: the field is assigned on every call.
    reg.attach_library(object())
    assert reg.start("/x")


# ----- elapsed_seconds: a slow import must be distinguishable from a hung one -----


def test_elapsed_seconds_keeps_counting_while_a_job_is_parked() -> None:
    """A job parked awaiting a decision is still "running" for the operator.

    Measured 2026-09-07: a one-track import took ~5 minutes with nothing logged,
    so this number is the only signal separating slow from hung. It must not
    stall the moment the phase leaves ``scanning``.
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry()
    job = ImportJob(id="parked", bridge=ImportBridge(), phase=ImportPhase.reviewing)
    job.started_monotonic = time.monotonic() - 125.0
    reg._job = job

    assert reg.state("parked").elapsed_seconds == 125
    assert job.ended_monotonic is None  # a parked job's clock is still open


def test_elapsed_seconds_is_frozen_once_the_clock_stopped() -> None:
    """A finished job's number must not keep growing after the last poll."""
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry()
    job = ImportJob(id="over", bridge=ImportBridge(), phase=ImportPhase.done)
    # Started five minutes ago, stopped seven seconds in: a reader that ignored
    # ``ended_monotonic`` and used the live clock would answer 300, not 7.
    job.started_monotonic = time.monotonic() - 300.0
    job.ended_monotonic = job.started_monotonic + 7.0
    reg._job = job

    assert reg.state("over").elapsed_seconds == 7


def test_finished_job_stops_its_clock() -> None:
    """``phase=done`` latches the clock, so elapsed measures the RUN, not the wait."""
    reg = ImportJobRegistry(runner=FakeImportRunner(applied=[_applied_outcome(0)]))
    job_id = reg.start("/music/incoming")
    _poll(lambda: reg.state(job_id).phase, lambda p: p is ImportPhase.done)

    job = reg.get(job_id)
    assert job is not None
    assert job.ended_monotonic is not None  # stop_clock ran on the done transition
    # Backdate the START only: the frozen end must carry the whole difference.
    job.started_monotonic = job.ended_monotonic - 42.0
    assert reg.state(job_id).elapsed_seconds == 42


def test_failed_job_stops_its_clock() -> None:
    """The failure path latches too — a failed import's number must not run on."""
    reg = ImportJobRegistry(runner=FakeImportRunner(fail_with="beets blew up"))
    job_id = reg.start("/music/incoming")
    _poll(lambda: reg.state(job_id).phase, lambda p: p is ImportPhase.failed)

    job = reg.get(job_id)
    assert job is not None
    assert job.ended_monotonic is not None
    job.started_monotonic = job.ended_monotonic - 13.0
    assert reg.state(job_id).elapsed_seconds == 13


def test_stop_clock_latches_on_the_first_terminal_transition() -> None:
    """done-then-failed must not extend a number already shown to the client."""
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    job = ImportJob(id="latch", bridge=ImportBridge())
    job.stop_clock()
    first = job.ended_monotonic
    time.sleep(0.01)
    job.stop_clock()

    assert job.ended_monotonic == first


# ----- awaiting_decision: "blocked on a person" is not the same as a set-aside row -----


def _park_on_a_worker_thread(bridge: "ImportBridge", parked: ParkedAlbum) -> None:
    """Park ``parked`` from a worker thread, which then BLOCKS in ``park()``.

    Models the real worker without beets: the thread stays inside ``reply.get()``
    until a choice is pushed, so a test can observe the blocked state instead of
    racing a fake runner that finishes the job microseconds later.
    """
    threading.Thread(target=lambda: bridge.park(parked), daemon=True).start()


def test_awaiting_decision_is_true_while_an_album_is_parked() -> None:
    """The positive case: an attended park really is blocked on a person."""
    fake = FakeImportRunner(parked=[_parked(0, Recommendation.medium)])
    reg = ImportJobRegistry(runner=fake)
    job_id = reg.start("/music/incoming")

    _poll(lambda: reg.state(job_id).awaiting_decision, lambda v: v is True)
    assert reg.state(job_id).albums[0].status is ImportAlbumStatus.needs_review

    reg.record_choice(job_id, 0, ImportChoice(action=ImportAction.apply))
    _poll(lambda: reg.state(job_id).phase, lambda p: p is ImportPhase.done)
    assert reg.state(job_id).awaiting_decision is False


def test_awaiting_decision_is_false_for_an_unattended_duplicate() -> None:
    """An unattended duplicate leaves a needs_dup_resolution row and NOBODY waiting.

    ``resolve_duplicate`` emits the outcome and SKIPs WITHOUT parking, so the
    worker keeps scanning the rest of the folder. Inferring "blocked" from the row
    status (what the page did before this field existed) wedges the whole run:
    spinner gone, poll backed off, for a decision nobody will ever be asked for.
    Phase is asserted ACTIVE so the answer cannot be coming from the terminal gate.
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry()
    job = ImportJob(id="dup-unattended", bridge=ImportBridge(), phase=ImportPhase.reviewing)
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
    assert state.albums[0].status is ImportAlbumStatus.needs_dup_resolution  # the misleading row
    assert state.phase is ImportPhase.reviewing  # still running: not the terminal gate answering
    assert state.awaiting_decision is False  # ...and nothing is blocked
    assert job.bridge.pending_count() == 0  # the worker never entered park_duplicate


def test_awaiting_decision_is_false_during_a_search_relookup() -> None:
    """A ``search`` keeps its row at needs_review while beets re-looks it up.

    ``record_choice`` deliberately does NOT mark a search decided (marking it would
    transiently miscount it as skipped), so the row status still reads "parked"
    during a multi-minute MusicBrainz lookup. The flag must flip the moment the
    worker is unblocked, and flip back when beets re-parks the album.
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry()
    job = ImportJob(id="relookup", bridge=ImportBridge(), phase=ImportPhase.reviewing)
    reg._job = job
    job.bridge.note_outcome(_needs_review_outcome(0))
    _park_on_a_worker_thread(job.bridge, _parked(0, Recommendation.medium))
    _poll(lambda: reg.state("relookup").awaiting_decision, lambda v: v is True)

    reg.record_choice(
        "relookup",
        0,
        ImportChoice(action=ImportAction.search, search=ImportSearch(release_id="rel-1")),
    )

    state = reg.state("relookup")
    assert state.albums[0].status is ImportAlbumStatus.needs_review  # NOT decided, by design
    assert state.phase is ImportPhase.reviewing  # still active: not the terminal gate answering
    # Two things make this False and nothing here picks between them: ``answered``
    # on a standing slot, or a woken worker that already released it. The pin that
    # separates them lives at the bridge, holding the release window open:
    # test_import_session.test_a_park_reads_unanswered_only_until_its_choice_is_delivered.
    assert state.awaiting_decision is False  # beets is working, not the operator

    # ...and the re-park puts the operator back in the loop.
    _park_on_a_worker_thread(job.bridge, _parked(0, Recommendation.medium))
    _poll(lambda: reg.state("relookup").awaiting_decision, lambda v: v is True)
    reg.record_choice("relookup", 0, ImportChoice(action=ImportAction.skip))  # release the thread


def test_awaiting_decision_covers_a_parked_duplicate_prompt() -> None:
    """An ATTENDED duplicate does park — the flag must not be candidate-only."""
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob
    from app.models.import_models import DuplicateAction, DuplicateDecision

    reg = ImportJobRegistry()
    job = ImportJob(id="dup-parked", bridge=ImportBridge(), phase=ImportPhase.reviewing)
    reg._job = job
    job.bridge.note_outcome(_applied_outcome(0))
    prompt = _dup_prompt(0)
    threading.Thread(target=lambda: job.bridge.park_duplicate(prompt), daemon=True).start()
    _poll(lambda: reg.state("dup-parked").awaiting_decision, lambda v: v is True)

    reg.record_duplicate_decision(
        "dup-parked", 0, DuplicateDecision(action=DuplicateAction.keep_both)
    )

    state = reg.state("dup-parked")
    assert state.phase is ImportPhase.reviewing  # still active
    # As in the search re-lookup above: ``answered`` or an already-released slot
    # both give False, and this test does not separate them. The gated pin for
    # this channel is
    # test_awaiting_decision_clears_when_a_duplicate_decision_beats_its_prompt.
    assert state.awaiting_decision is False


def test_awaiting_decision_survives_a_park_buffered_before_its_row() -> None:
    """A park popped before its feed row exists still blocks the worker.

    The buffer branch has no row to hang the payload on, so a row-derived answer
    would report "not waiting" while the worker sits in ``park()`` forever. A
    REAL park drives this (not a bare ``_out.put``): the reply slot the worker
    registers is what the answer is read from, and a fixture without one is not
    a picture of a blocked worker.
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    bridge = ImportBridge()
    reg = ImportJobRegistry()
    job = ImportJob(id="buffered", bridge=bridge, phase=ImportPhase.reviewing)
    reg._job = job

    _park_on_a_worker_thread(bridge, _parked(0, Recommendation.medium))  # no outcome emitted

    def _drain_then_pending() -> dict[int, ParkedAlbum]:
        reg.state("buffered")  # drains; the pop lands in the buffer branch
        return job.pending_parked

    _poll(_drain_then_pending, lambda pending: 0 in pending)
    state = reg.state("buffered")

    assert job.albums == {}  # no row exists
    assert 0 in job.pending_parked  # buffered, not discarded
    assert state.awaiting_decision is True
    reg.record_choice("buffered", 0, ImportChoice(action=ImportAction.skip))  # release the thread


def test_awaiting_decision_is_false_on_a_job_that_died_while_parked() -> None:
    """A failed run has no worker left to be blocked.

    ``_on_error`` sets ``failed`` without touching row status, so a crash during a
    park leaves a parked row behind. Nobody is waiting for the operator then.
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    bridge = ImportBridge()
    reg = ImportJobRegistry()
    job = ImportJob(id="died", bridge=bridge, phase=ImportPhase.reviewing)
    reg._job = job
    bridge.note_outcome(_needs_review_outcome(0))
    _park_on_a_worker_thread(bridge, _parked(0, Recommendation.medium))
    _poll(lambda: reg.state("died").awaiting_decision, lambda v: v is True)  # control: while it ran

    job.phase = ImportPhase.failed
    job.error = "beets blew up"

    assert bridge.has_unanswered_park() is True  # the park is still registered...
    assert reg.state("died").awaiting_decision is False  # ...but the worker is gone
    reg.record_choice("died", 0, ImportChoice(action=ImportAction.skip))  # release the thread


# ----- awaiting_decision: a choice accepted for a park that is not yet QUEUED -----


def test_awaiting_decision_clears_when_a_choice_beats_the_drain_to_the_park() -> None:
    """A park answered before the drain pops it leaves nobody waiting.

    ``park`` registers its reply slot and queues the album in ONE critical
    section, so the client cannot answer an unqueued park — but it can answer a
    queued one before the consumer has drained it. The drain that pops that park
    afterwards must not read the woken worker as blocked: it did, and the flag
    then held until the terminal transition, with the run polling at 10 s and no
    spinner for the rest of the import.
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    bridge = ImportBridge()
    reg = ImportJobRegistry()
    job = ImportJob(id="beaten", bridge=bridge, phase=ImportPhase.reviewing)
    reg._job = job
    finished = threading.Event()

    def worker() -> None:
        # choose_match emits the needs_review outcome BEFORE it parks, so the row
        # the client answers exists first.
        bridge.note_outcome(_needs_review_outcome(0))
        bridge.park(_parked(0, Recommendation.medium))
        finished.set()

    threading.Thread(target=worker, daemon=True).start()
    # pending_count rises inside park's critical section, so seeing it also means
    # the album is already on the channel. state() would drain the park here.
    _poll(lambda: bridge.pending_count(), lambda n: n == 1)

    # Answered at the BRIDGE, so no drain runs between the answer and the pop.
    bridge.push_choice(0, ImportChoice(action=ImportAction.apply))
    assert finished.wait(2.0), "the worker never left park()"

    state = reg.state("beaten")  # this drain pops the already-answered park
    assert state.phase is ImportPhase.reviewing  # still active: not the terminal gate answering
    assert state.awaiting_decision is False
    assert reg.state("beaten").awaiting_decision is False  # ...and it does not stick


def test_awaiting_decision_clears_when_a_decision_beats_the_drain_to_the_prompt() -> None:
    """The duplicate channel's twin: same one-shot pop, same stick."""
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob
    from app.models.import_models import DuplicateAction, DuplicateDecision

    bridge = ImportBridge()
    reg = ImportJobRegistry()
    job = ImportJob(id="dup-beaten", bridge=bridge, phase=ImportPhase.reviewing)
    reg._job = job
    finished = threading.Event()

    def worker() -> None:
        bridge.note_outcome(_dup_outcome(0))
        bridge.park_duplicate(_dup_prompt(0))
        finished.set()

    threading.Thread(target=worker, daemon=True).start()
    _poll(lambda: bridge.pending_count(), lambda n: n == 1)

    bridge.push_duplicate_decision(0, DuplicateDecision(action=DuplicateAction.keep_both))
    assert finished.wait(2.0), "the worker never left park_duplicate()"

    state = reg.state("dup-beaten")  # this drain pops the already-answered prompt
    assert state.phase is ImportPhase.reviewing  # still active: not the terminal gate answering
    assert state.awaiting_decision is False
    assert reg.state("dup-beaten").awaiting_decision is False  # ...and it does not stick


# --- already_known + path on the job state -----------------------------------


def test_already_known_is_reported_for_a_manual_job_and_never_counted_as_skipped() -> None:
    """``already_known`` is read from the bridge for EVERY job, not just a
    sweep: a review import of a folder beets' history holds is the case the
    "Import them again" retry exists for, and it is a manual job.

    It cannot double-count ``skipped``: beets' task factory consults its
    history BEFORE any session hook fires, so a history-skipped folder emits no
    outcome and this drain has no row to count.
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    reg = ImportJobRegistry()
    job = ImportJob(id="known-job", bridge=ImportBridge(), origin="manual")
    reg._job = job  # white-box: install in the single slot (established pattern)
    job.bridge.note_known_skip()
    job.bridge.note_known_skip()

    state = reg.state("known-job")
    assert state.progress.already_known == 2
    assert state.progress.skipped == 0
    assert state.albums == []
    assert state.sweep is None  # not a sweep: the counter is on progress


def test_a_sweep_reports_one_already_known_number_not_two() -> None:
    """One response, one number.

    ``state()`` drains first (which refreshes ``sweep.skipped_known``), releases
    the registry lock and takes it again — and the worker keeps counting in
    between. Reading the bridge a second time for ``progress.already_known``
    could therefore put "3 already known" beside "4 already known" in the same
    body. The sweep's own counter is the one number a sweep reports.
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    class _ClimbingBridge(ImportBridge):
        """A worker that skips one more folder between every read."""

        def known_skips(self) -> int:
            self.note_known_skip()
            return super().known_skips()

    reg = ImportJobRegistry()
    job = ImportJob(id="sweep-job", bridge=_ClimbingBridge(), origin="sweep", sweep=SweepStatus())
    reg._job = job  # white-box: install in the single slot (established pattern)

    state = reg.state("sweep-job")
    assert state.sweep is not None
    assert state.progress.already_known == state.sweep.skipped_known


def test_a_single_folder_start_reports_its_path_and_a_multi_folder_start_does_not() -> None:
    """The Import page re-posts this folder (with ``incremental: false``) after
    a reload, so the job has to carry it. A multi-folder start has no single
    folder to name — the inbox hands its settled folders over individually
    rather than importing their shared parent.
    """
    reg = ImportJobRegistry(runner=FakeImportRunner(applied=[]))
    job_id = reg.start("/downloads/Radiohead - OK Computer")
    _poll(lambda: reg.state(job_id).phase, lambda p: p is ImportPhase.done)
    assert reg.state(job_id).path == "/downloads/Radiohead - OK Computer"

    reg = ImportJobRegistry(runner=FakeImportRunner(applied=[]))
    job_id = reg.start(["/downloads/one", "/downloads/two"])
    _poll(lambda: reg.state(job_id).phase, lambda p: p is ImportPhase.done)
    assert reg.state(job_id).path is None

    # A one-element LIST is still one folder (the inbox's single-row Review).
    reg = ImportJobRegistry(runner=FakeImportRunner(applied=[]))
    job_id = reg.start(["/downloads/one"])
    _poll(lambda: reg.state(job_id).phase, lambda p: p is ImportPhase.done)
    assert reg.state(job_id).path == "/downloads/one"


# --- the Replace-became-Skip note --------------------------------------------


def _replace_note_outcome(index: int, note: str) -> AlbumOutcome:
    """The follow-up the session emits when a Replace could not reach Trash."""
    return _dup_outcome(index).model_copy(update={"note": note})


def test_a_replace_note_reaches_the_feed_row_without_moving_its_status() -> None:
    """The only channel the worker has for "your Replace imported nothing".

    The row is driven to the state the note actually arrives in: the duplicate
    outcome put it at ``needs_dup_resolution``, the user answered ``replace``
    (which marks it ``decided`` and records the action), and only then does the
    session find it cannot dispose of the old copy. So the note has to attach to
    a DECIDED row without dragging its status back — the decision stands, and
    ``needs_dup_resolution`` would put the prompt back in front of a user the
    worker has already moved past.

    Without the attach the sentence is dropped: a note-bearing outcome for a row
    that already exists does not replace ``row.outcome``.
    """
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob
    from app.models.import_models import DuplicateAction, DuplicateDecision

    note = "Replace could not read the old copy's files, so nothing was moved or imported."
    bridge = ImportBridge()
    reg = ImportJobRegistry()
    reg._job = ImportJob(id="note-job", bridge=bridge, phase=ImportPhase.reviewing)
    bridge.note_outcome(_dup_outcome(0))

    # The worker blocks in park_duplicate until the decision arrives, exactly as
    # the attended route does; the note is emitted after it returns.
    answered = threading.Event()

    def worker() -> None:
        bridge.park_duplicate(_dup_prompt(0))
        bridge.note_outcome(_replace_note_outcome(0, note))
        answered.set()

    threading.Thread(target=worker, daemon=True).start()
    deadline = time.monotonic() + 2.0
    while not reg.state("note-job").awaiting_decision and time.monotonic() < deadline:
        time.sleep(0.01)
    assert reg.state("note-job").awaiting_decision, "the prompt never parked"

    reg.record_duplicate_decision("note-job", 0, DuplicateDecision(action=DuplicateAction.replace))
    assert answered.wait(2.0), "the worker never left park_duplicate()"
    decided = reg.state("note-job").albums
    assert [r.status for r in decided] == [ImportAlbumStatus.decided]  # the premise

    rows = reg.drain("note-job")

    assert [r.note for r in rows] == [note]
    assert [r.status for r in rows] == [ImportAlbumStatus.decided]


def test_a_row_carries_no_note_when_nothing_went_wrong() -> None:
    """The control: an ordinary row's note is None, so the field is a signal."""
    from app.beets.import_session import ImportBridge
    from app.import_jobs.registry import ImportJob

    bridge = ImportBridge()
    reg = ImportJobRegistry()
    reg._job = ImportJob(id="clean-job", bridge=bridge, phase=ImportPhase.reviewing)
    bridge.note_outcome(_dup_outcome(0))
    bridge.note_outcome(_applied_outcome(1))

    assert [r.note for r in reg.drain("clean-job")] == [None, None]
