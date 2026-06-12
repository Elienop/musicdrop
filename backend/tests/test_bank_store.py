"""Bank store tests — pure filesystem, no beets, payloads kept None
(ParkedAlbum construction is exercised by its own model/mapping tests)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.bank import store
from app.models.bank import BankDecision


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
    page = store.list_items(_bank(tmp_path), offset=0, limit=3)
    assert [i.id for i in page] == ids[:3]  # banked_at ascending = FIFO review
    rest = store.list_items(_bank(tmp_path), offset=3, limit=3)
    assert [i.id for i in rest] == ids[3:]
    assert store.count_items(_bank(tmp_path)) == 5
    assert store.list_items(_bank(tmp_path), status="ignored") == []
    assert store.count_items(_bank(tmp_path), status="needs_review") == 5


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
    assert item.decided is not None and item.decided.candidate_index == 1
    assert item.decided_at is not None


def test_decide_ignore_resolves_immediately(tmp_path: Path) -> None:
    item_id = _create(tmp_path)
    item = store.decide_item(_bank(tmp_path), item_id, BankDecision(action="ignore"))
    assert item is not None
    assert item.status == "ignored"
    assert item.resolved_at is not None


def test_decide_rejects_wrong_state(tmp_path: Path) -> None:
    item_id = _create(tmp_path)
    store.decide_item(_bank(tmp_path), item_id, BankDecision(action="ignore"))
    with pytest.raises(store.InvalidTransitionError):
        store.decide_item(_bank(tmp_path), item_id, BankDecision(action="asis"))


def test_decide_failed_row_is_retryable(tmp_path: Path) -> None:
    item_id = _create(tmp_path)
    store.set_status(_bank(tmp_path), item_id, "failed", error="boom")
    item = store.decide_item(_bank(tmp_path), item_id, BankDecision(action="asis"))
    assert item is not None and item.status == "queued" and item.error is None


def test_delete_row(tmp_path: Path) -> None:
    item_id = _create(tmp_path)
    assert store.delete_item(_bank(tmp_path), item_id) is True
    assert store.get_item(_bank(tmp_path), item_id) is None
    assert store.delete_item(_bank(tmp_path), item_id) is False


def test_delete_refuses_applying(tmp_path: Path) -> None:
    item_id = _create(tmp_path)
    # An applying row carries the decision that got it there (model invariant).
    store.decide_item(_bank(tmp_path), item_id, BankDecision(action="asis"))
    store.set_status(_bank(tmp_path), item_id, "applying")
    with pytest.raises(store.InvalidTransitionError):
        store.delete_item(_bank(tmp_path), item_id)


def test_bulk_ignore_skips_non_pending(tmp_path: Path) -> None:
    pending = _create(tmp_path)
    decided = _create(tmp_path, folder="/library/A/C")
    store.decide_item(_bank(tmp_path), decided, BankDecision(action="asis"))
    count = store.bulk_ignore(_bank(tmp_path), [pending, decided, "missing-id"])
    assert count == 1
    refreshed = store.get_item(_bank(tmp_path), pending)
    assert refreshed is not None and refreshed.status == "ignored"


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
    assert replaced.source == "inbox" and replaced.artist == "New"
    assert replaced.decided is None and replaced.resolved_at is None


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
    assert head is not None and head.id == queued
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


def test_summary_carries_album_id(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    item_id = _create(tmp_path)
    store.decide_item(bank, item_id, BankDecision(action="asis"))
    store.set_status(bank, item_id, "done", album_id=7)
    [summary] = store.list_items(bank, offset=0, limit=10)
    assert summary.album_id == 7


def test_reconcile_interrupted_applying(tmp_path: Path) -> None:
    item_id = _create(tmp_path)
    # An applying row carries the decision that got it there (model invariant).
    store.decide_item(_bank(tmp_path), item_id, BankDecision(action="asis"))
    store.set_status(_bank(tmp_path), item_id, "applying")
    flipped = store.reconcile_interrupted(_bank(tmp_path))
    assert flipped == 1
    item = store.get_item(_bank(tmp_path), item_id)
    assert item is not None and item.status == "needs_review"
    assert item.error is not None and "interrupted" in item.error
    assert store.reconcile_interrupted(_bank(tmp_path)) == 0
