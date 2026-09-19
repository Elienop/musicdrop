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

from pydantic import AfterValidator, BaseModel, Field, StringConstraints

from app.models.import_models import ImportOptions, ImportOrigin, Recommendation


def _without_a_nul(path: str) -> str:
    """Refuse an embedded NUL: ``os.path.realpath`` 500'd the start and beets'
    ``lstat`` failed the job (``test_a_nul_in_the_posted_path_is_refused_before_any_job``)."""
    if "\x00" in path:
        raise ValueError("a folder path cannot contain a null character")
    return path


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

    needs_review         -> parked, awaiting the user's match decision
    needs_dup_resolution -> parked, awaiting the user's duplicate decision
    decided              -> the user decided a parked album
    applied              -> a strong match auto-applied in the worker
    skipped              -> the worker skipped it (no candidates)
    """

    needs_review = "needs_review"
    needs_dup_resolution = "needs_dup_resolution"
    decided = "decided"
    applied = "applied"
    skipped = "skipped"


class StartImportRequest(BaseModel):
    """Body of ``POST /api/import``.

    ``path`` is a server-side folder (maps 1:1 to ``beet import <path>``).
    ``options`` carries per-import overrides (operation move/copy/default,
    unattended, sweep, incremental). ``None`` falls through to today's manual
    default (the user's beets config, attended review).
    """

    # Non-blank after stripping (a blank/whitespace path is a 422). Otherwise
    # UNVALIDATED — any server-side folder is accepted, deliberately and not by
    # oversight. The decision and its full reasoning are recorded in BACKLOG.md;
    # the short form is that the sole caller is now the single authenticated
    # account (``/api/import`` is behind the session gate), and that account
    # already sets the library root and the whole beets config via
    # ``POST /api/config/save`` — so an allowlist here would restrict the owner
    # from their own feature while crossing no privilege boundary. Read the
    # BACKLOG entry before adding validation.
    # ``max_length`` is PATH_MAX, a DoS bound not an allowlist: the resolve walks
    # one component at a time — ~331 ms at the cap, 3.5 min at 80 KB without it.
    # It bounds the string, not the TIME (a ``..``-amplified path: 18.8 s over
    # 20 000 entries), so the resolver refuses that segment and the route resolves
    # off the loop (test_the_posted_path_resolve_runs_off_the_event_loop). Off the
    # loop REDUCES the stall rather than removing it: the dominant cost is
    # pure-Python pathlib joins holding the GIL, and the loop serves nobody for
    # 175-334 ms of those ~331 across five runs (security seat, measured
    # 2026-09-19).
    path: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=4096),
        AfterValidator(_without_a_nul),
    ]
    options: ImportOptions | None = None


class StartImportResponse(BaseModel):
    """Response of a successful ``POST /api/import``: the new job's id."""

    job_id: str


class ImportProgress(BaseModel):
    """Coarse progress counters derived from the drained outcomes."""

    applied: int
    needs_review: int
    # Albums that landed nothing: an auto-skip (no candidates) or a parked album
    # the user resolved with a non-apply action. The live mirror of the done
    # terminal skipped count (registry._is_skipped backs both).
    skipped: int
    # Albums resolved as an album-landing action (auto-apply / decided apply|asis
    # / dup keep_both|replace) for which no library album id ever arrived — the
    # session died before reporting one. Mid-run only NOTED rows count; the
    # id-based reading waits for a TERMINAL job, where an id cannot trail a poll.
    not_landed: int = 0
    # Disjoint from every other counter: beets' task factory consults its history
    # BEFORE any session hook, so a history-skipped folder emits no outcome and no
    # feed row (tests/test_import_incremental_e2e.py). It also answers "already
    # imported" for a RESUME record, which needs the user's own ``resume: yes``.
    already_known: int = Field(
        default=0,
        description="Album folders beets skipped as already imported.",
    )


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
    # The library album id once the album landed in the library (attached by a
    # follow-up outcome; see registry._drain_locked). Non-null => the album
    # exists at /albums/{album_id} and the FE links the row there. None for
    # skipped/set-aside rows — and transiently for landed rows (the id can
    # trail its row by one poll; the first poll after done carries every id).
    album_id: int | None = None
    # True when this row was resolved as an album-landing action but no library
    # album id ever arrived (the session died/aborted), or the row carries a
    # ``note``. Without a note it waits for a TERMINAL job; astracks and dup-merge
    # do not flag on the id alone (they land without one).
    did_not_land: bool = False
    # Up to three short sentences when a Replace imported nothing, naming what
    # stopped it and how many copies had moved. The row's status is untouched.
    note: str | None = None


class SweepStatus(BaseModel):
    """Live counters for a sweep-origin import job.

    Sweep jobs do not build the per-album feed (a whole-library sweep would
    accumulate thousands of rows); these monotone counters are the entire
    progress surface. ``auto_applied`` counts albums beets actually added (the
    follow-up outcome carrying the library album id), so it can trail
    ``processed`` by one album while the sweep runs and is exact once done.
    ``banked`` counts every set-aside the sweep persisted: uncertain matches,
    no-match folders, and duplicate prompts. ``skipped_known`` counts folders
    beets' incremental history skipped before tagging (done or banked by an
    earlier sweep).
    """

    processed: int = 0
    auto_applied: int = 0
    banked: int = 0
    skipped_known: int = 0
    current_folder: str | None = None
    paused: bool = False


class FinishedSweep(BaseModel):
    """The last finished sweep's recap — ``ActiveImportStatus.last_sweep``.

    Populated only while the registry's single slot still holds a DONE
    sweep-origin job: a new import replaces the slot (and this recap with
    it), and failed sweeps surface nothing. ``job_id`` targets the run page
    (``/import?job=…``), which lives exactly as long as this block does, so
    the link can never dangle. ``paused`` distinguishes a paused sweep (it
    ends ``phase=done`` with the flag set) from a completed one.
    """

    job_id: str
    processed: int
    auto_applied: int
    banked: int
    skipped_known: int
    paused: bool


class ImportJobState(BaseModel):
    """Response of ``GET /api/import/{job}``: phase + progress + the live feed."""

    job_id: str
    phase: ImportPhase
    progress: ImportProgress
    albums: list[ImportAlbumSummary]
    # The worker's failure message when phase == failed; None otherwise.
    error: str | None
    # Where the import came from: "manual" (the web Start flow) or "inbox" (the
    # unattended acquisition seam). Defaulted so manual imports need no change.
    origin: ImportOrigin = "manual"
    # So a reloaded page can re-post it ("Import them again" sends the same
    # folder with ``incremental: false``). None for a multi-folder start.
    path: str | None = Field(
        default=None,
        description="The folder this import was started with, when it was exactly one.",
    )
    # Albums left in the source for a later manual pass: needs_review (uncertain)
    # + needs_dup_resolution (a library duplicate). For an unattended import this
    # is everything that did not auto-apply, and a ``note`` row counts in
    # not_landed only.
    set_aside: int
    # Sweep-origin jobs surface counters instead of the per-album feed (their
    # ``albums`` list stays empty by design). None for manual/inbox jobs.
    sweep: SweepStatus | None = None
    # Server-computed (from the server's own monotonic clock) so the number
    # survives a page reload and never depends on the browser's clock agreeing
    # with the server's. Keeps counting while a job is parked awaiting a
    # decision; frozen at the first terminal transition.
    elapsed_seconds: int = Field(
        description=(
            "Whole seconds this job has been running, frozen once the phase is done or failed."
        ),
    )
    # Server-side truth, NOT inferable from row statuses: a set-aside row can mean
    # "the worker is blocked in park()" OR "the worker moved on". An unattended
    # duplicate emits needs_dup_resolution and SKIPs WITHOUT parking, and a
    # `search` re-lookup deliberately keeps its row needs_review while beets
    # works. Only the registry knows which, so it says so here.
    awaiting_decision: bool = Field(
        description="True while the worker is blocked on a parked album awaiting a decision.",
    )


class ActiveImportStatus(BaseModel):
    """Response of ``GET /api/imports/active``.

    A typed probe used two ways: the SettingsPage's Apply button polls
    ``active`` to gate itself (an in-flight import would 409 an Apply), and the
    import Start screen reads ``job_id`` to offer a "Resume" link back into a
    running import the user navigated away from.

    ``job_id`` is the active job's id, or ``None`` when nothing is running;
    ``active`` and ``job_id`` are always consistent (``active`` is ``True``
    exactly when ``job_id`` is non-null).

    A named model rather than a bare ``dict`` so the OpenAPI schema emits a
    ``$ref`` and the generated TS type is a concrete ``ActiveImportStatus``
    (per CLAUDE.md rule 2: every endpoint returns a Pydantic model).
    """

    active: bool
    # The active job's id (the Start screen's Resume target), or None when idle.
    job_id: str | None = None
    # The active import's origin (manual/inbox). Optional/defaulted so the
    # idle ``{active: false}`` fallback type-checks against the same model.
    origin: ImportOrigin = "manual"
    # How many albums the active import has set aside (needs_review +
    # needs_dup_resolution, minus ``note`` rows) — the FE inbox cue's count.
    needs_review_count: int = 0
    # The active sweep's counters (None when the active job is not a sweep, or
    # idle) — the FE sweep banner reads this off the existing probe.
    sweep: SweepStatus | None = None
    # The last finished sweep's recap (None while any job runs, when the slot
    # holds a non-sweep or failed job, or after a backend restart) — the
    # Review page's post-sweep summary strip reads this off the same probe.
    last_sweep: FinishedSweep | None = None
