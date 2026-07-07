"""Bank apply runner — hermetic suite (FakeImportRunner registry, tmp bank).

Follows the acquisition-queue suite's posture: tiny poll intervals, ALWAYS
``runner.stop()`` in a ``finally`` so no daemon thread leaks into teardown.
``directive_for`` is pure and tested directly with constructed models.
"""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

import pytest

from app.bank import store
from app.bank.apply_runner import BankApplyRunner, directive_for
from app.bank.fingerprint import folder_fingerprint
from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.gates import import_gate_clear
from app.import_jobs.registry import ImportJobRegistry
from app.models.bank import BankApplyDirective, BankDecision, BankItem, BankReason
from app.models.import_models import (
    AlbumChange,
    AlbumOutcome,
    AlbumOutcomeStatus,
    Candidate,
    CandidateOption,
    DuplicateAction,
    DuplicatePrompt,
    ExistingAlbum,
    ImportOptions,
    ImportOrigin,
    IncomingAlbum,
    ParkedAlbum,
    Recommendation,
)

T = TypeVar("T")


def _poll(
    get: Callable[[], T], pred: Callable[[T], bool], *, timeout: float = 2.0, interval: float = 0.01
) -> T:
    deadline = time.monotonic() + timeout
    value = get()
    while time.monotonic() < deadline:
        value = get()
        if pred(value):
            return value
        time.sleep(interval)
    return value


def _folder(tmp_path: Path, name: str = "Album") -> Path:
    folder = tmp_path / "swept" / name
    folder.mkdir(parents=True)
    (folder / "01 Track.mp3").write_bytes(b"x" * 64)
    return folder


def _bank(tmp_path: Path) -> Path:
    return tmp_path / "bank"


def _seed_queued(bank_dir: Path, folder: Path, decision: BankDecision | None = None) -> str:
    item = store.create_item(
        bank_dir,
        folder=str(folder),
        source="sweep",
        reason="no_match",
        fingerprint=folder_fingerprint(folder),
    )
    store.decide_item(bank_dir, item.id, decision or BankDecision(action="asis"))
    return item.id


def _dup_prompt() -> DuplicatePrompt:
    return DuplicatePrompt(
        album_index=0,
        incoming=IncomingAlbum(
            album_artist="A",
            album="B",
            year=None,
            track_count=1,
            format=None,
            bitrate_kbps=None,
            folder="/x",
            has_current_art=False,
        ),
        existing=[
            ExistingAlbum(
                album_id=1,
                album_artist="A",
                album="B",
                year=None,
                track_count=1,
                format=None,
                bitrate_kbps=None,
                folder="/lib/x",
            )
        ],
    )


def _outcome(status: AlbumOutcomeStatus, album_id: int | None = None) -> AlbumOutcome:
    return AlbumOutcome(
        album_index=0,
        folder="/x",
        artist="A",
        album="B",
        recommendation=Recommendation.strong,
        confidence=99.0,
        status=status,
        album_id=album_id,
    )


def _make_runner(bank_dir: Path, reg: ImportJobRegistry) -> BankApplyRunner:
    return BankApplyRunner(
        bank_dir=bank_dir,
        import_registry=reg,
        poll_interval=0.01,
        busy_backoff=0.02,
        idle_poll=0.05,
    )


# ----- directive_for (pure) -----


def _candidate_with_options(release_ids: list[str | None]) -> Candidate:
    change = AlbumChange(artist="A", album="B", year=None, label=None, country=None, media=None)
    return Candidate(
        confidence=80.0,
        recommendation=Recommendation.medium,
        data_source="MusicBrainz",
        data_url=None,
        cover_after_url=None,
        has_current_art=False,
        changed_fields=[],
        album_before=change,
        album_after=change,
        tracks=[],
        missing=[],
        unmatched=[],
        options=[
            CandidateOption(
                index=i,
                confidence=80.0,
                data_source="MusicBrainz",
                disambiguation=None,
                release_id=rid,
            )
            for i, rid in enumerate(release_ids)
        ],
    )


def _queued_item(
    decision: BankDecision,
    *,
    parked: ParkedAlbum | None = None,
    duplicate: DuplicatePrompt | None = None,
) -> BankItem:
    reason: BankReason = (
        "needs_review"
        if parked is not None
        else ("needs_dup_resolution" if duplicate is not None else "no_match")
    )
    now = datetime.now(UTC)
    return BankItem(
        id="a" * 32,
        folder="/x/A",
        source="sweep",
        reason=reason,
        parked=parked,
        duplicate=duplicate,
        fingerprint="f" * 64,
        status="queued",
        decided=decision,
        banked_at=now,
        decided_at=now,
    )


def test_directive_for_apply_picks_decided_options_release_id() -> None:
    parked = ParkedAlbum(
        album_index=0, folder="/x/A", candidate=_candidate_with_options(["rel-0", "rel-1"])
    )
    item = _queued_item(BankDecision(action="apply", candidate_index=1), parked=parked)
    assert directive_for(item) == BankApplyDirective(action="apply", search_id="rel-1")
    # None index = the top candidate; out-of-range falls back to the top too
    # (mirrors _apply_choice's defensive fallback).
    item_top = _queued_item(BankDecision(action="apply"), parked=parked)
    assert directive_for(item_top).search_id == "rel-0"
    item_oob = _queued_item(BankDecision(action="apply", candidate_index=9), parked=parked)
    assert directive_for(item_oob).search_id == "rel-0"


def test_directive_for_apply_without_stored_id_is_unpinned() -> None:
    parked = ParkedAlbum(album_index=0, folder="/x/A", candidate=_candidate_with_options([None]))
    item = _queued_item(BankDecision(action="apply"), parked=parked)
    assert directive_for(item) == BankApplyDirective(action="apply", search_id=None)


def test_directive_for_duplicate_and_passthroughs() -> None:
    dup = _queued_item(
        BankDecision(action="duplicate", duplicate_action=DuplicateAction.skip_new),
        duplicate=_dup_prompt(),
    )
    assert directive_for(dup) == BankApplyDirective(
        action="duplicate", duplicate_action=DuplicateAction.skip_new
    )
    assert directive_for(_queued_item(BankDecision(action="asis"))).action == "asis"
    assert directive_for(_queued_item(BankDecision(action="astracks"))).action == "astracks"


def test_duplicate_directive_pins_selected_release() -> None:
    # "decide once": a duplicate decision on a parked candidate row pins the
    # selected option's release_id so the apply imports the chosen release AND
    # resolves the collision in one pass.
    parked = ParkedAlbum(
        album_index=0, folder="/x/A", candidate=_candidate_with_options(["rel-0", "rel-1"])
    )
    item = _queued_item(
        BankDecision(
            action="duplicate", duplicate_action=DuplicateAction.replace, candidate_index=1
        ),
        parked=parked,
    )
    directive = directive_for(item)
    assert directive.action == "duplicate"
    assert directive.search_id == "rel-1"
    assert directive.duplicate_action == DuplicateAction.replace


def test_duplicate_directive_unpinned_when_no_parked() -> None:
    # A sweep-banked needs_dup_resolution row (parked is None) stays unpinned.
    item = _queued_item(
        BankDecision(action="duplicate", duplicate_action=DuplicateAction.skip_new),
        duplicate=_dup_prompt(),
    )
    directive = directive_for(item)
    assert directive.action == "duplicate"
    assert directive.search_id is None
    assert directive.duplicate_action == DuplicateAction.skip_new


# ----- the runner -----


def test_drains_queued_row_to_done_with_album_id(tmp_path: Path) -> None:
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=77)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_queued(bank, _folder(tmp_path))

    calls: list[tuple[str, ImportOptions | None, ImportOrigin]] = []
    real_start = reg.start

    def spy_start(
        path: str,
        *,
        options: ImportOptions | None = None,
        origin: ImportOrigin = "manual",
        directive: BankApplyDirective | None = None,
    ) -> str:
        calls.append((path, options, origin))
        return real_start(path, options=options, origin=origin, directive=directive)

    reg.start = spy_start  # type: ignore[method-assign]  # test spy delegates to the real start

    runner = _make_runner(bank, reg)
    runner.start()  # the startup kick IS the first loop pass: no poke needed
    try:
        item = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "done",
        )
        assert item is not None
        assert item.album_id == 77
        assert item.error is None
        assert item.resolved_at is not None
        assert fake.received_directive is not None
        assert fake.received_directive.action == "asis"
        # The apply IS an import: through the slot, origin bank_apply, default
        # operation (the worker's in-library guard force-corrects to move when
        # the folder lives under the library root).
        assert calls == [
            (
                str(_bank(tmp_path).parent / "swept" / "Album"),
                ImportOptions(operation="default"),
                "bank_apply",
            )
        ]
    finally:
        runner.stop()


def test_fifo_order_is_decided_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=1)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    folder_a = _folder(tmp_path, "A")
    folder_b = _folder(tmp_path, "B")
    item_a = store.create_item(
        bank,
        folder=str(folder_a),
        source="sweep",
        reason="no_match",
        fingerprint=folder_fingerprint(folder_a),
    )
    item_b = store.create_item(
        bank,
        folder=str(folder_b),
        source="sweep",
        reason="no_match",
        fingerprint=folder_fingerprint(folder_b),
    )
    times = iter(
        [
            datetime(2026, 6, 12, 12, 0, 1, tzinfo=UTC),  # A decided LATER
            datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC),  # B decided EARLIER
        ]
    )
    monkeypatch.setattr(store, "_now", lambda: next(times))
    store.decide_item(bank, item_a.id, BankDecision(action="asis"))
    store.decide_item(bank, item_b.id, BankDecision(action="asis"))
    monkeypatch.undo()  # the runner's own set_status timestamps need the real clock

    order: list[str] = []
    real_start = reg.start

    def spy_start(
        path: str,
        *,
        options: ImportOptions | None = None,
        origin: ImportOrigin = "manual",
        directive: BankApplyDirective | None = None,
    ) -> str:
        order.append(path)
        return real_start(path, options=options, origin=origin, directive=directive)

    reg.start = spy_start  # type: ignore[method-assign]

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        _poll(lambda: len(order), lambda n: n >= 2)
        assert order == [str(folder_b), str(folder_a)]  # decided_at order, not banked
    finally:
        runner.stop()


def test_stale_fingerprint_applies_nothing(tmp_path: Path) -> None:
    fake = FakeImportRunner()
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    folder = _folder(tmp_path)
    item_id = _seed_queued(bank, folder)
    (folder / "02 New.mp3").write_bytes(b"y" * 32)  # the folder changed after banking

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        item = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "stale",
        )
        assert item is not None and item.error is not None
        assert "changed" in item.error
        assert fake.validate_calls == []  # the import was never started
    finally:
        runner.stop()


def test_missing_folder_goes_stale(tmp_path: Path) -> None:
    fake = FakeImportRunner()
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    ghost = tmp_path / "swept" / "Ghost"
    item = store.create_item(
        bank, folder=str(ghost), source="sweep", reason="no_match", fingerprint="f" * 64
    )
    store.decide_item(bank, item.id, BankDecision(action="asis"))

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item.id),
            lambda i: i is not None and i.status == "stale",
        )
        assert got is not None and got.error is not None
        assert "no longer exists" in got.error
        assert fake.validate_calls == []
    finally:
        runner.stop()


def test_unanticipated_duplicate_fails_with_guidance(tmp_path: Path) -> None:
    # The run's feed shows needs_dup_resolution (the session SKIPped rather
    # than auto-resolving): the row fails with re-decide guidance, and a
    # duplicate decision IS re-postable on a failed row (store._DECIDABLE).
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.needs_dup_resolution)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_queued(bank, _folder(tmp_path))

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        item = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "failed",
        )
        assert item is not None and item.error is not None
        assert "duplicate" in item.error
    finally:
        runner.stop()
    requeued = store.decide_item(
        bank, item_id, BankDecision(action="duplicate", duplicate_action=DuplicateAction.skip_new)
    )
    assert requeued is not None and requeued.status == "queued"


def test_duplicate_decision_resolves_done(tmp_path: Path) -> None:
    # A banked dup row applied with its stored duplicate_action: the dup
    # outcome on the feed is the EXPECTED resolution, not a blocker.
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.needs_dup_resolution)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    folder = _folder(tmp_path)
    item = store.create_item(
        bank,
        folder=str(folder),
        source="sweep",
        reason="needs_dup_resolution",
        fingerprint=folder_fingerprint(folder),
        duplicate=_dup_prompt(),
    )
    store.decide_item(
        bank, item.id, BankDecision(action="duplicate", duplicate_action=DuplicateAction.skip_new)
    )

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item.id),
            lambda i: i is not None and i.status == "done",
        )
        assert got is not None
        assert got.album_id is None  # skip_new lands nothing, by the user's choice
        assert fake.received_directive is not None
        assert fake.received_directive.duplicate_action is DuplicateAction.skip_new
    finally:
        runner.stop()


def test_failed_import_is_recorded_retryable(tmp_path: Path) -> None:
    fake = FakeImportRunner(fail_with="boom")
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_queued(bank, _folder(tmp_path))

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        item = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "failed",
        )
        assert item is not None and item.error == "boom"
    finally:
        runner.stop()
    # Retryable per the as-built store: a failed row accepts a new decision.
    requeued = store.decide_item(bank, item_id, BankDecision(action="asis"))
    assert requeued is not None and requeued.status == "queued"


def test_apply_without_album_id_fails_honestly(tmp_path: Path) -> None:
    # The job finished but nothing landed (e.g. a pinned id resolved nothing,
    # the session SKIPped): apply/asis rows must FAIL, never claim done.
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.skipped)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_queued(bank, _folder(tmp_path))

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        item = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "failed",
        )
        assert item is not None and item.error is not None
        assert "no library album" in item.error
    finally:
        runner.stop()


def test_duplicate_decision_without_dup_evidence_fails(tmp_path: Path) -> None:
    # A transient lookup failure: the re-lookup returned zero candidates, the
    # session SKIPped (skipped outcome, no dup prompt ever surfaced), the job
    # still finished phase=done. NOTHING was imported and the duplicate
    # resolution never ran - the row must fail retryably, never claim done.
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.skipped)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    folder = _folder(tmp_path)
    item = store.create_item(
        bank,
        folder=str(folder),
        source="sweep",
        reason="needs_dup_resolution",
        fingerprint=folder_fingerprint(folder),
        duplicate=_dup_prompt(),
    )
    store.decide_item(
        bank, item.id, BankDecision(action="duplicate", duplicate_action=DuplicateAction.skip_new)
    )

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item.id),
            lambda i: i is not None and i.status == "failed",
        )
        assert got is not None and got.status == "failed"
        assert got.error is not None and "imported nothing" in got.error
    finally:
        runner.stop()
    # Retryable: a failed row accepts a fresh duplicate decision.
    requeued = store.decide_item(
        bank, item.id, BankDecision(action="duplicate", duplicate_action=DuplicateAction.skip_new)
    )
    assert requeued is not None and requeued.status == "queued"


def test_duplicate_decision_done_when_album_landed_without_prompt(tmp_path: Path) -> None:
    # The library copy vanished between banking and apply: no dup prompt
    # surfaces, the album just imports. The landed album id IS positive
    # evidence - done, carrying the id.
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=33)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    folder = _folder(tmp_path)
    item = store.create_item(
        bank,
        folder=str(folder),
        source="sweep",
        reason="needs_dup_resolution",
        fingerprint=folder_fingerprint(folder),
        duplicate=_dup_prompt(),
    )
    store.decide_item(
        bank, item.id, BankDecision(action="duplicate", duplicate_action=DuplicateAction.keep_both)
    )

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        got = _poll(
            lambda: store.get_item(bank, item.id),
            lambda i: i is not None and i.status == "done",
        )
        assert got is not None and got.status == "done"
        assert got.album_id == 33
    finally:
        runner.stop()


def test_astracks_without_applied_outcome_fails(tmp_path: Path) -> None:
    # The astracks re-lookup hit network trouble: the session emitted a
    # skipped outcome and the job finished done with zero applied outcomes.
    # No singleton ever imported - failed with re-decide guidance, not done.
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.skipped)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_queued(bank, _folder(tmp_path), BankDecision(action="astracks"))

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        item = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "failed",
        )
        assert item is not None and item.status == "failed"
        assert item.error is not None and "imported nothing" in item.error
    finally:
        runner.stop()


def test_astracks_done_without_album_id(tmp_path: Path) -> None:
    # Singleton imports create no album entity: done with album_id None.
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_queued(bank, _folder(tmp_path), BankDecision(action="astracks"))

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        item = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "done",
        )
        assert item is not None and item.album_id is None
    finally:
        runner.stop()


def test_defers_while_gate_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=5)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_queued(bank, _folder(tmp_path))

    monkeypatch.setattr("app.lyrics_jobs.registry.lyrics_backfill_active", lambda: True)
    runner = _make_runner(bank, reg)
    runner.start()
    try:
        time.sleep(0.2)
        assert fake.validate_calls == []  # never started while the gate is shut
        item = store.get_item(bank, item_id)
        assert item is not None and item.status == "queued"  # not even claimed
        monkeypatch.setattr("app.lyrics_jobs.registry.lyrics_backfill_active", lambda: False)
        done = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "done",
        )
        assert done is not None and done.album_id == 5
    finally:
        runner.stop()


def test_slot_toctou_requeues_and_retries(tmp_path: Path) -> None:
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=5)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_queued(bank, _folder(tmp_path))

    real_start = reg.start
    calls = {"n": 0}

    def flaky_start(
        path: str,
        *,
        options: ImportOptions | None = None,
        origin: ImportOrigin = "manual",
        directive: BankApplyDirective | None = None,
    ) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("an import is already running")
        return real_start(path, options=options, origin=origin, directive=directive)

    reg.start = flaky_start  # type: ignore[method-assign]

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        item = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "done",
        )
        assert item is not None
        assert calls["n"] == 2  # reverted to queued, backed off, retried
    finally:
        runner.stop()


def test_claim_race_skips_rebanked_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The history-loss race: between the FIFO pick and the queued->applying
    # claim, the folder is re-banked (upsert resets the row to needs_review,
    # decided=None). The CAS claim must refuse - a blind overwrite would
    # persist applying+decided=None, a row the BankItem validator rejects on
    # every later read (silent permanent loss) - and the drain must move on
    # to the next queued row, never crash.
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=9)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    folder_a = _folder(tmp_path, "A")
    folder_b = _folder(tmp_path, "B")
    raced_id = _seed_queued(bank, folder_a)  # decided first: the FIFO head
    other_id = _seed_queued(bank, folder_b)

    raced = {"done": False}

    def racing_gate(import_registry: ImportJobRegistry, swap_lock: asyncio.Lock | None) -> bool:
        # Fires between next_queued (the pick) and the claim: re-bank row A
        # exactly like a sweep upsert would (changed fingerprint -> reset).
        if not raced["done"]:
            raced["done"] = True
            store.upsert_by_folder(
                bank,
                folder=str(folder_a),
                source="sweep",
                reason="no_match",
                fingerprint="0" * 64,
            )
        return import_gate_clear(import_registry, swap_lock)

    monkeypatch.setattr("app.bank.apply_runner.import_gate_clear", racing_gate)

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        done = _poll(
            lambda: store.get_item(bank, other_id),
            lambda i: i is not None and i.status == "done",
        )
        assert done is not None and done.status == "done"  # the drain continued
        raced_row = store.get_item(bank, raced_id)
        assert raced_row is not None  # still readable - the row was never lost
        assert raced_row.status == "needs_review"  # the re-bank reset survived
        assert raced_row.decided is None
    finally:
        runner.stop()


def test_drain_survives_next_queued_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A raise from the FIFO pick itself (next_queued) must not kill the drain
    # daemon. There is no row to attach an error to, so the drain logs and keeps
    # going; a queued row still applies on a later pass. Before the fix the raise
    # escaped _drain and every queued row was stranded until a process restart.
    fake = FakeImportRunner(applied=[_outcome(AlbumOutcomeStatus.applied, album_id=42)])
    reg = ImportJobRegistry(runner=fake)
    bank = _bank(tmp_path)
    item_id = _seed_queued(bank, _folder(tmp_path))

    real_next_queued = store.next_queued
    raised = {"done": False}

    def flaky_next_queued(bank_dir: Path) -> BankItem | None:
        if not raised["done"]:
            raised["done"] = True
            raise RuntimeError("transient store read failure")
        return real_next_queued(bank_dir)

    monkeypatch.setattr(store, "next_queued", flaky_next_queued)

    runner = _make_runner(bank, reg)
    runner.start()
    try:
        item = _poll(
            lambda: store.get_item(bank, item_id),
            lambda i: i is not None and i.status == "done",
        )
        assert raised["done"]  # the faulty pick actually fired
        assert item is not None and item.status == "done"  # the drain survived it
    finally:
        runner.stop()


def test_stop_is_idempotent(tmp_path: Path) -> None:
    runner = _make_runner(_bank(tmp_path), ImportJobRegistry(runner=FakeImportRunner()))
    runner.start()
    runner.stop()
    runner.stop()  # second stop must not raise
