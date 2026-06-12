"""Pydantic contract for the import bank (chunk 2 of import banking).

The bank is the persistent queue of set-aside albums awaiting review. Rows
carry the SAME candidate payloads the live review screen consumes
(``ParkedAlbum`` / ``DuplicatePrompt``) so opening a banked album is instant —
no beets imports here, no new beets mapping (rule 3 untouched).
"""

from typing import Literal

from pydantic import BaseModel, model_validator

from app.models.import_models import DuplicateAction, DuplicatePrompt, ParkedAlbum

BankSource = Literal["sweep", "inbox", "manual"]

BankReason = Literal["needs_review", "needs_dup_resolution", "no_match"]

# "queued" = decided, waiting for the apply runner (chunk 4). "stale" = the
# folder changed since banking (fingerprint mismatch at apply time).
BankStatus = Literal["needs_review", "queued", "applying", "done", "failed", "ignored", "stale"]

BankDecisionAction = Literal["apply", "asis", "tracks", "duplicate", "ignore"]


class BankDecision(BaseModel):
    """The user's verdict on a banked row.

    ``apply`` needs the chosen ``candidate_id``; ``duplicate`` needs the
    ``duplicate_action``; the rest stand alone. Enforced here so an impossible
    decision can never be persisted or queued.
    """

    action: BankDecisionAction
    candidate_id: str | None = None
    duplicate_action: DuplicateAction | None = None

    @model_validator(mode="after")
    def _required_fields_by_action(self) -> "BankDecision":
        if self.action == "apply" and not self.candidate_id:
            raise ValueError("an apply decision requires candidate_id")
        if self.action == "duplicate" and self.duplicate_action is None:
            raise ValueError("a duplicate decision requires duplicate_action")
        return self


class BankItem(BaseModel):
    """One banked album — the full row, candidate payloads included."""

    id: str
    folder: str  # absolute server-side path; never client-supplied
    source: BankSource
    reason: BankReason
    artist: str | None = None
    album: str | None = None
    recommendation: str | None = None
    confidence: float | None = None
    parked: ParkedAlbum | None = None  # the live review screen's exact payload
    duplicate: DuplicatePrompt | None = None
    fingerprint: str
    status: BankStatus
    decided: BankDecision | None = None
    error: str | None = None
    banked_at: str  # ISO 8601 (UTC), string for a stable JSON contract
    decided_at: str | None = None
    resolved_at: str | None = None


class BankItemSummary(BaseModel):
    """List-row projection: everything except the heavy payloads."""

    id: str
    folder: str
    source: BankSource
    reason: BankReason
    artist: str | None = None
    album: str | None = None
    recommendation: str | None = None
    confidence: float | None = None
    status: BankStatus
    error: str | None = None
    banked_at: str


class BankListResponse(BaseModel):
    """``GET /api/bank`` page: summaries + total for pagination."""

    items: list[BankItemSummary]
    total: int
    offset: int
    limit: int


class BankBulkIgnoreRequest(BaseModel):
    ids: list[str]


class BankBulkIgnoreResponse(BaseModel):
    """How many rows actually flipped (non-``needs_review`` ids are skipped)."""

    ignored: int
