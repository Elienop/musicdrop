"""Bank store tests — the SQLite store and its one-time import of the old
``*.json`` rows. No beets; payloads mostly kept None (ParkedAlbum construction
is exercised by its own model/mapping tests)."""

import gc
import json
import logging
import os
import sqlite3
import threading
import weakref
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from app.bank import store
from app.models.bank import BankDecision, BankItem
from app.models.import_models import (
    AlbumChange,
    Candidate,
    CandidateOption,
    DuplicatePrompt,
    IncomingAlbum,
    ParkedAlbum,
    Recommendation,
)


def _bank(tmp_path: Path) -> Path:
    return tmp_path / "bank"


def _db_rows(bank: Path, sql: str) -> list[tuple[object, ...]]:
    """Read the bank database directly, on a connection of the test's own."""
    conn = sqlite3.connect(bank / store.DB_NAME)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


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
    # One database file; no per-row files any more.
    assert (_bank(tmp_path) / store.DB_NAME).is_file()
    assert list(_bank(tmp_path).glob("*.json")) == []


def test_get_rejects_traversal_ids(tmp_path: Path) -> None:
    _create(tmp_path)
    assert store.get_item(_bank(tmp_path), "../../etc/passwd") is None
    assert store.get_item(_bank(tmp_path), "nope") is None


def _candidate(confidence: float) -> Candidate:
    after = AlbumChange(
        artist="Artist", album="Album", year=2000, label=None, country=None, media=None
    )
    return Candidate(
        confidence=confidence,
        recommendation=Recommendation.medium,
        data_source="MusicBrainz",
        data_url="https://mb/a1",
        cover_after_url=None,
        has_current_art=False,
        changed_fields=[],
        album_before=after,
        album_after=after,
        tracks=[],
        missing=[],
        unmatched=[],
        options=[
            CandidateOption(
                index=0,
                confidence=confidence,
                data_source="MusicBrainz",
                disambiguation=None,
            )
        ],
    )


def _create_parked(tmp_path: Path, *, confidence: float = 75.5) -> str:
    item = store.create_item(
        _bank(tmp_path),
        folder="/library/A/B",
        source="sweep",
        reason="needs_review",
        fingerprint="f" * 64,
        artist="Artist",
        album="Album",
        recommendation="medium",
        confidence=confidence,
        parked=ParkedAlbum(album_index=0, folder="/library/A/B", candidate=_candidate(confidence)),
    )
    return item.id


def test_poisoned_non_finite_row_imports_lists_and_rewrites_strict(tmp_path: Path) -> None:
    """A legacy row file with non-finite floats must import, list, and re-write clean.

    Written by a pre-clamp producer (or hand-poisoned): NaN top-level, Infinity in the
    required nested candidate, -Infinity in options[0]. Healing to 0.0 — not to None:
    a null in a REQUIRED nested float would be a ValidationError, and the import would
    then skip the row — and the list is the only source of ids, so a skipped row is
    unreachable forever.
    """
    bank = _bank(tmp_path)
    bank.mkdir(parents=True)
    item_id = "a" * 32
    obj = BankItem(
        id=item_id,
        folder="/library/A/B",
        source="sweep",
        reason="needs_review",
        fingerprint="f" * 64,
        confidence=75.5,
        parked=ParkedAlbum(album_index=0, folder="/library/A/B", candidate=_candidate(75.5)),
        status="needs_review",
        banked_at=store._now(),
    ).model_dump(mode="json")
    obj["confidence"] = float("nan")
    obj["parked"]["candidate"]["confidence"] = float("inf")
    obj["parked"]["candidate"]["options"][0]["confidence"] = float("-inf")
    raw_path = bank / f"{item_id}.json"
    raw_path.write_text(json.dumps(obj), encoding="utf-8")
    poisoned = raw_path.read_text(encoding="utf-8")
    # All THREE non-finite tokens present on disk ("-Infinity" also satisfies a
    # bare "Infinity" substring check, so each token is pinned by its full form).
    assert '"confidence": NaN' in poisoned
    assert '"confidence": Infinity' in poisoned
    assert '"confidence": -Infinity' in poisoned

    # IMPORTS with every poisoned float healed to the same 0.0 clamp.
    healed = store.get_item(bank, item_id)
    assert healed is not None
    assert healed.confidence == 0.0
    assert healed.parked is not None
    assert healed.parked.candidate.confidence == 0.0
    assert healed.parked.candidate.options[0].confidence == 0.0

    # LISTS — the row did not vanish into the unreadable-row skip.
    listed = [s.id for s in store.list_items(bank, offset=0, limit=50)]
    assert item_id in listed

    # And a re-write through the sink is strict RFC-JSON: healed 0.0s, no tokens.
    updated = store.decide_item(bank, item_id, BankDecision(action="ignore"))
    assert updated is not None
    [(rewritten, summary)] = _db_rows(bank, "SELECT row, summary FROM bank")
    for text in (rewritten, summary):
        assert isinstance(text, str)
        json.loads(
            text,
            parse_constant=lambda token: pytest.fail(f"non-JSON token {token!r} in the stored row"),
        )
    # The legacy file itself is a frozen backup: the import never rewrites it.
    assert raw_path.read_text(encoding="utf-8") == poisoned


def test_sink_refuses_a_non_finite_confidence(tmp_path: Path) -> None:
    """The disk sink fails loud: a non-finite float reaching it is a bug.

    It must raise, not write a bare NaN token that no strict JSON parser reads
    back — the clamp lives at the producer, the heal at the read path.
    """
    bank = _bank(tmp_path)
    with pytest.raises(ValueError, match="Out of range float"):
        store.create_item(
            bank,
            folder="/library/A/B",
            source="sweep",
            reason="no_match",
            fingerprint="f" * 64,
            confidence=float("nan"),
        )


def test_sink_refuses_a_nested_non_finite_candidate_confidence(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    parked = ParkedAlbum(album_index=0, folder="/library/A/B", candidate=_candidate(float("inf")))
    with pytest.raises(ValueError, match="Out of range float"):
        store.create_item(
            bank,
            folder="/library/A/B",
            source="sweep",
            reason="needs_review",
            fingerprint="f" * 64,
            parked=parked,
        )


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


def test_import_skips_corrupt_rows(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    _write_row(bank, "a" * 32, folder="/library/A/B", banked_at=_T1)
    (bank / "garbage.json").write_text("{not json", encoding="utf-8")
    assert len(store.list_items(bank, offset=0, limit=10)) == 1


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


def test_deciding_a_failed_row_queues_it_and_clears_the_failure(tmp_path: Path) -> None:
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


def test_upsert_replace_arm_carries_both_payloads(tmp_path: Path) -> None:
    """The changed-fingerprint replace arm must forward ``parked`` AND
    ``duplicate`` — its update dict enumerates fields one by one, so a dropped
    field silently strands the release pin on the OLD payload (or none). The
    sibling test above uses a payload-free ``no_match`` row, which is why a
    dropped-``parked`` mutation survived the whole suite before this pin. The
    re-read matters too: the arm uses ``model_copy``, which skips validators,
    so a broken write would only surface as a corrupt row on the next load."""

    def parked(rid: str) -> ParkedAlbum:
        cand = _candidate(80.0)
        cand = cand.model_copy(
            update={
                "options": [
                    CandidateOption(
                        index=0,
                        confidence=80.0,
                        data_source="MusicBrainz",
                        disambiguation=None,
                        release_id=rid,
                    )
                ]
            }
        )
        return ParkedAlbum(album_index=0, folder="/library/X", candidate=cand)

    prompt = DuplicatePrompt(
        album_index=0,
        incoming=IncomingAlbum(
            album_artist="A",
            album="B",
            year=None,
            track_count=1,
            format=None,
            bitrate_kbps=None,
            folder="/library/X",
            has_current_art=False,
        ),
        existing=[],
    )
    store.upsert_by_folder(
        _bank(tmp_path),
        folder="/library/X",
        source="sweep",
        reason="needs_dup_resolution",
        fingerprint="f1",
        duplicate=prompt,
        parked=parked("mb-old"),
    )
    replaced = store.upsert_by_folder(
        _bank(tmp_path),
        folder="/library/X",
        source="sweep",
        reason="needs_dup_resolution",
        fingerprint="f2",
        duplicate=prompt,
        parked=parked("mb-new"),
    )
    assert replaced.parked is not None
    assert replaced.parked.candidate.options[0].release_id == "mb-new"
    assert replaced.duplicate is not None
    reread = store.get_item(_bank(tmp_path), replaced.id)
    assert reread is not None
    assert reread.parked is not None
    assert reread.parked.candidate.options[0].release_id == "mb-new"
    assert reread.duplicate is not None


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
    # The perf pin: the bank dir is listed EXACTLY once — the one-time import
    # of the old row files — no matter how many creates/upserts/list calls
    # follow, and never again once the database is marked imported (a restart
    # included). On the pre-index store every list_items/count_items/upsert
    # re-listed (O(N^2) across a sweep).
    calls = {"n": 0}
    original_iterdir = Path.iterdir
    bank = _bank(tmp_path)

    def counting_iterdir(self: Path) -> Iterator[Path]:
        if self == bank:
            calls["n"] += 1
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)
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
    store.close_connections()  # a restart
    assert store.count_items(bank) == 6
    store.reconcile_interrupted(bank)
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


def test_legacy_rows_are_imported_once_then_never_read_again(tmp_path: Path) -> None:
    """The first open imports every ``*.json`` row; after that the files are a
    frozen backup — one written later is never read, a restart included, and
    the imported ones are left byte-for-byte as they were."""
    bank = _bank(tmp_path)
    first = _write_row(bank, "a" * 32, folder="/l/first", banked_at=_T1)
    before = (bank / f"{first.id}.json").read_bytes()

    assert [s.id for s in store.list_items(bank, offset=0, limit=50)] == [first.id]
    [(version,)] = _db_rows(bank, "PRAGMA user_version")
    assert version == 1

    late = _write_row(bank, "b" * 32, folder="/l/late", banked_at=_T2)
    store.decide_item(bank, first.id, BankDecision(action="ignore"))
    store.close_connections()  # a restart
    assert [s.id for s in store.list_items(bank, offset=0, limit=50)] == [first.id]
    assert store.get_item(bank, late.id) is None
    assert (bank / f"{first.id}.json").read_bytes() == before


_T1 = datetime(2026, 1, 1, tzinfo=UTC)
_T2 = datetime(2026, 1, 2, tzinfo=UTC)
_T3 = datetime(2026, 1, 3, tzinfo=UTC)


def _write_row(bank: Path, item_id: str, *, folder: str, banked_at: datetime) -> BankItem:
    """Write a row in the OLD store's shape, one ``<id>.json`` file, for the
    import to find (the old store wrote the same JSON, indented)."""
    item = BankItem(
        id=item_id,
        folder=folder,
        source="sweep",
        reason="no_match",
        artist=f"Artist {item_id[0]}",
        album=f"Album {item_id[0]}",
        parked=_parked_payload(),
        fingerprint="f" * 64,
        status="needs_review",
        banked_at=banked_at,
    )
    bank.mkdir(parents=True, exist_ok=True)
    (bank / f"{item_id}.json").write_text(
        json.dumps(item.model_dump(mode="json"), ensure_ascii=True, indent=2), encoding="utf-8"
    )
    return item


def _seed_index_fixture(bank: Path) -> dict[str, BankItem]:
    """Four rows plus a corrupt one: two share a folder at DIFFERENT times,
    two share a folder at the SAME time (so only the id can order them)."""
    rows = {
        "a": _write_row(bank, "a" * 32, folder="/l/shared", banked_at=_T2),
        "b": _write_row(bank, "b" * 32, folder="/l/shared", banked_at=_T1),
        "d": _write_row(bank, "d" * 32, folder="/l/tie", banked_at=_T3),
        "c": _write_row(bank, "c" * 32, folder="/l/tie", banked_at=_T3),
    }
    (bank / f"{'e' * 32}.json").write_text("{not json", encoding="utf-8")
    return rows


def test_import_orders_rows_and_picks_the_folder_winner(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """What the import builds: rows listed newest banked first, id breaking a
    tie; the LATER row in ``(banked_at, id)`` order owns a shared folder (a
    same-fingerprint re-bank refreshes THAT row); a corrupt row is skipped out
    loud; and one INFO line reports the counts."""
    bank = _bank(tmp_path)
    rows = _seed_index_fixture(bank)
    with caplog.at_level(logging.INFO):
        listed = store.list_items(bank, offset=0, limit=50)

    assert [s.id for s in listed] == [rows[k].id for k in ("d", "c", "a", "b")]
    assert {s.id: s for s in listed} == {
        rows[k].id: store._summary_of(rows[k]) for k in ("a", "b", "c", "d")
    }
    for folder, owner in (("/l/shared", "a" * 32), ("/l/tie", "d" * 32)):
        rebanked = store.upsert_by_folder(
            bank, folder=folder, source="sweep", reason="no_match", fingerprint="f" * 64
        )
        assert rebanked.id == owner
    assert store.count_items(bank) == 4
    records = [r for r in caplog.records if r.name == "app.bank.store"]
    warnings = [r.getMessage() for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].startswith(f"Skipping unreadable bank row '{'e' * 32}.json': ")
    # The operator's line, on the logger the shipped container prints.
    infos = [r.getMessage() for r in caplog.records if r.name == "uvicorn.error"]
    assert len(infos) == 1
    assert infos[0].startswith("bank: imported 4 rows (1 skipped) in ")


def test_import_holds_one_full_row_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The import must not keep every full row alive at once.

    Python keeps the memory a peak allocated, so an import that parses every
    row (``parked`` payloads and all) into one list pins it for the life of
    the process.
    """
    bank = _bank(tmp_path)
    _seed_index_fixture(bank)
    parsed: list[weakref.ref[BankItem]] = []  # models are unhashable: no WeakSet
    peak = {"alive": 0}
    real_parse = store._parse_legacy_row

    def tracking_parse(raw: str) -> BankItem:
        gc.collect()  # count what is still REACHABLE, not what awaits collection
        item = real_parse(raw)
        parsed.append(weakref.ref(item))
        alive = sum(1 for ref in parsed if ref() is not None)
        peak["alive"] = max(peak["alive"], alive)
        return item

    monkeypatch.setattr(store, "_parse_legacy_row", tracking_parse)
    assert store.count_items(bank) == 4

    assert len(parsed) == 4  # control: every healthy row went through the parse
    assert peak["alive"] == 1


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
    # The CONTROL for the two tests below: an ordinary decide_again failure is
    # what a rescan must NOT discharge, so this is the arm that proves the new
    # branch is selective rather than clearing every failure it meets.
    assert updated.error_recovery == "decide_again"


def _failed_row_needing(tmp_path: Path, recovery: str) -> str:
    """A row that failed an apply and needs ``recovery`` (the runner's shape)."""
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
    store.set_status(
        tmp_path,
        row.id,
        "failed",
        error="boom",
        error_recovery=recovery,  # type: ignore[arg-type]  # test passes literal strings
    )
    return row.id


def _rescan(tmp_path: Path, item_id: str) -> BankItem:
    updated = store.rescan_item(
        tmp_path,
        item_id,
        fingerprint="a" * 64,
        parked=_parked_payload(),
        artist="A",
        album="B",
        recommendation="strong",
        confidence=90.0,
    )
    assert updated is not None
    return updated


def test_rescan_discharges_a_fix_folder_failure(tmp_path: Path) -> None:
    """The route only reaches here having fingerprinted the folder and read its
    audio files, so "the folder will not answer" has been disproved — and that
    sentence is the row's whole banner. Left standing, the operator fixes the
    folder, presses Rescan and gets the identical red banner back.
    """
    item_id = _failed_row_needing(tmp_path, "fix_folder")

    updated = _rescan(tmp_path, item_id)

    assert updated.status == "needs_review"
    assert updated.error is None
    assert updated.error_recovery == "decide_again"
    assert updated.decided is None


def test_rescan_keeps_a_remove_duplicate_failure(tmp_path: Path) -> None:
    """A rescan re-reads the FOLDER. It does not un-import the second copy the
    library is now carrying, so that banner (and its Duplicates link) stands.
    """
    item_id = _failed_row_needing(tmp_path, "remove_duplicate")

    updated = _rescan(tmp_path, item_id)

    assert updated.status == "failed"
    assert updated.error == "boom"
    assert updated.error_recovery == "remove_duplicate"


def test_a_row_reset_by_another_writer_drops_its_recovery(tmp_path: Path) -> None:
    """``set_status`` rewriting the field on its OWN transitions is not enough.

    Measured against the real store before this was fixed: a row that failed
    needing ``remove_duplicate`` was re-banked by ``upsert_by_folder`` (status
    ``needs_review``, error None) and then decided (``queued``, error None) —
    and carried ``remove_duplicate`` through both, because each reset lists the
    fields it clears and this one was missing from the list. Latent only
    because the field is read under ``failed``; the docstring promised more.
    """
    item_id = _failed_row_needing(tmp_path, "remove_duplicate")

    rebanked = store.upsert_by_folder(
        tmp_path, folder="/inbox/x", source="sweep", reason="no_match", fingerprint="b" * 64
    )
    assert rebanked.id == item_id
    assert rebanked.status == "needs_review"
    assert rebanked.error_recovery == "decide_again"

    # And decide_item's own arm, from a failed row this time (the retry path).
    failed_again = _failed_row_needing(tmp_path, "fix_folder")
    decided = store.decide_item(tmp_path, failed_again, BankDecision(action="asis"))
    assert decided is not None
    assert decided.status == "queued"
    assert decided.error_recovery == "decide_again"


def test_reconcile_clears_the_recovery_of_an_interrupted_row(tmp_path: Path) -> None:
    """Third reset, same rule. The ``applying`` row is forced to carry the
    value (``set_status`` is the only way to write one, so no ordinary sequence
    produces it) — what is pinned is that the revert clears whatever is there,
    the way it clears ``error``.
    """
    item_id = _failed_row_needing(tmp_path, "fix_folder")
    store.decide_item(tmp_path, item_id, BankDecision(action="asis"))
    store.set_status(tmp_path, item_id, "applying", error_recovery="fix_folder")

    assert store.reconcile_interrupted(tmp_path) == 1

    item = store.get_item(tmp_path, item_id)
    assert item is not None
    assert item.status == "needs_review"
    assert item.error_recovery == "decide_again"


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
    store.close_connections()  # read back from the file, not from anything in memory
    item = store.get_item(bank, item_id)
    assert item is not None
    assert item.folder == folder
    assert os.fsencode(item.folder) == b"/music/inbox/Bj\xf6rk"
    [(stored_folder, raw)] = _db_rows(bank, "SELECT folder, row FROM bank")
    # The folder column is the on-disk BYTES (a BLOB), not text.
    assert stored_folder == b"/music/inbox/Bj\xf6rk"
    # The row JSON must be plain UTF-8 text (a lone surrogate is unencodable
    # as UTF-8; the store escapes it instead of scrubbing it).
    assert isinstance(raw, str)
    assert "\\udcf6" in raw  # escaped as the 6-char \\udcf6 text, not U+FFFD and not raw bytes


def test_surrogate_folder_row_survives_a_restart(tmp_path: Path) -> None:
    bank = _bank(tmp_path)
    folder = _surrogate_folder()
    store.create_item(bank, folder=folder, source="manual", reason="no_match", fingerprint="s" * 64)
    store.close_connections()
    page = store.list_items(bank, offset=0, limit=50)
    assert len(page) == 1
    assert page[0].folder == folder
    assert store.count_items(bank) == 1
    # And the re-bank lookup finds it by those bytes.
    again = store.upsert_by_folder(
        bank, folder=folder, source="sweep", reason="no_match", fingerprint="s" * 64
    )
    assert again.id == page[0].id


def test_surrogate_folder_payload_row_roundtrips(tmp_path: Path) -> None:
    """A needs_review row (parked payload present) with a surrogate folder —
    every store sink (write, list, full-row load) must handle it."""
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
    store.close_connections()
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


def test_legacy_pydantic_json_row_still_imports(tmp_path: Path) -> None:
    """Backward compat: a row written by the OLDEST code (model_dump_json) must
    import unchanged."""
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
    store.close_connections()
    assert [s.id for s in store.list_items(bank, offset=0, limit=50)] == [legacy.id]


# Non-UTF-8 bytes: the class the "{not json" fixtures miss — those are valid
# UTF-8 and only exercise JSONDecodeError. UnicodeDecodeError is a ValueError,
# NOT an OSError, so an OSError-only read guard lets it through.
_NON_UTF8 = b"\x00\xe9\xff"


def test_non_utf8_row_file_is_skipped_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Loud skip: the row is left out of the import AND the skip is logged,
    naming the file and the reason — a silent skip reads as 'deleted'."""
    bank = _bank(tmp_path)
    _write_row(bank, "a" * 32, folder="/library/A/B", banked_at=_T1)
    (bank / "corrupt.json").write_bytes(_NON_UTF8)
    with caplog.at_level(logging.WARNING, logger="app.bank.store"):
        rows = store.list_items(bank, offset=0, limit=10)
    assert len(rows) == 1
    warnings = [
        r for r in caplog.records if r.name == "app.bank.store" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "corrupt.json" in warnings[0].getMessage()


def _legacy_row(
    item_id: str, *, status: str, banked_at: datetime, folder: str
) -> dict[str, object]:
    """One old-store row as JSON data: ``decided`` present where the status needs it."""
    decided = BankDecision(action="asis") if status in ("queued", "applying", "done") else None
    return BankItem(
        id=item_id,
        folder=folder,
        source="sweep",
        reason="no_match",
        fingerprint="f" * 64,
        status=status,  # type: ignore[arg-type]  # test passes literal strings
        decided=decided,
        decided_at=banked_at if decided is not None else None,
        banked_at=banked_at,
    ).model_dump(mode="json")


def test_the_boot_parses_only_the_applying_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boot cost must not grow with the bank: on 1,000 rows with exactly one
    ``applying`` and three ``queued``, the startup repair plus the apply
    runner's first pick parse the applying row and the queue's head — the one
    decided first, neither the lowest id nor the first written — and nothing else.
    """
    bank = _bank(tmp_path)
    bank.mkdir(parents=True)
    statuses = ["done", "ignored", "needs_review", "failed", "stale"]
    applying_id = f"{999:032x}"
    # id -> decision time (``_legacy_row`` stamps ``decided_at`` = ``banked_at``).
    queued = {f"{100:032x}": _T3, f"{200:032x}": _T2, f"{300:032x}": _T1}
    head_id = f"{300:032x}"
    for n in range(1000):
        item_id = f"{n:032x}"
        if item_id == applying_id:
            status, when = "applying", _T1
        elif item_id in queued:
            status, when = "queued", queued[item_id]
        else:
            status, when = statuses[n % len(statuses)], _T1
        row = _legacy_row(item_id, status=status, banked_at=when, folder=f"/l/{n}")
        (bank / f"{item_id}.json").write_text(json.dumps(row), encoding="utf-8")
    assert store.count_items(bank) == 1000  # the one-time import, before the boot below
    store.close_connections()  # the restart

    parsed: list[str] = []
    real_parse = store._parse_row

    def counting_parse(raw: str) -> BankItem:
        item = real_parse(raw)
        parsed.append(item.id)
        return item

    monkeypatch.setattr(store, "_parse_row", counting_parse)
    assert store.reconcile_interrupted(bank) == 1
    head = store.next_queued(bank)
    assert head is not None
    assert head.id == head_id
    assert parsed == [applying_id, head_id]

    # Control: the counter sees every parse the store makes.
    repaired = store.get_item(bank, applying_id)
    assert repaired is not None
    assert repaired.status == "needs_review"
    assert parsed == [applying_id, head_id, applying_id]


def test_the_import_is_all_or_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash mid-import leaves no table, no mark and no rows — and the JSON
    files as they were — so the next start simply imports again."""
    bank = _bank(tmp_path)
    for n, when in enumerate((_T1, _T2, _T3)):
        _write_row(bank, f"{n:032x}", folder=f"/l/{n}", banked_at=when)
    before = {p.name: p.read_bytes() for p in bank.glob("*.json")}
    real_put = store._put
    calls = {"n": 0}

    def failing_put(conn: sqlite3.Connection, item: BankItem) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            # A real mid-import failure (disk full, I/O): a SQLite error must
            # abort the import, never count as one skipped row.
            raise sqlite3.OperationalError("killed mid-import")
        real_put(conn, item)

    monkeypatch.setattr(store, "_put", failing_put)
    with pytest.raises(sqlite3.OperationalError, match="killed mid-import"):
        store.count_items(bank)
    assert calls["n"] == 2  # the first row WAS written inside the transaction

    assert _db_rows(bank, "PRAGMA user_version") == [(0,)]
    assert _db_rows(bank, "SELECT name FROM sqlite_master") == []
    assert {p.name: p.read_bytes() for p in bank.glob("*.json")} == before

    monkeypatch.setattr(store, "_put", real_put)  # the next start
    assert store.count_items(bank) == 3
    assert _db_rows(bank, "PRAGMA user_version") == [(1,)]


def test_an_unlistable_bank_is_not_marked_imported(tmp_path: Path) -> None:
    """A bank folder that can be written but not listed must fail the import,
    not mark an import of zero rows done: the files would never be read again,
    while the start logged ``imported 0 rows (0 skipped)``."""
    if os.getuid() == 0:
        pytest.skip("root lists a mode-0300 directory anyway")
    bank = _bank(tmp_path)
    _write_row(bank, f"{0:032x}", folder="/l/0", banked_at=_T1)
    os.chmod(bank, 0o300)
    try:
        with pytest.raises(PermissionError):
            store.count_items(bank)
    finally:
        os.chmod(bank, 0o755)
    assert _db_rows(bank, "PRAGMA user_version") == [(0,)]

    assert store.count_items(bank) == 1  # the next start, folder readable again


def test_a_row_the_old_boot_crashed_on_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Deeply nested JSON raised ``RecursionError`` past the old boot's
    ``except``; the import skips it like any unreadable row and goes on, so it
    cannot fail every start."""
    bank = _bank(tmp_path)
    good = _write_row(bank, "a" * 32, folder="/l/good", banked_at=_T1)
    (bank / f"{'b' * 32}.json").write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="app.bank.store"):
        assert [s.id for s in store.list_items(bank, offset=0, limit=50)] == [good.id]
    assert any(f"{'b' * 32}.json" in r.getMessage() for r in caplog.records)


def test_a_row_with_an_id_the_store_never_mints_is_skipped(tmp_path: Path) -> None:
    """``get_item`` and ``delete_item`` refuse such an id, so imported it would
    sit in the list for good: shown, but never opened or removed."""
    bank = _bank(tmp_path)
    real = _write_row(bank, "a" * 32, folder="/l/real", banked_at=_T1)
    odd = _legacy_row("not-an-id", status="needs_review", banked_at=_T2, folder="/l/odd")
    (bank / "not-an-id.json").write_text(json.dumps(odd), encoding="utf-8")
    assert [s.id for s in store.list_items(bank, offset=0, limit=50)] == [real.id]


def _import_and_log(
    bank: Path, caplog: pytest.LogCaptureFixture
) -> tuple[list[str], list[str], list[str]]:
    """Run the one-time import -> (listed ids, the store's warnings, the operator's lines)."""
    with caplog.at_level(logging.INFO):
        listed = [s.id for s in store.list_items(bank, offset=0, limit=50)]
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.name == "app.bank.store" and r.levelno == logging.WARNING
    ]
    infos = [r.getMessage() for r in caplog.records if r.name == "uvicorn.error"]
    return listed, warnings, infos


@pytest.mark.parametrize("spelling", ["NaN", "Infinity", "-inf"])
def test_a_row_the_strict_writer_refuses_is_skipped_not_fatal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, spelling: str
) -> None:
    """A non-finite spelled as a JSON STRING gets past the token healing, and
    pydantic's lax float parse turns it into nan/inf, which the strict writer
    refuses. That row is skipped out loud and every other row still imports;
    it used to roll the whole import back and stop every start."""
    bank = _bank(tmp_path)
    good = _write_row(bank, "a" * 32, folder="/l/good", banked_at=_T1)
    bad = _legacy_row("b" * 32, status="needs_review", banked_at=_T2, folder="/l/bad")
    bad["confidence"] = spelling
    (bank / f"{'b' * 32}.json").write_text(json.dumps(bad), encoding="utf-8")

    listed, warnings, infos = _import_and_log(bank, caplog)

    assert listed == [good.id]
    assert len(warnings) == 1
    assert warnings[0].startswith(f"Skipping unreadable bank row '{'b' * 32}.json': ")
    assert "not JSON compliant" in warnings[0]
    assert len(infos) == 1
    assert infos[0].startswith("bank: imported 1 rows (1 skipped) in ")
    assert _db_rows(bank, "PRAGMA user_version") == [(1,)]


def test_a_row_whose_folder_names_nothing_on_disk_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``os.fsdecode`` never yields a surrogate outside U+DC80-U+DCFF, so a
    folder holding one names no folder on disk, and its bytes (the folder
    column) do not exist. Skipped and counted like any unreadable row."""
    bank = _bank(tmp_path)
    good = _write_row(bank, "a" * 32, folder="/l/good", banked_at=_T1)
    odd = _legacy_row("b" * 32, status="needs_review", banked_at=_T2, folder="/l/bad\ud800")
    (bank / f"{'b' * 32}.json").write_text(json.dumps(odd), encoding="utf-8")

    listed, warnings, infos = _import_and_log(bank, caplog)

    assert listed == [good.id]
    assert len(warnings) == 1
    assert warnings[0].startswith(f"Skipping unreadable bank row '{'b' * 32}.json': ")
    assert "surrogates not allowed" in warnings[0]
    assert len(infos) == 1
    assert infos[0].startswith("bank: imported 1 rows (1 skipped) in ")


def test_the_skip_warning_stays_one_line(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Both the file name and pydantic's error text reach the skip line, and
    either can carry a newline (a hand-made name; a key in a nested model that
    forbids extras). Formatted raw, each forged a second line in ``docker logs``."""
    bank = _bank(tmp_path)
    bank.mkdir(parents=True)
    (bank / "x\nERROR forged-by-name.json").write_text("{not json", encoding="utf-8")
    row = _legacy_row("e" * 32, status="queued", banked_at=_T1, folder="/l/e")
    row["decided"] = {"action": "asis", "evil\nERROR forged-by-key": 1}
    (bank / f"{'e' * 32}.json").write_text(json.dumps(row), encoding="utf-8")

    listed, warnings, _infos = _import_and_log(bank, caplog)

    assert listed == []
    assert len(warnings) == 2
    assert all("\n" not in text for text in warnings), warnings
    joined = " ".join(warnings)
    assert "'x\\nERROR forged-by-name.json'" in joined  # the name, escaped
    assert "evil\\nERROR forged-by-key" in joined  # the key, escaped


def test_a_naive_banked_at_orders_as_utc(tmp_path: Path) -> None:
    """A ``banked_at`` with no offset beside rows that carry one raised
    ``TypeError`` at the old boot's sort. Imported, it orders as UTC, and the
    row itself keeps the value it was written with."""
    bank = _bank(tmp_path)
    bank.mkdir(parents=True)
    stamps = {
        "a" * 32: "2026-01-01T11:00:00+00:00",
        "b" * 32: "2026-01-01T12:00:00",  # naive
        "c" * 32: "2026-01-01T14:00:00+01:00",  # 13:00 UTC
    }
    for item_id, stamp in stamps.items():
        row = _legacy_row(item_id, status="needs_review", banked_at=_T1, folder=f"/l/{item_id}")
        row["banked_at"] = stamp
        (bank / f"{item_id}.json").write_text(json.dumps(row), encoding="utf-8")
    assert [s.id for s in store.list_items(bank, offset=0, limit=50)] == [
        "c" * 32,
        "b" * 32,
        "a" * 32,
    ]
    naive = store.get_item(bank, "b" * 32)
    assert naive is not None
    assert naive.banked_at == datetime(2026, 1, 1, 12, 0)


def test_rows_order_by_real_time_not_by_their_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pydantic writes ``...:00Z`` for a whole second and ``...:00.500000Z``
    otherwise, and as text the whole second sorts AFTER the half. Both orders —
    the list's newest-banked-first and the apply FIFO — must follow the clock.
    """
    whole = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)
    half = datetime(2026, 6, 12, 12, 0, 0, 500_000, tzinfo=UTC)
    as_text = TypeAdapter(datetime).dump_python
    assert as_text(whole, mode="json") == "2026-06-12T12:00:00Z"
    assert as_text(whole, mode="json") > as_text(half, mode="json")  # the text order is the trap
    bank = _bank(tmp_path)
    monkeypatch.setattr(store, "_now", lambda: whole)
    early = _create(tmp_path, folder="/l/early")
    monkeypatch.setattr(store, "_now", lambda: half)
    late = _create(tmp_path, folder="/l/late")
    assert [s.id for s in store.list_items(bank, offset=0, limit=50)] == [late, early]

    # Decided in the opposite order: the FIFO head is the one decided first.
    store.decide_item(bank, late, BankDecision(action="asis"))  # at `half`
    monkeypatch.setattr(store, "_now", lambda: whole)
    store.decide_item(bank, early, BankDecision(action="asis"))  # at `whole`
    head = store.next_queued(bank)
    assert head is not None
    assert head.id == early


def test_the_store_is_safe_across_threads(tmp_path: Path) -> None:
    """The API's anyio worker threads (``run_in_threadpool``), the apply-runner
    thread and the sweep's import worker thread all call the store at once.
    Every call must land — no ``database is locked``, no connection used from
    two threads at once — and the compare-and-set claim must still let exactly
    one caller win a row."""
    bank = _bank(tmp_path)
    ids = [_create(tmp_path, folder=f"/l/{n}") for n in range(8)]
    for item_id in ids:
        store.decide_item(bank, item_id, BankDecision(action="asis"))
    errors: list[BaseException] = []
    claims: list[str] = []
    start = threading.Barrier(8)

    def worker(n: int) -> None:
        try:
            start.wait()
            for item_id in ids:
                if store.set_status(bank, item_id, "applying", expected="queued") is not None:
                    claims.append(item_id)
                store.upsert_by_folder(
                    bank, folder=f"/l/new{n}", source="sweep", reason="no_match", fingerprint="n"
                )
            # Reads, as the API makes them (``get_item`` also runs outside any
            # transition): enough of them overlap for an unguarded one to show.
            for _ in range(50):
                for item_id in ids:
                    assert store.get_item(bank, item_id) is not None
                store.list_page(bank, active_only=True, offset=0, limit=50)
        except BaseException as exc:  # reported by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    assert sorted(claims) == sorted(ids)  # each row claimed exactly once
    assert store.count_items(bank) == 16  # 8 decided rows + one re-banked folder per worker
