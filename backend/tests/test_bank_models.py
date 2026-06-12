"""Contract tests for the bank models (chunk 2 of import banking)."""

import pytest
from pydantic import ValidationError

from app.models.bank import BankDecision, BankItem
from app.models.import_models import DuplicateAction


def test_apply_decision_requires_candidate_id() -> None:
    with pytest.raises(ValidationError):
        BankDecision(action="apply")
    decision = BankDecision(action="apply", candidate_id="abc123")
    assert decision.candidate_id == "abc123"


def test_duplicate_decision_requires_duplicate_action() -> None:
    with pytest.raises(ValidationError):
        BankDecision(action="duplicate")
    decision = BankDecision(action="duplicate", duplicate_action=DuplicateAction.keep_both)
    assert decision.duplicate_action == "keep_both"


def test_simple_decisions_take_no_extras() -> None:
    for action in ("asis", "tracks", "ignore"):
        decision = BankDecision(action=action)
        assert decision.candidate_id is None


def test_bank_item_roundtrip_minimal() -> None:
    item = BankItem(
        id="a" * 32,
        folder="/library/Artist/Album",
        source="sweep",
        reason="no_match",
        fingerprint="deadbeef",
        status="needs_review",
        banked_at="2026-06-12T00:00:00+00:00",
    )
    again = BankItem.model_validate_json(item.model_dump_json())
    assert again == item
    assert again.parked is None and again.duplicate is None
