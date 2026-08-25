"""Contract tests for the bank models (chunk 2 of import banking)."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.models.bank import BankDecision, BankItem
from app.models.import_models import (
    DuplicateAction,
    DuplicatesCheckResponse,
    TrackChange,
    UnmatchedItem,
)

BANKED_AT = datetime(2026, 6, 12, tzinfo=UTC)


def test_apply_decision_selects_by_candidate_index() -> None:
    decision = BankDecision(action="apply", candidate_index=2)
    assert decision.candidate_index == 2
    # None mirrors ImportChoice: "apply the top candidate" (runner resolves -> 0).
    assert BankDecision(action="apply").candidate_index is None


def test_duplicate_decision_requires_duplicate_action() -> None:
    with pytest.raises(ValidationError):
        BankDecision(action="duplicate")
    decision = BankDecision(action="duplicate", duplicate_action=DuplicateAction.keep_both)
    assert decision.duplicate_action == "keep_both"


def test_duplicate_decision_accepts_candidate_index() -> None:
    # "decide once": a duplicate decision pins the selected candidate release.
    decision = BankDecision(
        action="duplicate", duplicate_action=DuplicateAction.replace, candidate_index=2
    )
    assert decision.candidate_index == 2
    assert decision.duplicate_action == "replace"


def test_candidate_index_still_rejected_on_asis() -> None:
    with pytest.raises(ValidationError):
        BankDecision(action="asis", candidate_index=1)


def test_bank_duplicates_response_round_trips() -> None:
    # The bank /duplicates endpoint now returns the shared DuplicatesCheckResponse.
    resp = DuplicatesCheckResponse(existing=[])
    assert resp.existing == []


def test_decision_rejects_fields_foreign_to_its_action() -> None:
    with pytest.raises(ValidationError):
        BankDecision(action="ignore", candidate_index=0)
    with pytest.raises(ValidationError):
        BankDecision(action="asis", duplicate_action=DuplicateAction.keep_both)
    with pytest.raises(ValidationError):
        BankDecision(action="apply", duplicate_action=DuplicateAction.skip_new)


def test_decision_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        BankDecision(action="ignore", candidate_id="x")  # type: ignore[call-arg]  # the rejection under test


def test_simple_decisions_stand_alone() -> None:
    for action in ("asis", "astracks", "ignore"):
        decision = BankDecision(action=action)
        assert decision.candidate_index is None
        assert decision.duplicate_action is None


def test_bank_item_roundtrip_minimal() -> None:
    # ISO 8601 strings on the wire validate into real datetimes.
    item = BankItem.model_validate(
        {
            "id": "a" * 32,
            "folder": "/library/Artist/Album",
            "source": "sweep",
            "reason": "no_match",
            "fingerprint": "deadbeef",
            "status": "needs_review",
            "banked_at": "2026-06-12T00:00:00+00:00",
        }
    )
    assert isinstance(item.banked_at, datetime)
    again = BankItem.model_validate_json(item.model_dump_json())
    assert again == item
    assert again.parked is None
    assert again.duplicate is None


def test_bank_item_rejects_garbage_timestamps() -> None:
    with pytest.raises(ValidationError):
        BankItem.model_validate(
            {
                "id": "a" * 32,
                "folder": "/library/Artist/Album",
                "source": "sweep",
                "reason": "no_match",
                "fingerprint": "deadbeef",
                "status": "needs_review",
                "banked_at": "yesterday",
            }
        )


def test_bank_item_rejects_incoherent_rows() -> None:
    # A review row without the payload the review screen needs.
    with pytest.raises(ValidationError):
        BankItem(
            id="a" * 32,
            folder="/library/Artist/Album",
            source="sweep",
            reason="needs_review",
            fingerprint="deadbeef",
            status="needs_review",
            banked_at=BANKED_AT,
        )
    # A dup row without its duplicate prompt.
    with pytest.raises(ValidationError):
        BankItem(
            id="a" * 32,
            folder="/library/Artist/Album",
            source="sweep",
            reason="needs_dup_resolution",
            fingerprint="deadbeef",
            status="needs_review",
            banked_at=BANKED_AT,
        )
    # A queued row with no decision to apply.
    with pytest.raises(ValidationError):
        BankItem(
            id="a" * 32,
            folder="/library/Artist/Album",
            source="sweep",
            reason="no_match",
            fingerprint="deadbeef",
            status="queued",
            banked_at=BANKED_AT,
        )


def test_review_track_rows_parse_legacy_json_without_format() -> None:
    # Banked rows persist candidate payloads as JSON (app/bank/store.py); rows
    # banked before the format field existed must still validate.
    row = TrackChange.model_validate(
        {
            "index": 1,
            "status": "unchanged",
            "title_before": "Airbag",
            "title_after": "Airbag",
            "track_before": 1,
            "track_after": 1,
        }
    )
    assert row.format is None
    extra = UnmatchedItem.model_validate({"title": "bonus", "track": None})
    assert extra.format is None


def test_bank_item_decided_row_validates() -> None:
    item = BankItem(
        id="a" * 32,
        folder="/library/Artist/Album",
        source="sweep",
        reason="no_match",
        fingerprint="deadbeef",
        status="queued",
        decided=BankDecision(action="asis"),
        decided_at=datetime(2026, 6, 12, 1, tzinfo=UTC),
        banked_at=BANKED_AT,
    )
    assert item.decided is not None
    assert item.decided.action == "asis"
