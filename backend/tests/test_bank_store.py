"""Bank store tests — pure filesystem, no beets, payloads kept None
(ParkedAlbum construction is exercised by its own model/mapping tests)."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.bank import store
from app.models.bank import BankDecision, BankItem
from app.models.import_models import (
    AlbumChange,
    Candidate,
    DuplicatePrompt,
    IncomingAlbum,
    ParkedAlbum,
    Recommendation,
)


def _bank(tmp_path: Path) -> Path:
    return tmp_path / "bank"


def _create(tmp_path: Path, *, folder: str = "/library/A/B", reason: str = "no_match") -> str:
    item = store.create_item(
        _bank(tmp_path),
        folder=folder,
        source="sweep",
        reason=reason,  # type: ignore[arg-type]  # test passes literal strings
        fingerprint="f" * 64,
        artist="Artist",
        album="Album",
    )
    return item.id


def test_create_then_get_roundtrip(tmp_path: Path) -> None:
    item_id = _create(tmp_path)
    item = store.get_item(_bank(tmp_path), item_id)
    assert item is not None
    assert item.status == "needs_review"
    assert item.folder == "/library/A/B"
    assert item.banked_at  # set by the store
    assert (_bank(tmp_path) / f"{item_id}.json").exists()


def test_get_rejects_traversal_ids(tmp_path: Path) -> None:
    _create(tmp_path)
    assert store.get_item(_bank(tmp_path), "../../etc/passwd") is None
    assert store.get_item(_bank(tmp_path), "nope") is None


def test_list_paginates_and_filters(tmp_path: Path) -> None:
    ids = [_create(tmp_path, folder=f"/library/A/{i}") for i in range(5)]
    newest_first = list(reversed(ids))
    page = store.list_items(_bank(tmp_path), offset=0, limit=3)
    assert [i.id for i in page] == newest_first[:3]  # newest banked first = display order
    rest = store.list_items(_bank(tmp_path), offset=3, limit=3)
    assert [i.id for i in rest] == newest_first[3:]
    assert store.count_items(_bank(tmp_path)) == 5
    assert store.list_items(_bank(tmp_path), status="ignored") == []
    assert store.count_items(_bank(tmp_path), status="needs_review") == 5


def test_list_active_only_excludes_resolved_rows(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    pending = _create(tmp_path, folder="/library/A/pending")
    queued = _create(tmp_path, folder="/library/A/queued")
    store.decide_item(bank, queued, BankDecision(action="asis"))
    applying = _create(tmp_path, folder="/library/A/applying")
    store.decide_item(bank, applying, BankDecision(action="asis"))
    store.set_status(bank, applying, "applying")
    failed = _create(tmp_path, folder="/library/A/failed")
    store.set_status(bank, failed, "failed", error="boom")
    stale = _create(tmp_path, folder="/library/A/stale")
    store.set_status(bank, stale, "stale")
    done = _create(tmp_path, folder="/library/A/done")
    store.decide_item(bank, done, BankDecision(action="asis"))
    store.set_status(bank, done, "applying")
    store.set_status(bank, done, "done", album_id=1)
    ignored = _create(tmp_path, folder="/library/A/ignored")
    store.decide_item(bank, ignored, BankDecision(action="ignore"))

    active = store.list_items(bank, active_only=True, offset=0, limit=50)
    assert {i.id for i in active} == {pending, queued, applying, failed, stale}
    assert store.count_items(bank, active_only=True) == 5
    # The default stays everything — active_only is strictly opt-in.
    assert store.count_items(bank) == 7


def test_status_filter_wins_over_active_only(tmp_path: Path) -> None:
    # The router never sends both, but the store keeps status precedence
    # anyway: a specific filter must return its rows even when resolved.
    bank = _bank(tmp_path)
    _create(tmp_path, folder="/library/A/pending")
    ignored = _create(tmp_path, folder="/library/A/ignored")
    store.decide_item(bank, ignored, BankDecision(action="ignore"))
    rows = store.list_items(bank, status="ignored", active_only=True, offset=0, limit=50)
    assert [i.id for i in rows] == [ignored]
    assert store.count_items(bank, status="ignored", active_only=True) == 1


def test_list_skips_corrupt_rows(tmp_path: Path) -> None:
    _create(tmp_path)
    (_bank(tmp_path) / "garbage.json").write_text("{not json", encoding="utf-8")
    assert len(store.list_items(_bank(tmp_path), offset=0, limit=10)) == 1


def test_decide_apply_queues_row(tmp_path: Path) -> None:
    item_id = _create(tmp_path)
    item = store.decide_item(
        _bank(tmp_path), item_id, BankDecision(action="apply", candidate_index=1)
    )
    assert item is not None
    assert item.status == "queued"
    assert item.decided is not None
    assert item.decided.candidate_index == 1
    assert item.decided_at is not None


def test_decide_ignore_resolves_immediately(tmp_path: Path) -> None:
    item_id = _create(tmp_path)
    item = store.decide_item(_bank(tmp_path), item_id, BankDecision(action="ignore"))
    assert item is not None
    assert item.status == "ignored"
    assert item.resolved_at is not None


def test_decide_rejects_wrong_state(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    item_id = _create(tmp_path)
    store.decide_item(bank, item_id, BankDecision(action="ignore"))
    decision = BankDecision(action="asis")
    with pytest.raises(store.InvalidTransitionError):
        store.decide_item(bank, item_id, decision)


def test_decide_failed_row_is_retryable(tmp_path: Path) -> None:
    item_id = _create(tmp_path)
    store.set_status(_bank(tmp_path), item_id, "failed", error="boom")
    item = store.decide_item(_bank(tmp_path), item_id, BankDecision(action="asis"))
    assert item is not None
    assert item.status == "queued"
    assert item.error is None


def test_delete_row(tmp_path: Path) -> None:
    item_id = _create(tmp_path)
    assert store.delete_item(_bank(tmp_path), item_id) is True
    assert store.get_item(_bank(tmp_path), item_id) is None
    assert store.delete_item(_bank(tmp_path), item_id) is False


def test_delete_refuses_applying(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    item_id = _create(tmp_path)
    # An applying row carries the decision that got it there (model invariant).
    store.decide_item(bank, item_id, BankDecision(action="asis"))
    store.set_status(bank, item_id, "applying")
    with pytest.raises(store.InvalidTransitionError):
        store.delete_item(bank, item_id)


def test_bulk_ignore_skips_non_pending(tmp_path: Path) -> None:
    pending = _create(tmp_path)
    decided = _create(tmp_path, folder="/library/A/C")
    store.decide_item(_bank(tmp_path), decided, BankDecision(action="asis"))
    count = store.bulk_ignore(_bank(tmp_path), [pending, decided, "missing-id"])
    assert count == 1
    refreshed = store.get_item(_bank(tmp_path), pending)
    assert refreshed is not None
    assert refreshed.status == "ignored"


def test_bulk_delete_removes_deletable_skips_applying_and_missing(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    needs_review = _create(tmp_path, folder="/library/A/needs_review")
    failed = _create(tmp_path, folder="/library/A/failed")
    store.set_status(bank, failed, "failed", error="boom")
    done = _create(tmp_path, folder="/library/A/done")
    store.decide_item(bank, done, BankDecision(action="asis"))
    store.set_status(bank, done, "applying")
    store.set_status(bank, done, "done", album_id=1)
    ignored = _create(tmp_path, folder="/library/A/ignored")
    store.decide_item(bank, ignored, BankDecision(action="ignore"))
    stale = _create(tmp_path, folder="/library/A/stale")
    store.set_status(bank, stale, "stale")
    # An applying row carries the decision that got it there (model invariant).
    applying = _create(tmp_path, folder="/library/A/applying")
    store.decide_item(bank, applying, BankDecision(action="asis"))
    store.set_status(bank, applying, "applying")

    count = store.bulk_delete(
        bank,
        [needs_review, failed, done, ignored, stale, applying, "missing-id"],
    )
    assert count == 5  # every deletable row; applying + missing-id skipped
    for gone in (needs_review, failed, done, ignored, stale):
        assert store.get_item(bank, gone) is None
    survivor = store.get_item(bank, applying)
    assert survivor is not None
    assert survivor.status == "applying"


def test_upsert_refreshes_same_fingerprint(tmp_path: Path) -> None:
    first = store.upsert_by_folder(
        _bank(tmp_path),
        folder="/library/X",
        source="sweep",
        reason="no_match",
        fingerprint="f1",
        artist="A",
        album="B",
    )
    again = store.upsert_by_folder(
        _bank(tmp_path),
        folder="/library/X",
        source="sweep",
        reason="no_match",
        fingerprint="f1",
        artist="A",
        album="B",
    )
    assert again.id == first.id  # refreshed, not duplicated
    assert again.banked_at >= first.banked_at
    assert store.count_items(_bank(tmp_path)) == 1


def test_upsert_replaces_changed_fingerprint(tmp_path: Path) -> None:
    first = store.upsert_by_folder(
        _bank(tmp_path),
        folder="/library/X",
        source="sweep",
        reason="no_match",
        fingerprint="f1",
        artist="Old",
    )
    store.decide_item(_bank(tmp_path), first.id, BankDecision(action="ignore"))
    replaced = store.upsert_by_folder(
        _bank(tmp_path),
        folder="/library/X",
        source="inbox",
        reason="no_match",
        fingerprint="f2",
        artist="New",
    )
    assert replaced.id == first.id
    assert replaced.status == "needs_review"  # reset: the folder changed
    assert replaced.fingerprint == "f2"
    assert replaced.source == "inbox"
    assert replaced.artist == "New"
    assert replaced.decided is None
    assert replaced.resolved_at is None


def test_next_queued_is_fifo_by_decided_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bank = _bank(tmp_path)
    first_banked = _create(tmp_path, folder="/x/A")
    second_banked = _create(tmp_path, folder="/x/B")
    # Decide them in REVERSE wall-clock order to prove the FIFO key is
    # decided_at (the spec's apply order), not banked_at (the review order).
    times = iter(
        [
            datetime(2026, 6, 12, 12, 0, 1, tzinfo=UTC),  # first_banked decided LATER
            datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC),  # second_banked decided EARLIER
        ]
    )
    monkeypatch.setattr(store, "_now", lambda: next(times))
    store.decide_item(bank, first_banked, BankDecision(action="asis"))
    store.decide_item(bank, second_banked, BankDecision(action="asis"))
    head = store.next_queued(bank)
    assert head is not None
    assert head.id == second_banked


def test_next_queued_ignores_everything_but_queued(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    _create(tmp_path, folder="/x/A")  # needs_review
    ignored = _create(tmp_path, folder="/x/B")
    store.decide_item(bank, ignored, BankDecision(action="ignore"))
    assert store.next_queued(bank) is None
    queued = _create(tmp_path, folder="/x/C")
    store.decide_item(bank, queued, BankDecision(action="asis"))
    head = store.next_queued(bank)
    assert head is not None
    assert head.id == queued
    store.set_status(bank, queued, "applying")
    assert store.next_queued(bank) is None  # applying rows are claimed, not queued


def test_set_status_done_records_album_id(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    item_id = _create(tmp_path)
    store.decide_item(bank, item_id, BankDecision(action="asis"))
    store.set_status(bank, item_id, "applying")
    done = store.set_status(bank, item_id, "done", album_id=42)
    assert done is not None
    assert done.album_id == 42
    assert done.resolved_at is not None
    assert done.error is None


def test_set_status_without_album_id_leaves_it_alone(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    item_id = _create(tmp_path)
    store.decide_item(bank, item_id, BankDecision(action="asis"))
    failed = store.set_status(bank, item_id, "failed", error="boom")
    assert failed is not None
    assert failed.album_id is None
    assert failed.error == "boom"


def test_set_status_cas_mismatch_returns_none_unchanged(tmp_path: Path) -> None:
    # The history-loss race: a queued row is re-banked (reset to needs_review,
    # decided=None) and a stale runner reference then tries to claim it. With
    # expected given and the status changed underneath, set_status must refuse
    # WITHOUT writing - a blind overwrite would persist applying+decided=None,
    # which the BankItem validator rejects on every later read (row loss).
    bank = _bank(tmp_path)
    item_id = _create(tmp_path)  # needs_review, not queued
    claimed = store.set_status(bank, item_id, "applying", expected="queued")
    assert claimed is None
    unchanged = store.get_item(bank, item_id)
    assert unchanged is not None
    assert unchanged.status == "needs_review"
    assert unchanged.error is None
    assert unchanged.resolved_at is None


def test_set_status_cas_match_transitions(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    item_id = _create(tmp_path)
    store.decide_item(bank, item_id, BankDecision(action="asis"))
    claimed = store.set_status(bank, item_id, "applying", expected="queued")
    assert claimed is not None
    assert claimed.status == "applying"
    persisted = store.get_item(bank, item_id)
    assert persisted is not None
    assert persisted.status == "applying"


def test_summary_carries_album_id(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    item_id = _create(tmp_path)
    store.decide_item(bank, item_id, BankDecision(action="asis"))
    store.set_status(bank, item_id, "done", album_id=7)
    [summary] = store.list_items(bank, offset=0, limit=10)
    assert summary.album_id == 7


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
        existing=[],
    )


def test_store_scans_the_bank_dir_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The perf pin: the in-memory summary index means the whole dir is globbed
    # and parsed EXACTLY once, no matter how many creates/upserts/list calls
    # follow. On the pre-index store every list_items/count_items/upsert
    # re-globbed (O(N^2) across a sweep).
    calls = {"n": 0}
    original_glob = Path.glob

    def counting_glob(self: Path, pattern: str) -> Iterator[Path]:
        calls["n"] += 1
        return original_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", counting_glob)
    bank = _bank(tmp_path)
    for i in range(3):
        store.create_item(
            bank, folder=f"/lib/c{i}", source="sweep", reason="no_match", fingerprint="f" * 64
        )
    for i in range(3):
        store.upsert_by_folder(
            bank, folder=f"/lib/u{i}", source="sweep", reason="no_match", fingerprint="g" * 64
        )
    for _ in range(4):
        store.list_page(bank, offset=0, limit=50)
        store.list_items(bank, offset=0, limit=50)
        store.count_items(bank)
    assert calls["n"] == 1


def test_list_page_reports_total_and_total_all(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    pending = _create(tmp_path, folder="/l/pending")
    ignored = _create(tmp_path, folder="/l/ignored")
    store.decide_item(bank, ignored, BankDecision(action="ignore"))
    # active view: only the pending row; total_all counts BOTH rows.
    page, total, total_all = store.list_page(bank, active_only=True, offset=0, limit=50)
    assert [s.id for s in page] == [pending]
    assert total == 1
    assert total_all == 2
    # status filter narrows total but total_all is unconditional.
    _, total_ignored, total_all2 = store.list_page(bank, status="ignored", offset=0, limit=50)
    assert total_ignored == 1
    assert total_all2 == 2


def test_list_page_reason_filter(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    a = _create(tmp_path, folder="/l/a", reason="no_match")
    b = _create(tmp_path, folder="/l/b", reason="no_match")
    dup = store.create_item(
        bank,
        folder="/l/dup",
        source="sweep",
        reason="needs_dup_resolution",
        fingerprint="f" * 64,
        duplicate=_dup_prompt(),
    )
    page, total, total_all = store.list_page(bank, reason="no_match", offset=0, limit=50)
    assert {s.id for s in page} == {a, b}
    assert total == 2
    assert total_all == 3
    dup_page, dup_total, _ = store.list_page(
        bank, reason="needs_dup_resolution", offset=0, limit=50
    )
    assert [s.id for s in dup_page] == [dup.id]
    assert dup_total == 1


def test_list_page_reason_ands_with_active_only(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    active_dup = store.create_item(
        bank,
        folder="/l/active",
        source="sweep",
        reason="needs_dup_resolution",
        fingerprint="f" * 64,
        duplicate=_dup_prompt(),
    )
    resolved_dup = store.create_item(
        bank,
        folder="/l/resolved",
        source="sweep",
        reason="needs_dup_resolution",
        fingerprint="f" * 64,
        duplicate=_dup_prompt(),
    )
    store.decide_item(bank, resolved_dup.id, BankDecision(action="ignore"))
    _create(tmp_path, folder="/l/nomatch", reason="no_match")  # active but wrong reason
    page, total, total_all = store.list_page(
        bank, active_only=True, reason="needs_dup_resolution", offset=0, limit=50
    )
    assert [s.id for s in page] == [active_dup.id]
    assert total == 1
    assert total_all == 3


def test_list_page_newest_banked_first_and_paging(tmp_path: Path) -> None:
    """Display order is newest banked first (the Review page reads top-down);
    the APPLY order stays oldest-decided-first via next_queued — separate path.
    """
    ids = [_create(tmp_path, folder=f"/l/{i}") for i in range(5)]
    newest_first = list(reversed(ids))
    first, total, total_all = store.list_page(tmp_path / "bank", offset=0, limit=2)
    assert [s.id for s in first] == newest_first[:2]
    assert total == 5
    assert total_all == 5
    rest, _, _ = store.list_page(tmp_path / "bank", offset=2, limit=10)
    assert [s.id for s in rest] == newest_first[2:]


def test_reset_bank_index_reveals_externally_written_row(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    seeded = _create(tmp_path, folder="/l/seeded")  # builds the index
    external = BankItem(
        id="a" * 32,
        folder="/l/external",
        source="manual",
        reason="no_match",
        fingerprint="x" * 64,
        status="needs_review",
        banked_at=store._now(),
    )
    (bank / f"{external.id}.json").write_text(external.model_dump_json(), encoding="utf-8")
    # The store is the single writer: an out-of-band file is invisible until reset.
    before = {s.id for s in store.list_items(bank, offset=0, limit=50)}
    assert before == {seeded}
    store.reset_bank_index()
    after = {s.id for s in store.list_items(bank, offset=0, limit=50)}
    assert after == {seeded, external.id}


def test_reconcile_interrupted_applying(tmp_path: Path) -> None:
    item_id = _create(tmp_path)
    # An applying row carries the decision that got it there (model invariant).
    store.decide_item(_bank(tmp_path), item_id, BankDecision(action="asis"))
    store.set_status(_bank(tmp_path), item_id, "applying")
    flipped = store.reconcile_interrupted(_bank(tmp_path))
    assert flipped == 1
    item = store.get_item(_bank(tmp_path), item_id)
    assert item is not None
    assert item.status == "needs_review"
    assert item.error is not None
    assert "interrupted" in item.error
    assert store.reconcile_interrupted(_bank(tmp_path)) == 0


def _parked_payload(revision: int = 1) -> ParkedAlbum:
    change = AlbumChange(artist="A", album="B", year=2000, label=None, country=None, media=None)
    return ParkedAlbum(
        album_index=0,
        folder="/inbox/x",
        candidate=Candidate(
            confidence=90.0,
            recommendation=Recommendation.strong,
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
            options=[],
            search_revision=revision,
        ),
    )


def test_research_item_gives_a_no_match_row_its_first_payload(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    row = store.create_item(
        bank, folder="/inbox/x", source="sweep", reason="no_match", fingerprint="f" * 64
    )
    updated = store.research_item(
        bank,
        row.id,
        parked=_parked_payload(),
        artist="A",
        album="B",
        recommendation="strong",
        confidence=90.0,
    )
    assert updated is not None
    assert updated.reason == "needs_review"
    assert updated.status == "needs_review"
    assert updated.parked is not None
    assert updated.artist == "A"
    # persisted, not just returned
    reread = store.get_item(bank, row.id)
    assert reread is not None
    assert reread.reason == "needs_review"


def test_research_item_preserves_failed_status(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    row = store.create_item(
        bank,
        folder="/inbox/x",
        source="sweep",
        reason="needs_review",
        fingerprint="f" * 64,
        parked=_parked_payload(),
    )
    store.decide_item(bank, row.id, BankDecision(action="asis"))
    store.set_status(bank, row.id, "applying")
    store.set_status(bank, row.id, "failed", error="boom")
    updated = store.research_item(
        bank,
        row.id,
        parked=_parked_payload(revision=2),
        artist="A",
        album="B",
        recommendation="strong",
        confidence=90.0,
    )
    assert updated is not None
    assert updated.status == "failed"  # banner stays honest; user re-decides next
    assert updated.parked is not None
    assert updated.parked.candidate.search_revision == 2


def test_research_item_rejects_undecidable_statuses(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    row = store.create_item(
        bank,
        folder="/inbox/x",
        source="sweep",
        reason="needs_review",
        fingerprint="f" * 64,
        parked=_parked_payload(),
    )
    store.decide_item(bank, row.id, BankDecision(action="asis"))  # -> queued
    parked = _parked_payload()
    with pytest.raises(store.InvalidTransitionError):
        store.research_item(
            bank,
            row.id,
            parked=parked,
            artist=None,
            album=None,
            recommendation=None,
            confidence=None,
        )


def test_research_item_rejects_dup_resolution_rows(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    row = store.create_item(
        bank,
        folder="/inbox/x",
        source="sweep",
        reason="needs_dup_resolution",
        fingerprint="f" * 64,
        duplicate=_dup_prompt(),
    )
    parked = _parked_payload()
    with pytest.raises(store.InvalidTransitionError):
        store.research_item(
            bank,
            row.id,
            parked=parked,
            artist=None,
            album=None,
            recommendation=None,
            confidence=None,
        )


def test_research_item_unknown_id_returns_none(tmp_path: Path) -> None:
    assert (
        store.research_item(
            _bank(tmp_path),
            "0" * 32,
            parked=_parked_payload(),
            artist=None,
            album=None,
            recommendation=None,
            confidence=None,
        )
        is None
    )


def test_rescan_item_rescues_a_stale_row(tmp_path: Path) -> None:
    row = store.create_item(
        tmp_path, folder="/inbox/x", source="sweep", reason="no_match", fingerprint="f" * 64
    )
    store.set_status(tmp_path, row.id, "stale", error="changed")
    updated = store.rescan_item(
        tmp_path,
        row.id,
        fingerprint="a" * 64,
        parked=_parked_payload(),
        artist="A",
        album="B",
        recommendation="strong",
        confidence=90.0,
    )
    assert updated is not None
    assert updated.status == "needs_review"
    assert updated.reason == "needs_review"
    assert updated.fingerprint == "a" * 64
    assert updated.decided is None
    assert updated.error is None
    assert updated.parked is not None


def test_rescan_item_no_candidates_becomes_no_match(tmp_path: Path) -> None:
    row = store.create_item(
        tmp_path,
        folder="/inbox/x",
        source="sweep",
        reason="needs_review",
        fingerprint="f" * 64,
        parked=_parked_payload(),
    )
    updated = store.rescan_item(
        tmp_path,
        row.id,
        fingerprint="a" * 64,
        parked=None,
        artist="A",
        album="B",
        recommendation="none",
        confidence=0.0,
    )
    assert updated is not None
    assert updated.reason == "no_match"
    assert updated.parked is None
    assert updated.fingerprint == "a" * 64
    assert updated.status == "needs_review"


def test_rescan_item_clears_a_duplicate_prompt(tmp_path: Path) -> None:
    row = store.create_item(
        tmp_path,
        folder="/inbox/x",
        source="sweep",
        reason="needs_dup_resolution",
        fingerprint="f" * 64,
        duplicate=_dup_prompt(),
    )
    updated = store.rescan_item(
        tmp_path,
        row.id,
        fingerprint="a" * 64,
        parked=_parked_payload(),
        artist="A",
        album="B",
        recommendation="strong",
        confidence=90.0,
    )
    assert updated is not None
    assert updated.duplicate is None
    assert updated.reason == "needs_review"


def test_rescan_item_preserves_failed_status(tmp_path: Path) -> None:
    row = store.create_item(
        tmp_path,
        folder="/inbox/x",
        source="sweep",
        reason="needs_review",
        fingerprint="f" * 64,
        parked=_parked_payload(),
    )
    store.decide_item(tmp_path, row.id, BankDecision(action="asis"))
    store.set_status(tmp_path, row.id, "applying")
    store.set_status(tmp_path, row.id, "failed", error="boom")
    updated = store.rescan_item(
        tmp_path,
        row.id,
        fingerprint="a" * 64,
        parked=_parked_payload(),
        artist="A",
        album="B",
        recommendation="strong",
        confidence=90.0,
    )
    assert updated is not None
    assert updated.status == "failed"
    assert updated.error == "boom"  # failed rows keep their banner


def test_rescan_item_rejects_settled_statuses(tmp_path: Path) -> None:
    row = store.create_item(
        tmp_path,
        folder="/inbox/x",
        source="sweep",
        reason="needs_review",
        fingerprint="f" * 64,
        parked=_parked_payload(),
    )
    store.decide_item(tmp_path, row.id, BankDecision(action="asis"))  # -> queued
    with pytest.raises(store.InvalidTransitionError):
        store.rescan_item(
            tmp_path,
            row.id,
            fingerprint="a" * 64,
            parked=None,
            artist=None,
            album=None,
            recommendation=None,
            confidence=None,
        )


def _surrogate_folder() -> str:
    # A real filesystem folder whose name is not valid UTF-8: os.fsdecode
    # yields a Python str carrying a lone surrogate.
    return os.fsdecode(b"/music/inbox/Bj\xf6rk")


def test_surrogate_folder_row_persists_and_roundtrips(tmp_path: Path) -> None:
    """The store is lossless: the saved folder reloads to the identical str,
    so os.fsencode gives back the original on-disk bytes."""
    bank = _bank(tmp_path)
    folder = _surrogate_folder()
    item_id = store.create_item(
        bank, folder=folder, source="manual", reason="no_match", fingerprint="s" * 64
    ).id
    item = store.get_item(bank, item_id)
    assert item is not None
    assert item.folder == folder
    assert os.fsencode(item.folder) == b"/music/inbox/Bj\xf6rk"
    # The on-disk text must be plain UTF-8 (a lone surrogate is unencodable
    # as UTF-8; the store escapes it instead of scrubbing it).
    raw = (bank / f"{item_id}.json").read_text(encoding="utf-8")
    assert "\\udcf6" in raw  # escaped as the 6-char \\udcf6 text, not U+FFFD and not raw bytes


def test_surrogate_folder_row_survives_index_reset(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    folder = _surrogate_folder()
    store.create_item(bank, folder=folder, source="manual", reason="no_match", fingerprint="s" * 64)
    store.reset_bank_index()
    page = store.list_items(bank, offset=0, limit=50)
    assert len(page) == 1
    assert page[0].folder == folder
    assert store.count_items(bank) == 1


def test_surrogate_folder_payload_row_roundtrips(tmp_path: Path) -> None:
    """A needs_review row (parked payload present) with a surrogate folder —
    every store sink (write, index build, full-row load) must handle it."""
    bank = _bank(tmp_path)
    folder = _surrogate_folder()
    parking = _parked_payload()
    item_id = store.create_item(
        bank,
        folder=folder,
        source="manual",
        reason="needs_review",
        fingerprint="s" * 64,
        parked=parking,
    ).id
    store.reset_bank_index()
    item = store.get_item(bank, item_id)
    assert item is not None
    assert item.folder == folder
    assert item.parked == parking
    assert [s.folder for s in store.list_items(bank, offset=0, limit=50)] == [folder]


def test_surrogate_folder_row_survives_a_rewrite(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    folder = _surrogate_folder()
    item_id = store.create_item(
        bank, folder=folder, source="manual", reason="no_match", fingerprint="s" * 64
    ).id
    store.decide_item(bank, item_id, BankDecision(action="asis"))
    item = store.get_item(bank, item_id)
    assert item is not None
    assert item.status == "queued"
    assert item.folder == folder


def test_legacy_pydantic_json_row_still_loads(tmp_path: Path) -> None:
    """Backward compat: a row written by the OLD code (model_dump_json) must
    read back unchanged under the new sink."""
    bank = _bank(tmp_path)
    bank.mkdir(parents=True)
    legacy = BankItem(
        id="b" * 32,
        folder="/music/l\u00fcne",  # real UTF-8 folder, as old rows carry it
        source="sweep",
        reason="no_match",
        fingerprint="y" * 64,
        status="needs_review",
        banked_at=store._now(),
    )
    (bank / f"{legacy.id}.json").write_text(legacy.model_dump_json(indent=2), encoding="utf-8")
    item = store.get_item(bank, legacy.id)
    assert item is not None
    assert item.folder == legacy.folder
    assert item.banked_at == legacy.banked_at
    store.reset_bank_index()
    assert [s.id for s in store.list_items(bank, offset=0, limit=50)] == [legacy.id]
