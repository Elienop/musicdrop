"""HTTP-contract Pydantic models for the import API (chunk 2).

These are the request/response shapes the import endpoints take and return. They
map the in-memory ImportJob/ImportBridge/feed state to JSON; no beets object
ever reaches here. The per-album ``recommendation`` reuses
app.models.import_models.Recommendation (the chunk-1 contract); the full
Candidate diff is fetched by a separate endpoint.

The review model is SEQUENTIAL: beets imports one album at a time, so this
exposes a live feed (auto-applied + skipped + the one current parked album), not
a browsable multi-album queue. There is no apply-ready shape.
"""

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, StringConstraints

from app.models.import_models import Recommendation


class ImportPhase(StrEnum):
    """Coarse lifecycle phase of an import job (the registry owns transitions).

    scanning  -> the worker is reading/grouping/looking up (nothing parked yet)
    reviewing -> an album is parked-and-waiting for a decision
    applying  -> reserved (beets exposes no signal to set it transiently)
    done      -> the import finished (incl. a clean abort)
    failed    -> the worker raised; ``error`` holds the message
    """

    scanning = "scanning"
    reviewing = "reviewing"
    applying = "applying"
    done = "done"
    failed = "failed"


# Superset of the worker's AlbumOutcomeStatus (import_models): the registry adds
# "decided". Worker->API mapping must be by VALUE, never by ordinal (orders differ).
class ImportAlbumStatus(StrEnum):
    """Per-album state in the live feed.

    needs_review -> parked, awaiting the user's decision (the current album)
    decided      -> the user decided a parked album (apply/skip/asis/astracks)
    applied      -> a strong match auto-applied in the worker (never parked)
    skipped      -> the worker skipped it (no candidates)
    """

    needs_review = "needs_review"
    decided = "decided"
    applied = "applied"
    skipped = "skipped"


class StartImportRequest(BaseModel):
    """Body of ``POST /api/import``.

    ``path`` is a server-side folder (maps 1:1 to ``beet import <path>``).
    ``options`` is reserved for future per-import overrides (copy/move/autotag);
    v1 reads those from the user's beets config, so it is accepted but unused.
    """

    # Non-blank after stripping (a blank/whitespace path is a 422).
    path: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    options: dict[str, str] | None = None


class StartImportResponse(BaseModel):
    """Response of a successful ``POST /api/import``: the new job's id."""

    job_id: str


class ImportProgress(BaseModel):
    """Coarse progress counters derived from the drained outcomes."""

    applied: int
    needs_review: int


class ImportAlbumSummary(BaseModel):
    """One row in the live feed (the GET /api/import/{job} listing).

    The full per-album diff is fetched separately via
    ``GET /api/import/{job}/albums/{index}`` (only meaningful while an album is
    ``needs_review``); this is the compact feed row.
    """

    index: int
    folder: str
    artist: str | None
    album: str | None
    recommendation: Recommendation
    confidence: float
    status: ImportAlbumStatus


class ImportJobState(BaseModel):
    """Response of ``GET /api/import/{job}``: phase + progress + the live feed."""

    job_id: str
    phase: ImportPhase
    progress: ImportProgress
    albums: list[ImportAlbumSummary]
    # A short human summary once done (e.g. "2 imported, 1 skipped"); None until then.
    summary: str | None
    # The worker's failure message when phase == failed; None otherwise.
    error: str | None
