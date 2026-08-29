"""Pydantic contract for the import bank (chunk 2 of import banking).

The bank is the persistent queue of set-aside albums awaiting review. Rows
carry the SAME candidate payloads the live review screen consumes
(``ParkedAlbum`` / ``DuplicatePrompt``) so opening a banked album is instant —
no beets imports here, no new beets mapping (rule 3 untouched).
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from app.models.import_models import (
    DuplicateAction,
    DuplicatePrompt,
    ExistingAlbum,
    ParkedAlbum,
)

BankSource = Literal["sweep", "inbox", "manual"]

BankReason = Literal["needs_review", "needs_dup_resolution", "no_match"]

# "queued" = decided, waiting for the apply runner (chunk 4). "stale" = the
# folder changed since banking (fingerprint mismatch at apply time).
BankStatus = Literal["needs_review", "queued", "applying", "done", "failed", "ignored", "stale"]

# Same spellings as the live review's ImportAction where the action is the
# same user gesture ("asis"/"astracks"); "ignore" is bank-only (keep the row,
# don't import) and "duplicate" resolves a banked DuplicatePrompt.
BankDecisionAction = Literal["apply", "asis", "astracks", "duplicate", "ignore"]


class BankDecision(BaseModel):
    """The user's verdict on a banked row.

    Mirrors the live review dialect (``ImportChoice``): ``apply`` selects a
    ranked option by ``candidate_index`` (None = the top candidate);
    ``duplicate`` needs the ``duplicate_action`` and may ALSO pin the selected
    release by ``candidate_index`` (the "decide once" path — one click both
    picks the release and resolves the collision); the rest stand alone. Fields
    foreign to the chosen action — known or unknown — are rejected here so an
    impossible decision can never be persisted or queued.
    """

    model_config = ConfigDict(extra="forbid")

    action: BankDecisionAction
    # Index into the banked Candidate.options; only meaningful when action ==
    # "apply" or "duplicate". None means "apply the top candidate" (the apply
    # runner resolves None -> 0), exactly like ImportChoice.candidate_index.
    candidate_index: int | None = None
    duplicate_action: DuplicateAction | None = None

    @model_validator(mode="after")
    def _fields_match_action(self) -> "BankDecision":
        if self.action not in ("apply", "duplicate") and self.candidate_index is not None:
            raise ValueError("candidate_index is only valid on an apply or duplicate decision")
        if self.action == "duplicate":
            if self.duplicate_action is None:
                raise ValueError("a duplicate decision requires duplicate_action")
        elif self.duplicate_action is not None:
            raise ValueError("duplicate_action is only valid on a duplicate decision")
        return self


# The subset of decision actions an apply run can execute ("ignore" resolves
# in the store and never reaches the runner).
BankApplyAction = Literal["apply", "asis", "astracks", "duplicate"]


class BankApplyDirective(BaseModel):
    """A queued decision translated for the import engine (internal, NOT API).

    Threaded ``registry.start -> ImportRunner.run -> WebImportSession`` so the
    one-folder apply run answers every beets hook from the banked decision:

    * ``apply``     — ``search_id`` pins beets ``import.search_ids`` to the
      chosen release; the session selects the pinned lookup's top candidate.
      ``search_id`` None falls back to an unpinned lookup's top candidate,
      which re-runs the match instead of replaying the banked option. That is
      the LEGACY shape only: a row banked before its matched release was
      stored, a row whose task had no match, or an option from a source that
      carries no release id.
    * ``asis`` / ``astracks`` — direct ``Action.ASIS`` / ``Action.TRACKS``
      (astracks singletons then import as-is via ``choose_item``).
    * ``duplicate`` — ``get_duplicate_action`` auto-answers ``duplicate_action``,
      and ``search_id`` pins the banked release the same way ``apply`` does
      (the sweep banks the matched release with the prompt). ``replace_existing``
      carries the collision the BANKED prompt recorded, so a ``replace`` is
      enforced from stored ids rather than from beets' name-keyed re-detection
      (which never fires when the library copy's naming has drifted).

    Never referenced by an endpoint, so it stays out of the OpenAPI schema.
    """

    action: BankApplyAction
    search_id: str | None = None
    duplicate_action: DuplicateAction | None = None
    # The library albums the banked ``DuplicatePrompt`` recorded as colliding.
    # Populated ONLY for ``duplicate_action is replace``; empty everywhere else
    # (including a legacy row whose prompt listed nothing). Carries the whole
    # ``ExistingAlbum``, not a bare id, because beets ids are reused rowids: the
    # session identity-checks each entry against the live library at trash time,
    # where it holds the ``Library``. See the seed's own guards in
    # ``WebImportSession._seed_replace_from_directive``.
    replace_existing: list[ExistingAlbum] = []


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
    # The live review screen's exact payload. Required on a needs_review row;
    # a needs_dup_resolution row carries one too (the release the sweep matched
    # before the collision was found), which is what pins its apply.
    parked: ParkedAlbum | None = None
    duplicate: DuplicatePrompt | None = None
    fingerprint: str
    status: BankStatus
    decided: BankDecision | None = None
    error: str | None = None
    # Whether "decide again" is the sane recovery for THIS failure — the failed
    # banner's headline, which would otherwise be one hardcoded sentence. Only
    # meaningful while status is ``failed``; ``set_status`` (the sole writer of
    # that status) always rewrites it, so a row can never surface a stale value.
    # Default True: every failure retries except the merge that landed a second
    # copy, and a row persisted before this field existed is retryable.
    error_retryable: bool = True
    # The library album id the apply landed (set with status done when the
    # import's outcome carried one; None for skip_new dup resolutions and
    # astracks applies, which create no album entity).
    album_id: int | None = None
    # datetimes validate for real and still serialize as ISO 8601 JSON strings
    # (the config_api.py precedent).
    banked_at: datetime
    decided_at: datetime | None = None
    resolved_at: datetime | None = None

    @model_validator(mode="after")
    def _payloads_match_reason_and_status(self) -> "BankItem":
        """Reject rows the UI or apply runner could do nothing with.

        Deliberately minimal: only the payload/decision presence the next
        screen depends on. Softer bookkeeping coherence (failed <-> error,
        decided <-> decided_at, ignored/stale history) stays the store's job.
        """
        if self.reason == "needs_review" and self.parked is None:
            raise ValueError("a needs_review row requires its parked payload")
        if self.reason == "needs_dup_resolution" and self.duplicate is None:
            raise ValueError("a needs_dup_resolution row requires its duplicate prompt")
        if self.status in ("queued", "applying", "done") and self.decided is None:
            raise ValueError(f"a {self.status} row requires the decision that got it there")
        return self


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
    album_id: int | None = None
    banked_at: datetime


class BankListResponse(BaseModel):
    """``GET /api/bank`` page: summaries + total for pagination."""

    items: list[BankItemSummary]
    total: int
    # Rows of ANY status (ignores the status/view/reason filters). Drives the
    # Review page section's visibility so resolved history stays reachable once
    # the active view empties out.
    total_all: int = 0
    offset: int
    limit: int


class BankBulkIgnoreRequest(BaseModel):
    ids: list[str]


class BankBulkIgnoreResponse(BaseModel):
    """How many rows actually flipped (non-``needs_review`` ids are skipped)."""

    ignored: int


class BankBulkDeleteRequest(BaseModel):
    ids: list[str]


class BankBulkDeleteResponse(BaseModel):
    """How many rows were actually removed (``applying``/missing ids skipped)."""

    deleted: int


class BankSearchResponse(BaseModel):
    """``POST /api/bank/{id}/search``: the row after a re-lookup.

    ``found=False`` = the lookup returned nothing; the row is untouched and
    the client shows its "no release found" line.
    """

    item: BankItem
    found: bool
