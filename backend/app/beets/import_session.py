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

from app.bank import store as bank_store
from app.bank.fingerprint import folder_fingerprint
from app.beets.import_mapping import (
    _confidence,
    _opt_int,
    _opt_str,
    embedded_art,
    map_album_match,
    map_candidate_options,
)
from app.beets.merge_preview import build_merge_preview
from app.beets.release_identity import release_identity
from app.beets.relookup import relookup
from app.beets.trash import album_folder, album_format_bitrate, trash_album
from app.models.album import ReleaseIdentity
from app.models.bank import BankApplyDirective, BankReason
from app.models.import_models import (
    AlbumOutcome,
    AlbumOutcomeStatus,
    Candidate,
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


class InLibraryCopyError(ValueError):
    """Copy-mode import of a source inside the library directory (refused).

    beets' "won't duplicate in-library files" guarantee is DB-based, not
    filesystem-based: with files the DB doesn't know yet, copy-mode duplicates
    every file whose computed destination differs from its current path and
    strands the original as an unregistered orphan. The API maps this to a 422;
    the worker raises it as defense-in-depth.
    """


def is_in_library_source(library_dir: bytes, source: str) -> bool:
    """True when ``source`` is *physically* inside the beets library directory.

    ``library_dir`` is ``lib.directory`` as beets stores it (bytes). Two checks,
    both filesystem-aware (no beets calls):

    1. **realpath prefix.** Both sides are resolved with ``os.path.realpath``
       (not just ``abspath``) before the lexical prefix test, so a *symlink
       alias* to the library dir collapses to the same canonical path. This is
       the TrueNAS case where ``directory: /library`` is a symlink onto the real
       dataset and a swept folder reaches the same files through a different
       string: ``abspath`` left the two strings distinct and the guard missed
       it; ``realpath`` makes them equal and the prefix check fires.

    2. **samefile fallback.** ``realpath`` does NOT collapse bind mounts — two
       distinct bind paths onto one directory keep distinct realpaths — so a
       second, stronger check follows: walk the source's ancestor chain and
       return True if any ancestor is the *same physical directory* as the
       resolved library root (``os.path.samefile`` — identical st_dev/st_ino).
       Every filesystem probe is guarded with ``try/except OSError`` so a
       vanished or again-unreadable path can never raise; forcing move on any
       same-dataset source is always the safe direction (a copy there would
       duplicate the files).
    """
    lib_root = Path(os.path.realpath(os.fsdecode(library_dir)))
    src = Path(os.path.realpath(source))
    if src == lib_root or src.is_relative_to(lib_root):
        return True
    # Bind-mount / dataset-alias fallback: realpath keeps distinct strings for
    # two bind paths onto one dir, but samefile sees through to st_dev/st_ino.
    try:
        if not lib_root.exists():
            return False
    except OSError:
        return False
    for ancestor in [src, *src.parents]:
        try:
            if ancestor.samefile(lib_root):
                return True
        except OSError:
            continue
    return False


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
        # Sweep pause flag: set by the registry's request_pause (consumer
        # side), read by the session at the top of every decision hook (worker
        # side). It lives on the bridge because the bridge is the one object
        # both sides already share - the registry never holds the session.
        self._pause = threading.Event()
        # Folders beets' task factory skipped as already imported (incremental
        # history). Monotone; the sweep counters read it, others ignore it.
        self._known_skips = 0

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

    def park_duplicate(
        self, prompt: DuplicatePrompt, art_source: str | None = None
    ) -> DuplicateDecision:
        """Push a duplicate prompt and block until a decision arrives for it."""
        reply: queue.Queue[DuplicateDecision] = queue.Queue(maxsize=1)
        with self._lock:
            self._dup_replies[prompt.album_index] = reply
            if art_source is not None:
                self._art_source[prompt.album_index] = art_source
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

    def request_pause(self) -> None:
        """Ask the worker to abort cleanly at its next decision hook."""
        self._pause.set()

    def pause_requested(self) -> bool:
        return self._pause.is_set()

    def note_known_skip(self) -> None:
        """Count one folder beets skipped as already imported (worker side)."""
        with self._lock:
            self._known_skips += 1

    def known_skips(self) -> int:
        with self._lock:
            return self._known_skips


def _incoming_release(task: ImportTask, items: list[Any]) -> ReleaseIdentity | None:
    """Release identity for the import's 'new' side.

    The matched release when the album matched (what it WILL become, so the user
    can compare it to the existing copy); otherwise the current files' own tags.
    """
    match = getattr(task, "match", None)
    info = getattr(match, "info", None) if match is not None else None
    if info is not None:
        return release_identity(info, getattr(info, "album_id", None))
    if items:
        return release_identity(items[0], getattr(items[0], "mb_albumid", None))
    return None


class WebImportSession(ImportSession):
    """An ImportSession driven by the web UI instead of a terminal prompt."""

    bridge: ImportBridge
    # Unattended (inbox) imports auto-apply strong matches and set the rest aside
    # (SKIP, never park) so the worker never blocks on a human decision.
    unattended: bool
    # Sweep mode: unattended + bank-emitting. The runner builds sweep sessions
    # with the bank dir; attended/inbox sessions carry sweep=False, bank_dir=None.
    sweep: bool
    # Apply mode (chunk 4): when set, every decision hook answers from the
    # banked decision instead of policy. Mutually exclusive with sweep (the
    # apply runner never sets options.sweep); implies unattended.
    _directive: BankApplyDirective | None

    def __init__(
        self,
        lib: Any,
        loghandler: Any,
        paths: Any,
        query: Any,
        bridge: ImportBridge,
        trash_dir: Path | None = None,
        *,
        unattended: bool = False,
        sweep: bool = False,
        bank_dir: Path | None = None,
        directive: BankApplyDirective | None = None,
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
        # When True, uncertain matches + duplicates are set aside (SKIP), not
        # parked. A sweep is unattended by definition, and so is an apply run
        # (the directive IS the decision) - the flags OR in, so a caller can
        # never construct a parked (blocking) sweep or apply.
        self.unattended = unattended or sweep or directive is not None
        # Sweep mode additionally BANKS each set-aside (chunk 3); bank_dir is
        # where the rows go (threaded from the runner, mirroring trash_dir).
        self.sweep = sweep
        self._bank_dir = bank_dir
        self._directive = directive
        # (outcome, task) pairs awaiting the library album id beets assigns
        # AFTER choose_match returns (task.add inside the user_query stage's
        # _apply_choice). Flushed at the next choose_match entry + once after
        # run(): config["threaded"]=False makes beets' pipeline sequential
        # (pipeline.pull), so the previous task has fully finished at both
        # points. Holds at most one task between flushes.
        self._await_album_id: list[tuple[AlbumOutcome, ImportTask]] = []

    # ----- the four decision hooks -----

    def should_resume(self, path: Any) -> bool:
        # Chunk 1 exposes nothing fancy: never resume interactively.
        return False

    def _check_pause(self) -> None:
        """Abort cleanly when a pause was requested (sweep pause).

        Raises beets' own ``ImportAbortError`` - the exact native abort the
        abort choice already uses: beets' ``run()`` catches it and stops the
        pipeline at this album boundary. The aborted task was never chosen, so
        it is not finalized into incremental history and the next sweep picks
        it up again; any pending album-id follow-up still flushes because our
        ``run()`` override flushes after beets swallows the abort.
        """
        if self.bridge.pause_requested():
            raise ImportAbortError

    def already_imported(self, toppath: Any, paths: Any) -> bool:
        """Count folders beets skips as already imported (sweep counters).

        beets' task factory consults this per prospective album folder BEFORE
        any session hook fires, so history-skipped folders never reach the
        outcome stream - this override is the only seam that sees them. The
        count rides the bridge; non-sweep consumers simply never read it.
        """
        known = bool(super().already_imported(toppath, paths))
        if known:
            self.bridge.note_known_skip()
        return known

    def choose_item(self, task: ImportTask) -> Action:
        # Singletons stay out of scope (the whole web flow is album-shaped,
        # the chunk-1 decision) with ONE exception: a banked astracks apply.
        # Its TRACKS choice re-pipelines each file as a SingletonImportTask
        # whose choose_match routes HERE (beets tasks.py:758-760), so ASIS is
        # what actually imports the tracks - the chunk-1 SKIP would silently
        # import nothing. Every other mode (attended, inbox, sweep, non-
        # astracks directives) keeps SKIP; the sweep worker additionally
        # forces import.singletons off so a singletons:yes user config can
        # never funnel files here and history-mark them done without banking.
        self._check_pause()
        if self._directive is not None and self._directive.action == "astracks":
            return Action.ASIS
        return Action.SKIP

    def resolve_duplicate(self, task: ImportTask, found_duplicates: Any) -> None:
        """Park a duplicate prompt and apply the user's decision.

        beets calls this (when ``import.duplicate_action`` resolves to ``ask`` —
        forced in run_import_worker) for any APPLY/ASIS/RETAG task that has
        library duplicates. We reuse the album's feed index (stashed by
        choose_match) so the duplicate prompt flips that one row, then block the
        serial worker until a decision arrives over the bridge. In sweep mode
        the prompt is banked (reason needs_dup_resolution) and the new album
        SKIPped instead — the library copy stays, the decision moves to the bank.
        """
        self._check_pause()
        index = getattr(task, "md_album_index", None)
        if index is None:
            # Defensive: resolve_duplicate should always follow choose_match.
            index = self._album_index
            self._album_index += 1
        # The current files' first item supplies the "before" cover; record it on
        # the bridge so GET /cover can serve the duplicate panel's "new" side
        # (mirrors choose_match's park). Computed once and reused for the
        # IncomingAlbum's has_current_art (see _to_incoming_album).
        art_source = self._first_item_art_source(list(task.items or []))
        incoming = self._to_incoming_album(task)
        existing = [self._to_existing_album(album) for album in found_duplicates]
        prompt = DuplicatePrompt(
            album_index=index,
            incoming=incoming,
            existing=existing,
            merge_preview=build_merge_preview(task, found_duplicates),
        )
        self.bridge.note_outcome(self._dup_outcome(index, task))
        if self._directive is not None:
            dup_action = self._directive.duplicate_action
            if dup_action is None:
                # An apply/asis/astracks row that turns out to duplicate a
                # library album: never auto-pick a destructive resolution.
                # SKIP; the dup outcome above flips the feed row, and the
                # apply runner fails the bank row with re-decide guidance.
                task.set_choice(Action.SKIP)
                return None
            if dup_action is DuplicateAction.skip_new:
                task.set_choice(Action.SKIP)
            elif dup_action is DuplicateAction.merge:
                # Loop-safe: the merged task carries the duplicate's paths, so
                # beets' find_duplicates excludes the old album next time
                # (tasks.py:391-422) and record_replaced absorbs its rows.
                task.should_merge_duplicates = True
            elif dup_action is DuplicateAction.replace:
                # Reversible Trash after run(), by id - never beets' hard
                # delete (mirrors the attended Replace path).
                self._replace_album_ids.update(int(a.id) for a in found_duplicates)
            # keep_both: leave the choice intact (no-op, now explicit + chosen).
            return None
        if self.unattended:
            if self.sweep:
                # Bank the prompt the attended flow would park: the user
                # resolves skip/keep/replace/merge later from the Review page.
                rec = task.rec if task.rec is not None else BeetsRec.none
                self._bank_row(
                    task,
                    reason="needs_dup_resolution",
                    recommendation=_REC_MAP.get(rec, Recommendation.none),
                    confidence=_confidence(task.match.distance) if task.match is not None else 0.0,
                    duplicate=prompt,
                )
            # Unattended: the outcome above records the set-aside; SKIP the new
            # album (keeps the library copy) without parking + blocking.
            task.set_choice(Action.SKIP)
            return None
        decision = self.bridge.park_duplicate(prompt, art_source=art_source)
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
        # Pause lands here first: abort BEFORE this album claims a feed index.
        self._check_pause()
        # Flush the PREVIOUS task's library album id (its task.add has run by
        # now — sequential pipeline) before this album claims the feed.
        self._flush_album_ids()
        # This hook only fires for album tasks, so every candidate is an
        # AlbumMatch; typed as Any since beets' task.candidates is the wider
        # list[AlbumMatch | TrackMatch] union (singletons go through choose_item).
        # Pass beets' full candidate list straight through — beets owns the
        # count (its per-source ``search_limit`` fetch), MusicDrop adds no cap of
        # its own. The switcher options + per-option diffs (built at the mapping
        # boundary) use this SAME list, so the apply-able set and what's shown
        # stay in lock-step: a user can only ever select a release that was both
        # shown and persisted. The strong-auto-apply and directive paths use only
        # candidates[0], so the list length is irrelevant to them.
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

        if self._directive is not None:
            # Apply mode: the banked decision, not policy, decides this album.
            return self._directive_choice(self._directive, task, index, recommendation, candidates)

        if rec == BeetsRec.strong and candidates:
            # Mirror beets' auto-apply of a strong recommendation.
            self._note_outcome_awaiting_album_id(
                self._outcome(
                    index, task, recommendation, AlbumOutcomeStatus.applied, match=candidates[0]
                ),
                task,
            )
            return candidates[0]

        if not candidates:
            # Nothing to choose from: skip (an empty match can't be applied).
            self.bridge.note_outcome(
                self._outcome(index, task, recommendation, AlbumOutcomeStatus.skipped)
            )
            if self.sweep:
                # Bank the folder as no_match: zero candidates, so the banked
                # decisions are as-is / as-tracks / ignore (parked stays None —
                # the BankItem validator only requires a payload for
                # needs_review rows).
                self._bank_row(
                    task, reason="no_match", recommendation=recommendation, confidence=0.0
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
        self._note_outcome_awaiting_album_id(
            self._outcome(index, task, recommendation, AlbumOutcomeStatus.needs_review, match=top),
            task,
        )
        if self.unattended:
            # Unattended: the needs_review outcome above records the set-aside;
            # SKIP instead of parking so the worker never blocks on a decision.
            if self.sweep:
                # The sweep banks what the inbox merely skips: the exact
                # ParkedAlbum the attended park would push, persisted instead.
                # The lookups were already paid for - this only serializes them.
                self._bank_row(
                    task,
                    reason="needs_review",
                    recommendation=recommendation,
                    confidence=_confidence(top.distance),
                    parked=ParkedAlbum(album_index=index, folder=folder, candidate=candidate),
                )
            return Action.SKIP
        # Attended: park and allow "search for a different release" re-lookups
        # (beets' enter-Id loop). A search choice re-runs the lookup on this
        # worker thread and re-parks the SAME index; any other action resolves it.
        return self._park_with_research(
            task,
            index=index,
            folder=folder,
            art_source=art_source,
            has_current_art=has_current_art,
            candidates=candidates,
            recommendation=recommendation,
            first_candidate=candidate,
        )

    def _park_with_research(
        self,
        task: ImportTask,
        *,
        index: int,
        folder: str,
        art_source: str | None,
        has_current_art: bool,
        candidates: list[Any],
        recommendation: Recommendation,
        first_candidate: Candidate,
    ) -> Any:
        """Park the attended review, looping on 'search for a different release'.

        The first park reuses the candidate choose_match already built + the
        needs_review outcome it already emitted. A ``search`` choice re-runs the
        lookup (release id / forced-non-VA name search) on this worker thread and
        re-parks the SAME index with the fresh candidate, a re-emitted
        needs_review outcome (which flips the registry row back from ``decided``),
        and a bumped ``search_revision`` (the client's completion signal). An
        empty result keeps the previous candidates and sets ``search_feedback``.
        Any non-search action resolves via ``_apply_choice``. Manual-search
        results are NEVER auto-applied — always re-parked to confirm (mirrors
        beets' enter-Id).
        """
        candidate = first_candidate
        revision = 0
        while True:
            choice = self.bridge.park(
                ParkedAlbum(album_index=index, folder=folder, candidate=candidate),
                art_source=art_source,
            )
            if choice.action is not ImportAction.search or choice.search is None:
                return self._apply_choice(choice, candidates)
            new_candidates, new_rec = relookup(task, choice.search)
            revision += 1
            feedback: str | None
            if new_candidates:
                candidates = new_candidates
                task.candidates = candidates
                recommendation = _REC_MAP.get(new_rec, Recommendation.none)
                feedback = None
            else:
                feedback = "No release found for that search — showing your previous matches."
            top = candidates[0]
            candidate = map_album_match(
                top,
                cur_artist=task.cur_artist,
                cur_album=task.cur_album,
                options=map_candidate_options(candidates),
                recommendation=recommendation,
                has_current_art=has_current_art,
            ).model_copy(update={"search_feedback": feedback, "search_revision": revision})
            # Re-emit needs_review (plain note_outcome — NOT the album-id stash;
            # the task is not applied) so the registry's drain upgrade branch flips
            # the row back from `decided` (record_choice marked it on submit).
            self.bridge.note_outcome(
                self._outcome(
                    index, task, recommendation, AlbumOutcomeStatus.needs_review, match=top
                )
            )

    def _directive_choice(
        self,
        directive: BankApplyDirective,
        task: ImportTask,
        index: int,
        recommendation: Recommendation,
        candidates: list[Any],
    ) -> Any:
        """Answer choose_match from the banked decision (apply mode).

        ``asis``/``astracks`` need no candidates. ``apply`` and ``duplicate``
        take the lookup's top candidate: with ``search_ids`` pinned (worker)
        the lookup returned exactly the chosen release; unpinned (no stored
        release id - duplicate rows, legacy rows) it is the fresh top match.
        Zero candidates (an id nothing resolved, or network trouble) emits a
        skipped outcome and SKIPs - the apply runner fails the row retryably.
        ASIS and APPLY outcomes ride the album-id follow-up stash so the bank
        row can learn the landed album id; TRACKS lands singletons (no album
        entity), so its outcome is emitted without a follow-up.
        """
        if directive.action == "asis":
            self._note_outcome_awaiting_album_id(
                self._outcome(index, task, recommendation, AlbumOutcomeStatus.applied), task
            )
            return Action.ASIS
        if directive.action == "astracks":
            self.bridge.note_outcome(
                self._outcome(index, task, recommendation, AlbumOutcomeStatus.applied)
            )
            return Action.TRACKS
        if not candidates:
            self.bridge.note_outcome(
                self._outcome(index, task, recommendation, AlbumOutcomeStatus.skipped)
            )
            return Action.SKIP
        self._note_outcome_awaiting_album_id(
            self._outcome(
                index, task, recommendation, AlbumOutcomeStatus.applied, match=candidates[0]
            ),
            task,
        )
        return candidates[0]

    # ----- helpers -----

    def run(self) -> None:
        """Run the import, then flush the final task's library album id.

        beets' run() drives the whole sequential pipeline; the LAST task's
        ``task.add`` happens inside it with no later choose_match to flush it,
        so the follow-up is emitted here. Safe after an abort too: beets'
        run() catches ImportAbortError internally, and an aborted task never
        gained ``task.album``, so the flush drops it.
        """
        super().run()
        self._flush_album_ids()

    def _note_outcome_awaiting_album_id(self, outcome: AlbumOutcome, task: ImportTask) -> None:
        """Emit a feed outcome AND stash the task for the album-id follow-up."""
        self.bridge.note_outcome(outcome)
        self._await_album_id.append((outcome, task))

    def _flush_album_ids(self) -> None:
        """Emit follow-up outcomes carrying the library album id, where added.

        For every stashed (outcome, task) whose task beets actually added
        (``task.add`` created ``task.album`` — it does not exist otherwise),
        emit a copy of the outcome with ``album_id`` set and ``status`` forced
        to ``applied``: by this point the album IS in the library regardless of
        how it was chosen. Tasks that were skipped, aborted, merged away, or
        re-pipelined as-tracks never gain ``task.album`` and are dropped.
        """
        pending, self._await_album_id = self._await_album_id, []
        for outcome, task in pending:
            album = getattr(task, "album", None)
            album_id = getattr(album, "id", None)
            if album_id is None:
                continue
            self.bridge.note_outcome(
                outcome.model_copy(
                    update={"album_id": int(album_id), "status": AlbumOutcomeStatus.applied}
                )
            )

    def _bank_row(
        self,
        task: ImportTask,
        *,
        reason: BankReason,
        recommendation: Recommendation,
        confidence: float,
        parked: ParkedAlbum | None = None,
        duplicate: DuplicatePrompt | None = None,
    ) -> None:
        """Write (or dedupe-refresh) the bank row for this task's folder.

        Called on the worker thread right before the caller SKIPs; the bank
        store's module lock + atomic per-row writes were designed for exactly
        this writer (the API thread reads/mutates rows concurrently). Failures
        PROPAGATE: a sweep that cannot persist its bank becomes a failed job
        (worker on_error), never a silent sweep-on that loses rows.
        """
        if self._bank_dir is None:
            return  # not a sweep session (defensive; the runner always wires it)
        folder = self._task_folder(task)
        if not folder:
            # No folder identity (pathless task): nothing the apply runner
            # could ever re-import - the outcome already recorded the skip.
            return
        bank_store.upsert_by_folder(
            self._bank_dir,
            folder=folder,
            source="sweep",
            reason=reason,
            fingerprint=folder_fingerprint(Path(folder)),
            artist=_opt_str(task.cur_artist),
            album=_opt_str(task.cur_album),
            recommendation=recommendation.value,
            confidence=confidence,
            parked=parked,
            duplicate=duplicate,
        )

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
            release=_incoming_release(task, items),
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
            release=release_identity(album, getattr(album, "mb_albumid", None)),
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
        # The album's folder = the common parent of the task's paths. For a
        # one-folder album this is that folder; for a multi-disc task whose paths
        # are [CD1, CD2, CD3] (a deemix layout excludes the parent) it is the
        # album dir — NOT paths[0]=CD1, which would bank/re-import only disc 1.
        if not task.paths:
            return ""
        decoded = [os.fsdecode(p) for p in task.paths]
        try:
            return os.path.commonpath(decoded)
        except ValueError:  # mixed/relative paths — never happens for beets toppaths
            return decoded[0]


def run_import_worker(
    session: WebImportSession,
    *,
    move: bool | None = None,
    sweep: bool = False,
    directive: BankApplyDirective | None = None,
) -> None:
    """Run one import session serially on the calling (worker) thread.

    Forces single-threaded execution and ``import.duplicate_action: ask`` (so the
    duplicate hook always fires, regardless of the user's config — the web review
    IS the "ask"), then runs beets. After run() returns, moves any album the
    Replace action recorded to the reversible Trash, by stable id — beets imports
    the new album first, so the old copy is only touched once the new one is safe.

    ``move`` scopes the file operation to this one run: ``True`` forces a move
    (``copy=False``), ``False`` forces a copy (``move=False``). Because
    ``config["import"]`` is a process-global confuse singleton, the prior
    move/copy values are snapshotted and restored in a ``finally`` so an inbox
    move never leaks into the next manual import. ``None`` touches nothing — the
    manual-import default falls through to the user's beets config untouched.

    In-library sources are always forced to move-mode (explicit copy raises
    ``InLibraryCopyError``): beets' no-duplicate guarantee is DB-based and does
    not protect files the DB doesn't know yet.

    ``sweep`` scopes the banking sweep's beets flags to this one run (same
    snapshot/restore discipline as move/copy): ``incremental`` on — beets'
    taghistory then skips every folder a previous sweep finished OR banked
    (SKIPped tasks are recorded too: ``incremental_skip_later`` stays at
    beets' default ``no``, so the bank is the sole re-entry path for banked
    folders. ``resume`` off EXPLICITLY — beets' incremental/resume exclusion
    in ``set_config`` is dead code in 2.11 (``want_resume`` reads the global
    ``config["resume"]``, not the excluded copy), so without this every task
    writes resume progress and an aborted sweep re-enters the resume path on
    the next run. ``singletons`` off — a ``singletons: yes`` user config
    would route every file through choose_item -> SKIP and history-mark it
    done WITHOUT a bank row (silent loss); album-shaped tasks are the only
    thing the bank can review.

    ``directive`` scopes a bank apply run's beets flags (same snapshot/restore
    discipline): ``incremental`` off EXPLICITLY — the sweep recorded every
    banked folder in taghistory (SKIPped tasks included) and the user's own
    config may say ``incremental: yes``, so without this beets' task factory
    skips the banked folder before any hook fires and the apply silently does
    nothing; ``resume``/``singletons`` off for the sweep's reasons (astracks
    singletons arrive deliberately via the TRACKS re-pipeline, not the
    singletons flag); ``search_ids`` pinned to the chosen release id for an
    ``apply`` directive (consumed by beets' lookup_candidates stage ->
    ``tag_album(search_ids=...)``: candidates come ONLY from that id) and
    cleared otherwise so a stale user pin can never hijack the run. The
    ``search_ids`` snapshot/restore is unconditional so a directive pin never
    leaks into the next manual import. ``sweep`` and ``directive`` are never
    both set (the runner builds one or the other).
    """
    config["threaded"] = False
    config["import"]["duplicate_action"] = "ask"
    # In-library sources MUST move (same-dataset rename; samefile no-op):
    # with a fresh DB, copy-mode would duplicate any file whose computed
    # destination differs from its current path. Explicit copy is refused;
    # default/None and move pass through forced to move.
    sources = [os.fsdecode(p) for p in session.paths]
    if any(is_in_library_source(session.lib.directory, src) for src in sources):
        if move is False:
            raise InLibraryCopyError(
                "Refusing to copy-import a folder inside the music library: "
                "copy-mode would duplicate the files. Use move instead."
            )
        move = True
    orig_move = config["import"]["move"].get(bool)
    orig_copy = config["import"]["copy"].get(bool)
    orig_incremental = config["import"]["incremental"].get(bool)
    orig_resume = config["import"]["resume"].get()  # bool OR "ask" - restore verbatim
    orig_singletons = config["import"]["singletons"].get(bool)
    orig_search_ids = config["import"]["search_ids"].get()  # restore verbatim
    if move is not None:
        config["import"]["move"] = move
        config["import"]["copy"] = not move
    if sweep:
        config["import"]["incremental"] = True
        config["import"]["resume"] = False
        config["import"]["singletons"] = False
    if directive is not None:
        config["import"]["incremental"] = False
        config["import"]["resume"] = False
        config["import"]["singletons"] = False
        config["import"]["search_ids"] = [directive.search_id] if directive.search_id else []
    try:
        session.run()
    finally:
        config["import"]["move"] = orig_move
        config["import"]["copy"] = orig_copy
        config["import"]["incremental"] = orig_incremental
        config["import"]["resume"] = orig_resume
        config["import"]["singletons"] = orig_singletons
        config["import"]["search_ids"] = orig_search_ids
    _trash_replaced_albums(session)


def _trash_replaced_albums(session: WebImportSession) -> None:
    """Move every Replace-recorded existing album to Trash (post-run, by id).

    Synchronous library primitive on the worker thread — NOT the async
    resolve_duplicates_op (which gates on has_active_job + the swap lock and would
    deadlock/409 against this in-flight import). A missing album (already gone) is
    skipped, not an error.

    Binds ``lib.music_dir_context()`` for the loads + moves: beets 2.11 expands
    DB-relative item paths via a ``ContextVar`` set when the ``Library`` is opened
    (the main thread). This runs on the import worker thread, which does not
    inherit that ``ContextVar``, so without the bind ``Album.move`` gets a relative
    source path and raises ``FileNotFoundError`` (same root cause as the /duplicates
    resolve path).
    """
    trash_dir = session._trash_dir
    if trash_dir is None or not session._replace_album_ids:
        return
    lib = session.lib
    with lib.music_dir_context():
        for album_id in session._replace_album_ids:
            album = lib.get_album(album_id)
            if album is None:
                continue  # already gone — nothing to trash
            with lib.transaction():
                trash_album(lib, album, trash_dir=trash_dir)
