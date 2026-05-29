"""The import driver: WebImportSession + the async bridge.

This is the only module that subclasses beets' ImportSession. It runs an import
serially on a single dedicated worker thread (config["threaded"] = False —
global-singleton safety). Strong matches auto-apply (mirroring beets); uncertain
ones are mapped to a Candidate, pushed onto a thread-safe out-queue, and the
worker blocks on a per-album reply event until a decision is pushed back.

beets imports are allowed here (inside app/beets/, CLAUDE.md rule 3).
"""

from __future__ import annotations

import os
import queue
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from beets import config
from beets.autotag.match import Recommendation as BeetsRec
from beets.importer.session import ImportAbortError, ImportSession
from beets.importer.tasks import Action

from app.beets.import_mapping import (
    _confidence,
    _opt_int,
    _opt_str,
    embedded_art,
    map_album_match,
    map_candidate_options,
)
from app.beets.trash import album_folder, album_format_bitrate, trash_album
from app.models.import_models import (
    AlbumOutcome,
    AlbumOutcomeStatus,
    DuplicateAction,
    DuplicateDecision,
    DuplicatePrompt,
    ExistingAlbum,
    ImportAction,
    ImportChoice,
    IncomingAlbum,
    ParkedAlbum,
    Recommendation,
)

if TYPE_CHECKING:
    from beets.importer.tasks import ImportTask


# beets IntEnum -> our string enum (only the album-level levels are needed).
_REC_MAP = {
    BeetsRec.none: Recommendation.none,
    BeetsRec.low: Recommendation.low,
    BeetsRec.medium: Recommendation.medium,
    BeetsRec.strong: Recommendation.strong,
}


class ImportBridge:
    """Thread-safe bridge between the import worker and an async consumer.

    The worker (running beets) pushes a ``ParkedAlbum`` to ``_out`` and blocks on
    a per-album reply slot. A consumer drains ``_out``, presents the candidate,
    and calls ``push_choice`` to unblock the worker with an ``ImportChoice``.
    """

    def __init__(self) -> None:
        self._out: queue.Queue[ParkedAlbum] = queue.Queue()
        self._outcomes: queue.Queue[AlbumOutcome] = queue.Queue()
        self._replies: dict[int, queue.Queue[ImportChoice]] = {}
        # NOT popped on unblock (unlike _replies): it serves GET /cover during the
        # parked review window. Growth is bounded - single-slot registry, one
        # active job, a fresh ImportBridge per import is GC'd with the old job.
        self._art_source: dict[int, str] = {}
        # Parallel park channel for duplicate prompts — same maxsize-1 reply
        # rendezvous as the candidate channel, kept separate so the two payload
        # types (ParkedAlbum vs DuplicatePrompt) stay typed.
        self._dup_out: queue.Queue[DuplicatePrompt] = queue.Queue()
        self._dup_replies: dict[int, queue.Queue[DuplicateDecision]] = {}
        self._lock = threading.Lock()
        self._pending = 0

    # ----- worker side -----

    def park(self, parked: ParkedAlbum, art_source: str | None = None) -> ImportChoice:
        """Push a parked album and block until a choice arrives for it."""
        reply: queue.Queue[ImportChoice] = queue.Queue(maxsize=1)
        with self._lock:
            self._replies[parked.album_index] = reply
            if art_source is not None:
                self._art_source[parked.album_index] = art_source
            self._pending += 1
        self._out.put(parked)
        choice = reply.get()  # blocks the worker thread
        with self._lock:
            self._replies.pop(parked.album_index, None)
            self._pending -= 1
        return choice

    def park_duplicate(self, prompt: DuplicatePrompt) -> DuplicateDecision:
        """Push a duplicate prompt and block until a decision arrives for it."""
        reply: queue.Queue[DuplicateDecision] = queue.Queue(maxsize=1)
        with self._lock:
            self._dup_replies[prompt.album_index] = reply
            self._pending += 1
        self._dup_out.put(prompt)
        decision = reply.get()  # blocks the worker thread
        with self._lock:
            self._dup_replies.pop(prompt.album_index, None)
            self._pending -= 1
        return decision

    def art_source(self, album_index: int) -> str | None:
        """The current-files art source path for a parked album, or None."""
        with self._lock:
            return self._art_source.get(album_index)

    def note_outcome(self, outcome: AlbumOutcome) -> None:
        """Record what the worker did with one album (non-blocking).

        Called for EVERY album choose_match processes — auto-applied, skipped, or
        parked — so the consumer can render the full live feed. Never blocks.
        """
        self._outcomes.put_nowait(outcome)

    # ----- consumer side -----

    def drain_outcomes(self) -> list[AlbumOutcome]:
        """Pop every outcome queued since the last drain (non-blocking)."""
        drained: list[AlbumOutcome] = []
        while True:
            try:
                drained.append(self._outcomes.get_nowait())
            except queue.Empty:
                break
        return drained

    def get_parked(self, timeout: float | None = None) -> ParkedAlbum | None:
        """Pop the next parked album, or None on timeout."""
        try:
            return self._out.get(timeout=timeout)
        except queue.Empty:
            return None

    def get_parked_duplicate(self, timeout: float | None = None) -> DuplicatePrompt | None:
        """Pop the next parked duplicate prompt, or None on timeout."""
        try:
            return self._dup_out.get(timeout=timeout)
        except queue.Empty:
            return None

    def push_choice(self, album_index: int, choice: ImportChoice) -> None:
        """Deliver a decision to the worker blocked on ``album_index``."""
        with self._lock:
            reply = self._replies.get(album_index)
        if reply is None:
            raise KeyError(f"no album parked at index {album_index}")
        try:
            reply.put_nowait(choice)
        except queue.Full:
            raise RuntimeError(f"album {album_index} already has a pending choice") from None

    def push_duplicate_decision(self, album_index: int, decision: DuplicateDecision) -> None:
        """Deliver a duplicate decision to the worker blocked on ``album_index``."""
        with self._lock:
            reply = self._dup_replies.get(album_index)
        if reply is None:
            raise KeyError(f"no duplicate parked at index {album_index}")
        try:
            reply.put_nowait(decision)
        except queue.Full:
            raise RuntimeError(f"duplicate {album_index} already has a pending decision") from None

    def pending_count(self) -> int:
        with self._lock:
            return self._pending


class WebImportSession(ImportSession):
    """An ImportSession driven by the web UI instead of a terminal prompt."""

    bridge: ImportBridge

    def __init__(
        self,
        lib: Any,
        loghandler: Any,
        paths: Any,
        query: Any,
        bridge: ImportBridge,
        trash_dir: Path | None = None,
    ) -> None:
        super().__init__(lib, loghandler, paths, query)
        self.bridge = bridge
        # Counter that assigns each parked album a stable index for replies.
        self._album_index = 0
        # Where Replace moves the old copies (reversible Trash). None = unwired
        # (the post-run trash pass is then skipped defensively).
        self._trash_dir = trash_dir
        # Existing duplicate album ids recorded by Replace, trashed AFTER run().
        self._replace_album_ids: set[int] = set()

    # ----- the four decision hooks -----

    def should_resume(self, path: Any) -> bool:
        # Chunk 1 exposes nothing fancy: never resume interactively.
        return False

    def choose_item(self, task: ImportTask) -> Action:
        # Singletons are out of scope for chunk 1; skip them.
        return Action.SKIP

    def resolve_duplicate(self, task: ImportTask, found_duplicates: Any) -> None:
        """Park a duplicate prompt and apply the user's decision.

        beets calls this (when ``import.duplicate_action`` resolves to ``ask`` —
        forced in run_import_worker) for any APPLY/ASIS/RETAG task that has
        library duplicates. We reuse the album's feed index (stashed by
        choose_match) so the duplicate prompt flips that one row, then block the
        serial worker until a decision arrives over the bridge.
        """
        index = getattr(task, "md_album_index", None)
        if index is None:
            # Defensive: resolve_duplicate should always follow choose_match.
            index = self._album_index
            self._album_index += 1
        incoming = self._to_incoming_album(task)
        existing = [self._to_existing_album(album) for album in found_duplicates]
        self.bridge.note_outcome(self._dup_outcome(index, task))
        decision = self.bridge.park_duplicate(
            DuplicatePrompt(album_index=index, incoming=incoming, existing=existing)
        )
        if decision.action is DuplicateAction.skip_new:
            task.set_choice(Action.SKIP)
        elif decision.action is DuplicateAction.merge:
            task.should_merge_duplicates = True
        elif decision.action is DuplicateAction.replace:
            # Leave the APPLY choice intact (new album imports normally); record
            # the existing ids to move to Trash AFTER run() (see run_import_worker).
            # NOT should_remove_duplicates — that is beets' hard-delete path.
            self._replace_album_ids.update(int(a.id) for a in found_duplicates)
        # keep_both: leave the choice intact (no-op, now explicit + chosen).
        return None

    def choose_match(self, task: ImportTask) -> Any:
        """Auto-apply a strong match; otherwise park and await the user.

        Emits exactly one AlbumOutcome for the album (applied / skipped /
        needs_review) so the API can show it in the live feed. Returns either an
        ``AlbumMatch`` (to apply) or an ``Action`` constant.
        """
        # This hook only fires for album tasks, so every candidate is an
        # AlbumMatch; typed as Any since beets' task.candidates is the wider
        # list[AlbumMatch | TrackMatch] union (singletons go through choose_item).
        candidates: list[Any] = list(task.candidates or [])
        # Each album gets a stable index for both its outcome and (if parked) its
        # reply slot. Serial-only: single-writer counter, no lock (config
        # ["threaded"] = False keeps choose_match on one thread).
        index = self._album_index
        self._album_index += 1
        # Stash on the task so resolve_duplicate (a LATER beets stage) reuses
        # this album's feed index instead of creating a second row. setattr (not
        # `task.md_album_index = index`) because md_album_index is a dynamic
        # attribute beets' ImportTask does not declare (mypy attr-defined).
        setattr(task, "md_album_index", index)  # noqa: B010
        rec = task.rec if task.rec is not None else BeetsRec.none
        recommendation = _REC_MAP.get(rec, Recommendation.none)

        if rec == BeetsRec.strong and candidates:
            # Mirror beets' auto-apply of a strong recommendation.
            self.bridge.note_outcome(
                self._outcome(
                    index, task, recommendation, AlbumOutcomeStatus.applied, match=candidates[0]
                )
            )
            return candidates[0]

        if not candidates:
            # Nothing to choose from: skip (an empty match can't be applied).
            self.bridge.note_outcome(
                self._outcome(index, task, recommendation, AlbumOutcomeStatus.skipped)
            )
            return Action.SKIP

        # Park: map the top match + ranked alternatives, emit needs_review, push,
        # block. The outcome is emitted BEFORE park so the API sees the album the
        # instant it parks (park then blocks on the reply).
        # The current files' first item supplies the "before" cover. Detect art
        # now (the worker is about to block parked, so the file is still here).
        items = list(task.items or [])
        art_source = self._first_item_art_source(items)
        has_current_art = art_source is not None and embedded_art(art_source) is not None
        top = candidates[0]
        options = map_candidate_options(candidates)
        candidate = map_album_match(
            top,
            cur_artist=task.cur_artist,
            cur_album=task.cur_album,
            options=options,
            recommendation=recommendation,
            has_current_art=has_current_art,
        )
        folder = self._task_folder(task)
        self.bridge.note_outcome(
            self._outcome(index, task, recommendation, AlbumOutcomeStatus.needs_review, match=top)
        )
        choice = self.bridge.park(
            ParkedAlbum(album_index=index, folder=folder, candidate=candidate),
            art_source=art_source,
        )
        return self._apply_choice(choice, candidates)

    # ----- helpers -----

    def _outcome(
        self,
        index: int,
        task: ImportTask,
        recommendation: Recommendation,
        status: AlbumOutcomeStatus,
        *,
        match: Any | None = None,
    ) -> AlbumOutcome:
        """Build a compact feed outcome for one album.

        ``match`` (an AlbumMatch) supplies the confidence when present (applied /
        needs_review); a skip has no match, so confidence is 0.0.
        """
        confidence = _confidence(match.distance) if match is not None else 0.0
        return AlbumOutcome(
            album_index=index,
            folder=self._task_folder(task),
            artist=_opt_str(task.cur_artist),
            album=_opt_str(task.cur_album),
            recommendation=recommendation,
            confidence=confidence,
            status=status,
        )

    def _dup_outcome(self, index: int, task: ImportTask) -> AlbumOutcome:
        """Feed outcome for a parked duplicate (reuses the album's index)."""
        rec = task.rec if task.rec is not None else BeetsRec.none
        recommendation = _REC_MAP.get(rec, Recommendation.none)
        return self._outcome(
            index, task, recommendation, AlbumOutcomeStatus.needs_dup_resolution, match=task.match
        )

    @staticmethod
    def _first_item_art_source(items: list[Any]) -> str | None:
        """The current-files art source path (first item), or None."""
        return os.fsdecode(items[0].path) if items and items[0].path else None

    def _to_incoming_album(self, task: ImportTask) -> IncomingAlbum:
        """Build the slim 'new' side from the incoming files (APPLY or ASIS)."""
        items = list(task.items or [])
        fmt, bitrate_kbps = album_format_bitrate(items)
        year = _opt_int(items[0].year) if items else None
        art_source = self._first_item_art_source(items)
        has_art = art_source is not None and embedded_art(art_source) is not None
        return IncomingAlbum(
            album_artist=_opt_str(task.cur_artist),
            album=_opt_str(task.cur_album),
            year=year,
            track_count=len(items),
            format=fmt,
            bitrate_kbps=bitrate_kbps,
            folder=self._task_folder(task),
            has_current_art=has_art,
        )

    def _to_existing_album(self, album: Any) -> ExistingAlbum:
        """Map one in-library beets Album (a found_duplicate) to the slim view.

        Uses the session's own ``self.lib`` for the folder resolution (never a
        per-album back-reference). ``self.lib`` is read only when the album has
        items, so an item-less album resolves to "" without touching it.
        """
        items = list(album.items())
        fmt, bitrate_kbps = album_format_bitrate(items)
        folder = album_folder(self.lib, items) if items else ""
        return ExistingAlbum(
            album_id=int(album.id),
            album_artist=_opt_str(album.albumartist),
            album=_opt_str(album.album),
            year=_opt_int(getattr(album, "year", None)),
            track_count=len(items),
            format=fmt,
            bitrate_kbps=bitrate_kbps,
            folder=folder,
        )

    @staticmethod
    def _apply_choice(choice: ImportChoice, candidates: list[Any]) -> Any:
        """Translate a user ImportChoice into a beets match/Action.

        Raises beets' own ``ImportAbortError`` for the abort action — beets'
        ``run()`` catches it and stops the import cleanly (its native abort
        path), so abort behaves exactly like a beets CLI abort.
        """
        if choice.action is ImportAction.abort:
            raise ImportAbortError
        if choice.action is ImportAction.apply:
            idx = choice.candidate_index or 0
            if 0 <= idx < len(candidates):
                return candidates[idx]
            return candidates[0]
        if choice.action is ImportAction.asis:
            return Action.ASIS
        if choice.action is ImportAction.astracks:
            return Action.TRACKS
        return Action.SKIP

    @staticmethod
    def _task_folder(task: ImportTask) -> str:
        if task.paths:
            return os.fsdecode(task.paths[0])
        return ""


def run_import_worker(session: WebImportSession) -> None:
    """Run one import session serially on the calling (worker) thread.

    Forces single-threaded execution and ``import.duplicate_action: ask`` (so the
    duplicate hook always fires, regardless of the user's config — the web review
    IS the "ask"), then runs beets. After run() returns, moves any album the
    Replace action recorded to the reversible Trash, by stable id — beets imports
    the new album first, so the old copy is only touched once the new one is safe.
    """
    config["threaded"] = False
    config["import"]["duplicate_action"] = "ask"
    session.run()
    _trash_replaced_albums(session)


def _trash_replaced_albums(session: WebImportSession) -> None:
    """Move every Replace-recorded existing album to Trash (post-run, by id).

    Synchronous library primitive on the worker thread — NOT the async
    resolve_duplicates_op (which gates on has_active_job + the swap lock and would
    deadlock/409 against this in-flight import). A missing album (already gone) is
    skipped, not an error.
    """
    trash_dir = session._trash_dir
    if trash_dir is None or not session._replace_album_ids:
        return
    lib = session.lib
    for album_id in session._replace_album_ids:
        album = lib.get_album(album_id)
        if album is None:
            continue  # already gone — nothing to trash
        with lib.transaction():
            trash_album(lib, album, trash_dir=trash_dir)
