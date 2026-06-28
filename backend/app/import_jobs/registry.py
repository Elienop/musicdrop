"""The in-memory, single-slot import-job registry (sequential review).

Holds at most one active ImportJob. Starting a second import while one is active
raises RuntimeError (the API maps it to 409). beets imports one album at a time,
so the registry drains chunk-1's ImportBridge — every per-album outcome
(non-blocking ``drain_outcomes``) plus the at-most-one parked album
(``get_parked(timeout=0)``) — into a live feed, and delivers the user's choice
for the parked album to the worker. Thread-safe: the worker thread mutates
phase/summary via callbacks while API threads read state and push the choice.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.beets.import_mapping import embedded_art
from app.beets.import_session import ImportBridge
from app.import_jobs.runner import BeetsImportRunner, ImportRunner
from app.models.bank import BankApplyDirective
from app.models.import_api import (
    ActiveImportStatus,
    ImportAlbumStatus,
    ImportAlbumSummary,
    ImportJobState,
    ImportPhase,
    ImportProgress,
    SweepStatus,
)
from app.models.import_models import (
    AlbumOutcome,
    AlbumOutcomeStatus,
    Candidate,
    DuplicateAction,
    DuplicateDecision,
    DuplicatePrompt,
    ImportAction,
    ImportChoice,
    ImportOptions,
    ImportOrigin,
    ParkedAlbum,
)

# Phases in which a job still owns the single import slot.
_ACTIVE_PHASES = {ImportPhase.scanning, ImportPhase.reviewing, ImportPhase.applying}
# Feed statuses that count as "set aside" (left in the source for a later pass):
# an uncertain match awaiting review, or an unresolved library duplicate.
_SET_ASIDE_STATUSES = {
    ImportAlbumStatus.needs_review,
    ImportAlbumStatus.needs_dup_resolution,
}
# Decisions that count as "imported" in the truthful summary.
_APPLY_ACTIONS = {ImportAction.apply, ImportAction.asis, ImportAction.astracks}
# Duplicate decisions that count as "imported" (skip_new is the only skip).
_DUP_IMPORTED_ACTIONS = {
    DuplicateAction.keep_both,
    DuplicateAction.replace,
    DuplicateAction.merge,
}
# How a worker outcome maps to an initial feed-row status (by VALUE, never ordinal).
_OUTCOME_STATUS = {
    AlbumOutcomeStatus.applied: ImportAlbumStatus.applied,
    AlbumOutcomeStatus.skipped: ImportAlbumStatus.skipped,
    AlbumOutcomeStatus.needs_review: ImportAlbumStatus.needs_review,
    AlbumOutcomeStatus.needs_dup_resolution: ImportAlbumStatus.needs_dup_resolution,
}


@dataclass
class _FeedAlbum:
    """One album in the live feed: its outcome, status, optional parked payload."""

    outcome: AlbumOutcome
    status: ImportAlbumStatus
    parked: ParkedAlbum | None = None
    # The action the user chose for a parked album (None until decided).
    decided_action: ImportAction | None = None
    # The current files' art source path for a parked album (None when none).
    art_source: str | None = None
    # The parked duplicate prompt (None unless this row needs dup resolution).
    duplicate: DuplicatePrompt | None = None
    # The duplicate action the user chose (None until decided).
    duplicate_action: DuplicateAction | None = None


@dataclass
class ImportJob:
    """One import's full in-memory state."""

    id: str
    bridge: ImportBridge
    phase: ImportPhase = ImportPhase.scanning
    albums: dict[int, _FeedAlbum] = field(default_factory=dict)
    summary: str | None = None
    error: str | None = None
    # Where this import came from: "manual" (the web Start flow) or "inbox" (the
    # unattended acquisition seam). Surfaced on the job state + the active probe.
    origin: ImportOrigin = "manual"
    # Sweep-origin jobs count instead of accumulating feed rows: a whole-library
    # sweep would otherwise hold thousands of _FeedAlbum dicts. None for
    # manual/inbox jobs (their feed is untouched).
    sweep: SweepStatus | None = None


class ImportJobRegistry:
    """Single-slot registry of the active (or last) import job."""

    def __init__(self, runner: ImportRunner | None = None) -> None:
        # Default to the real beets runner; tests pass a FakeImportRunner. The
        # real runner needs the Library, set via attach_library() at lifespan.
        self._runner = runner
        self._lib: object | None = None
        self._trash_dir: Path | None = None
        self._bank_dir: Path | None = None
        self._job: ImportJob | None = None
        self._lock = threading.Lock()
        self._broker: object | None = None

    # ----- wiring -----

    def attach_event_broker(self, broker: object | None) -> None:
        """Attach the SSE broker so a finished import notifies open tabs.

        ``object`` (not EventBroker) keeps this registry import-light; the only
        method called is ``publish_library_changed``. None in tests = no-op.
        """
        self._broker = broker

    def attach_library(
        self,
        lib: object | None,
        trash_dir: Path | None = None,
        bank_dir: Path | None = None,
    ) -> None:
        """Provide the beets Library + Trash dir + bank dir the production
        runner builds from (bank_dir feeds sweep-mode sessions)."""
        self._lib = lib
        self._trash_dir = trash_dir
        self._bank_dir = bank_dir

    def _resolve_runner(self) -> ImportRunner:
        if self._runner is not None:
            return self._runner
        return BeetsImportRunner(self._lib, self._trash_dir, self._bank_dir)

    # ----- lifecycle -----

    def has_active_job(self) -> bool:
        with self._lock:
            return self._job is not None and self._job.phase in _ACTIVE_PHASES

    def active_job_id(self) -> str | None:
        """The active job's id (resume target), or None when no job owns the slot.

        Mirrors ``has_active_job`` (same ``_ACTIVE_PHASES`` gate, same lock) but
        returns the id so the import Start screen can deep-link a "Resume" back
        into a running import. ``active_job_id() is not None`` is equivalent to
        ``has_active_job()``.
        """
        with self._lock:
            if self._job is not None and self._job.phase in _ACTIVE_PHASES:
                return self._job.id
            return None

    def start(
        self,
        path: str,
        *,
        options: ImportOptions | None = None,
        origin: ImportOrigin = "manual",
        directive: BankApplyDirective | None = None,
    ) -> str:
        """Start an import; raise RuntimeError if one is already active.

        ``options`` threads per-import overrides (operation move/copy,
        unattended, sweep) to the runner; ``None`` is today's manual default.
        ``origin`` (manual/inbox/sweep/bank_apply) is recorded on the job and
        surfaced on the job state + the active probe. ``directive`` is the
        bank apply runner's translated decision, threaded to the session so
        the one-folder run answers every hook from it (None everywhere else).
        """
        runner = self._resolve_runner()
        runner.validate(path, options)
        # options.sweep is the single source of truth for the sweep origin:
        # callers never pass origin="sweep" themselves, and the inbox/manual
        # call sites stay untouched.
        if options is not None and options.sweep:
            origin = "sweep"
        with self._lock:
            if self._job is not None and self._job.phase in _ACTIVE_PHASES:
                raise RuntimeError("an import is already running")
            job = ImportJob(
                id=uuid.uuid4().hex,
                bridge=ImportBridge(),
                origin=origin,
                sweep=SweepStatus() if origin == "sweep" else None,
            )
            self._job = job

        runner.run(
            path,
            job.bridge,
            on_finish=lambda: self._on_finish(job.id),
            on_error=lambda message: self._on_error(job.id, message),
            options=options,
            directive=directive,
        )
        return job.id

    def _on_finish(self, job_id: str) -> None:
        finished = False
        with self._lock:
            if (
                self._job is not None
                and self._job.id == job_id
                and self._job.phase != ImportPhase.failed
            ):
                self._drain_locked(self._job)
                self._job.phase = ImportPhase.done
                self._job.summary = self._summarize(self._job)
                finished = True
        # Emit OUTSIDE the lock: a finished import (manual / inbox / bank-apply
        # all route through here) tells every open tab to refetch. publish is
        # thread-safe (this runs on the worker thread).
        if finished and self._broker is not None:
            self._broker.publish_library_changed()  # type: ignore[attr-defined]  # duck-typed broker

    def _on_error(self, job_id: str, message: str) -> None:
        matched = False
        with self._lock:
            if self._job is not None and self._job.id == job_id:
                self._job.phase = ImportPhase.failed
                self._job.error = message
                matched = True
        # Emit OUTSIDE the lock: imports apply sequentially, so a partial-then-
        # failed run can have landed albums — open tabs must refetch (reorganize
        # and lyrics emit on their failure paths too). publish is thread-safe.
        if matched and self._broker is not None:
            self._broker.publish_library_changed()  # type: ignore[attr-defined]  # duck-typed broker

    @staticmethod
    def _is_imported(row: _FeedAlbum) -> bool:
        """Imported: auto-applied, a parked album resolved apply-like, or a
        duplicate resolved keep_both/replace/merge."""
        if row.duplicate_action is not None:
            return row.duplicate_action in _DUP_IMPORTED_ACTIONS
        return row.status is ImportAlbumStatus.applied or (
            row.status is ImportAlbumStatus.decided and row.decided_action in _APPLY_ACTIONS
        )

    @staticmethod
    def _is_skipped(row: _FeedAlbum) -> bool:
        """The terminal complement of _is_imported for albums that landed nothing
        (auto-skip, a non-apply choice, or a skip_new duplicate)."""
        if row.duplicate_action is not None:
            return row.duplicate_action not in _DUP_IMPORTED_ACTIONS
        return row.status is ImportAlbumStatus.skipped or (
            row.status is ImportAlbumStatus.decided and row.decided_action not in _APPLY_ACTIONS
        )

    @staticmethod
    def _summarize(job: ImportJob) -> str:
        if job.sweep is not None:
            sweep = job.sweep
            summary = (
                f"swept {sweep.processed}, auto-applied {sweep.auto_applied}, banked {sweep.banked}"
            )
            if sweep.skipped_known:
                summary += f", skipped {sweep.skipped_known} already imported"
            if sweep.paused:
                summary += " - paused"
            return summary
        imported = sum(1 for a in job.albums.values() if ImportJobRegistry._is_imported(a))
        skipped = sum(1 for a in job.albums.values() if ImportJobRegistry._is_skipped(a))
        return f"{imported} imported, {skipped} skipped"

    # ----- access -----

    def get(self, job_id: str) -> ImportJob | None:
        with self._lock:
            if self._job is not None and self._job.id == job_id:
                return self._job
            return None

    def _drain_locked(self, job: ImportJob) -> None:
        """Pull every new outcome + the (at-most-one) parked album into the feed.

        Caller holds ``self._lock``. Both bridge calls are non-blocking.
        """
        if job.sweep is not None:
            self._drain_sweep_locked(job)
            return
        for outcome in job.bridge.drain_outcomes():
            row = job.albums.get(outcome.album_index)
            if row is None:
                job.albums[outcome.album_index] = _FeedAlbum(
                    outcome=outcome,
                    status=_OUTCOME_STATUS.get(outcome.status, ImportAlbumStatus.needs_review),
                )
            elif outcome.status in (
                AlbumOutcomeStatus.needs_review,
                AlbumOutcomeStatus.needs_dup_resolution,
            ):
                # A later set-aside outcome for an album already in the feed must
                # upgrade its row. In UNATTENDED mode a strong match auto-applies
                # (applied outcome) and then resolve_duplicate emits
                # needs_dup_resolution for the SAME index and SKIPs WITHOUT
                # parking — so the park-duplicate flip below never runs. Without
                # this the row stays `applied` and the SKIPped album is
                # mis-reported as imported (and uncounted as set-aside). Mirrors
                # the manual flow's park-duplicate status flip.
                row.status = _OUTCOME_STATUS[outcome.status]
                if outcome.status is AlbumOutcomeStatus.needs_review:
                    # A search re-park re-emits needs_review for an album already
                    # in the feed; its match changed, so refresh the row's
                    # confidence/recommendation (preserving any album_id a later
                    # follow-up attaches). Scoped to needs_review so the duplicate
                    # path (needs_dup_resolution) keeps its existing row.outcome.
                    row.outcome = outcome.model_copy(update={"album_id": row.outcome.album_id})
            elif outcome.album_id is not None:
                # A follow-up outcome carrying the library album id beets
                # assigned at task.add (flushed by the session AFTER
                # choose_match — see _flush_album_ids). Attach the id WITHOUT
                # touching row.status: the row may have moved on (decided /
                # duplicate-resolved) and the id is the only new fact. Follow-
                # ups always carry status=applied, so the upgrade branch above
                # can never match them.
                row.outcome = row.outcome.model_copy(update={"album_id": outcome.album_id})
        while True:
            parked = job.bridge.get_parked(timeout=0)
            if parked is None:
                break
            row = job.albums.get(parked.album_index)
            if row is not None:
                row.parked = parked
                row.art_source = job.bridge.art_source(parked.album_index)
            # (The needs_review outcome is emitted before park, so the row
            # already exists; if ordering ever changed, we'd create it here.)
        while True:
            prompt = job.bridge.get_parked_duplicate(timeout=0)
            if prompt is None:
                break
            row = job.albums.get(prompt.album_index)
            if row is not None:
                row.duplicate = prompt
                # Record the current-files art source (mirrors the parked-candidate
                # loop) so GET /cover can serve the duplicate panel's "new" side.
                row.art_source = job.bridge.art_source(prompt.album_index)
                # Flip the (applied/decided) row to the duplicate-pending state so
                # the feed + UI route to the duplicate decision panel.
                row.status = ImportAlbumStatus.needs_dup_resolution

    def _drain_sweep_locked(self, job: ImportJob) -> None:
        """Counter drain for sweep jobs (caller holds ``self._lock``).

        No ``_FeedAlbum`` rows are ever created, so job state stays O(1) for a
        10k-folder sweep. Mapping (every increment is monotone — a duplicate
        rescinding an auto-apply adds to ``banked`` instead of decrementing):

        * follow-up outcome (``album_id`` set) -> ``auto_applied`` — the only
          truthful "landed in the library" signal; an auto-apply that later hit
          a duplicate and was banked+SKIPped never gets one.
        * ``needs_dup_resolution``           -> ``banked`` (its index was
          already counted as processed by its earlier applied outcome).
        * any other (initial) outcome        -> ``processed`` (+ ``banked``
          for ``needs_review``/``skipped`` — the sweep banks both) and
          refreshes ``current_folder``.

        ``skipped_known`` mirrors the bridge's monotone already-imported
        counter (folders beets' task factory skipped before tagging).
        """
        sweep = job.sweep
        if sweep is None:  # pragma: no cover - callers gate on job.sweep
            return
        for outcome in job.bridge.drain_outcomes():
            if outcome.album_id is not None:
                sweep.auto_applied += 1
            elif outcome.status is AlbumOutcomeStatus.needs_dup_resolution:
                sweep.banked += 1
            else:
                sweep.processed += 1
                sweep.current_folder = outcome.folder
                if outcome.status in (
                    AlbumOutcomeStatus.needs_review,
                    AlbumOutcomeStatus.skipped,
                ):
                    sweep.banked += 1
        sweep.skipped_known = job.bridge.known_skips()

    def drain(self, job_id: str) -> list[ImportAlbumSummary]:
        """Drain the bridge and return the current feed rows (non-blocking)."""
        job = self._require(job_id)
        with self._lock:
            self._drain_locked(job)
            if job.phase == ImportPhase.scanning and any(
                a.status in (ImportAlbumStatus.needs_review, ImportAlbumStatus.needs_dup_resolution)
                for a in job.albums.values()
            ):
                job.phase = ImportPhase.reviewing
            return self._summaries(job)

    def candidate(self, job_id: str, index: int) -> Candidate:
        """Return the full Candidate for the parked album at ``index``."""
        self.drain(job_id)
        job = self._require(job_id)
        with self._lock:
            row = job.albums.get(index)
            if row is None or row.parked is None:
                raise KeyError(index)
            return row.parked.candidate

    def candidate_cover(self, job_id: str, index: int) -> tuple[bytes, str] | None:
        """Embedded cover art for the parked album at ``index``, or None.

        Reads the current files' first item on demand (the worker is parked, so
        the source is still in place). Serves a parked candidate OR a parked
        duplicate (both record the current-files art source on the bridge).
        KeyError when the job/album is unknown, has no art source, or is not
        parked at all - the API maps that to 404, same as the Candidate route.
        """
        self.drain(job_id)
        job = self._require(job_id)
        with self._lock:
            row = job.albums.get(index)
            if (
                row is None
                or row.art_source is None
                or (row.parked is None and row.duplicate is None)
            ):
                raise KeyError(index)
            source = row.art_source
        return embedded_art(source)  # read outside the lock (file I/O)

    def record_choice(self, job_id: str, index: int, choice: ImportChoice) -> None:
        """Deliver a decision for the parked album and mark its row decided.

        Holds the lock across push_choice + the mark so the worker (unblocked by
        the push) cannot summarize the job while the row is still needs_review.
        Propagates the bridge's KeyError (unknown/unparked index) and
        RuntimeError (duplicate choice); the API maps them to 404/409.
        """
        self.drain(job_id)
        job = self._require(job_id)
        with self._lock:
            job.bridge.push_choice(index, choice)  # non-blocking; KeyError/RuntimeError bubble
            row = job.albums.get(index)
            # A `search` is a re-lookup request, not a decision: the worker
            # re-parks the album in place, so leave the row needs_review. Marking
            # it decided would transiently miscount it as skipped (search is not in
            # _APPLY_ACTIONS) until the re-park flips it back.
            if row is not None and choice.action is not ImportAction.search:
                row.status = ImportAlbumStatus.decided
                row.decided_action = choice.action

    def duplicate_prompt(self, job_id: str, index: int) -> DuplicatePrompt:
        """Return the parked DuplicatePrompt at ``index`` (KeyError if none)."""
        self.drain(job_id)
        job = self._require(job_id)
        with self._lock:
            row = job.albums.get(index)
            if row is None or row.duplicate is None:
                raise KeyError(index)
            return row.duplicate

    def record_duplicate_decision(
        self, job_id: str, index: int, decision: DuplicateDecision
    ) -> None:
        """Deliver a duplicate decision to the worker and mark the row decided.

        Mirrors record_choice: holds the lock across push + mark so the worker
        cannot summarize while the row is still needs_dup_resolution. Propagates
        the bridge's KeyError (unknown index) / RuntimeError (duplicate decision).
        """
        self.drain(job_id)
        job = self._require(job_id)
        with self._lock:
            job.bridge.push_duplicate_decision(index, decision)
            row = job.albums.get(index)
            if row is not None:
                row.status = ImportAlbumStatus.decided
                row.duplicate_action = decision.action

    def request_pause(self, job_id: str) -> None:
        """Ask the active sweep to abort cleanly at its next album boundary.

        Sets the bridge pause event the session checks at the top of every
        decision hook (-> beets' native ImportAbortError -> run() unwinds ->
        on_finish -> phase done, slot freed). KeyError for an unknown job
        (API: 404); RuntimeError when the job is not a sweep or no longer
        active (API: 409). Pausing an already-pausing active sweep is a no-op.
        """
        with self._lock:
            job = self._job
            if job is None or job.id != job_id:
                raise KeyError(job_id)
            if job.origin != "sweep" or job.sweep is None:
                raise RuntimeError("only a sweep import can be paused")
            if job.phase not in _ACTIVE_PHASES:
                raise RuntimeError("the sweep is no longer running")
            job.sweep.paused = True
            job.bridge.request_pause()

    def state(self, job_id: str) -> ImportJobState:
        """Drain, then return the full job state for the GET endpoint."""
        self.drain(job_id)
        job = self._require(job_id)
        with self._lock:
            applied = sum(1 for a in job.albums.values() if self._is_imported(a))
            needs_review = sum(
                1 for a in job.albums.values() if a.status is ImportAlbumStatus.needs_review
            )
            skipped = sum(1 for a in job.albums.values() if self._is_skipped(a))
            set_aside = sum(1 for a in job.albums.values() if a.status in _SET_ASIDE_STATUSES)
            return ImportJobState(
                job_id=job.id,
                phase=job.phase,
                progress=ImportProgress(
                    applied=applied, needs_review=needs_review, skipped=skipped
                ),
                albums=self._summaries(job),
                summary=job.summary,
                error=job.error,
                origin=job.origin,
                set_aside=set_aside,
                sweep=job.sweep.model_copy() if job.sweep is not None else None,
            )

    def active_status(self) -> ActiveImportStatus:
        """The active-import probe: ``active`` + resume ``job_id`` (invariant:
        equal), plus the live job's ``origin`` and set-aside count (the FE inbox
        cue's "N set aside for review").

        Drains the active job first so the count tracks the worker's latest
        outcomes; returns the idle ``{active: false}`` shape (with the defaulted
        origin/count) when nothing owns the slot — or when the slot finished
        between the snapshot and the drain.
        """
        with self._lock:
            job = self._job
            job_id = job.id if job is not None and job.phase in _ACTIVE_PHASES else None
        if job_id is None:
            return ActiveImportStatus(active=False, job_id=None)
        try:
            self.drain(job_id)  # refresh the feed (acquires the lock itself)
        except KeyError:
            # TOCTOU: between the snapshot above and this drain the slot can
            # finish AND a fresh import claim it (the swap leaves a new uuid in
            # the slot), so the captured job_id no longer resolves and drain()
            # raises KeyError. Report idle rather than 500 a frequently-polled
            # probe — same outcome as the post-drain re-check below.
            return ActiveImportStatus(active=False, job_id=None)
        with self._lock:
            job = self._job
            if job is None or job.id != job_id or job.phase not in _ACTIVE_PHASES:
                return ActiveImportStatus(active=False, job_id=None)
            set_aside = sum(1 for a in job.albums.values() if a.status in _SET_ASIDE_STATUSES)
            return ActiveImportStatus(
                active=True,
                job_id=job.id,
                origin=job.origin,
                needs_review_count=set_aside,
                sweep=job.sweep.model_copy() if job.sweep is not None else None,
            )

    # ----- helpers -----

    def _require(self, job_id: str) -> ImportJob:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    @staticmethod
    def _summaries(job: ImportJob) -> list[ImportAlbumSummary]:
        rows: list[ImportAlbumSummary] = []
        for index in sorted(job.albums):
            row = job.albums[index]
            outcome = row.outcome
            rows.append(
                ImportAlbumSummary(
                    index=index,
                    folder=outcome.folder,
                    artist=outcome.artist,
                    album=outcome.album,
                    recommendation=outcome.recommendation,
                    confidence=outcome.confidence,
                    status=row.status,
                    album_id=outcome.album_id,
                )
            )
        return rows


# Process-global single-slot registry. The router imports this instance; the
# lifespan calls attach_library on it. Tests call reset_registry to swap in a
# fresh registry (with a FakeImportRunner) so no job leaks across tests.
registry = ImportJobRegistry()


def get_registry() -> ImportJobRegistry:
    """Return the current process-global import registry.

    A function (not a module-level ``from ... import registry``) so callers read
    the LIVE binding — tests swap it via ``reset_registry`` and that swap must be
    visible to the API router. Usable directly as a FastAPI dependency.
    """
    return registry


def reset_registry(runner: ImportRunner | None = None) -> ImportJobRegistry:
    """Replace the global registry (test helper). Returns the new instance."""
    global registry
    registry = ImportJobRegistry(runner=runner)
    return registry
