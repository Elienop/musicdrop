"""The import driver: WebImportSession + the async bridge.

This is the only module that subclasses beets' ImportSession. It runs an import
serially on a single dedicated worker thread (config["threaded"] = False —
global-singleton safety). Strong matches auto-apply (mirroring beets); uncertain
ones are mapped to a Candidate, pushed onto a thread-safe out-queue, and the
worker blocks on a per-album reply event until a decision is pushed back.

beets imports are allowed here (inside app/beets/, CLAUDE.md rule 3).
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from beets import config
from beets.autotag.match import Recommendation as BeetsRec
from beets.importer.actions import Action
from beets.importer.actions import DuplicateAction as BeetsDuplicateAction
from beets.importer.session import ImportAbortError, ImportSession
from beets.library import Album

from app.bank import store as bank_store
from app.bank.fingerprint import folder_fingerprint
from app.beets.duplicates import _fuzzy_part
from app.beets.existing_album import to_existing_album
from app.beets.import_mapping import (
    _REC_MAP,
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
from app.beets.research import _read_items, lookup_items
from app.beets.trash import album_format_bitrate, trash_album
from app.models.album import ReleaseIdentity
from app.models.bank import BankApplyDirective, BankReason
from app.models.import_models import (
    AlbumOutcome,
    AlbumOutcomeStatus,
    Candidate,
    DuplicateAction,
    DuplicateDecision,
    DuplicatePrompt,
    ImportAction,
    ImportChoice,
    IncomingAlbum,
    ParkedAlbum,
    Recommendation,
)

if TYPE_CHECKING:
    from beets.importer.tasks import ImportTask

logger = logging.getLogger(__name__)


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
        # True only while an attended album decided "as tracks" is being
        # re-pipelined into singletons: choose_match arms it when it returns
        # Action.TRACKS, choose_item reads it to import each singleton ASIS, and
        # the next album's choose_match clears it. config["threaded"]=False keeps
        # the pipeline serial, so this album's singletons all pass through
        # choose_item before the next choose_match runs.
        self._astracks_in_flight: bool = False

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
        # the chunk-1 decision) with TWO exceptions, both an "as tracks"
        # decision: a banked astracks apply (directive) OR an attended park the
        # user decided astracks (_astracks_in_flight, armed by choose_match). An
        # astracks choice re-pipelines each file as a SingletonImportTask whose
        # choose_match routes HERE (beets tasks.py:758-760), so ASIS is what
        # actually imports the tracks - the chunk-1 SKIP silently imported
        # NOTHING for the attended path (the bug this branch fixes). Every other
        # mode (inbox, sweep, non-astracks directives) keeps SKIP; the sweep
        # worker additionally forces import.singletons off so a singletons:yes
        # user config can never funnel files here and history-mark them done
        # without banking.
        self._check_pause()
        if self._directive is not None and self._directive.action == "astracks":
            return Action.ASIS
        if self._astracks_in_flight:
            return Action.ASIS
        return Action.SKIP

    def get_duplicate_action(self, task: ImportTask, found_duplicates: Any) -> BeetsDuplicateAction:
        """Park a duplicate prompt and return the user's resolution.

        beets 2.12 renamed the old ``resolve_duplicate`` hook to this and made it
        RETURN a ``DuplicateAction`` enum (the pipeline assigns it to
        ``task.duplicate_action``) instead of mutating boolean flags. beets calls
        it (when ``import.duplicate_action`` resolves to ``ask`` — forced in
        run_import_worker) for any APPLY/ASIS/RETAG task with library duplicates.
        We reuse the album's feed index (stashed by choose_match) so the prompt
        flips that one row, then block the serial worker until a decision arrives.
        In sweep mode the prompt is banked (reason needs_dup_resolution) and the
        new album SKIPped instead — the library copy stays, the decision moves to
        the bank.

        Our four model actions map onto beets' enum:
        skip_new→SKIP, keep_both→KEEP, merge→MERGE, and replace→KEEP (the new
        album imports + the old copy is kept in the DB, then trashed by id after
        run() — never beets' destructive REMOVE).
        """
        self._check_pause()
        if not task.is_album:
            # A singleton "as tracks" import whose track duplicates a library item.
            # beets 2.12 shares this hook for singletons, but passes Items — not
            # Albums (SingletonImportTask.find_duplicates, tasks.py) — and the
            # prompt / replace machinery below is album-shaped. SKIP the duplicate
            # track (keeps the library copy): the safe, non-destructive resolution,
            # matching beets' own singleton default. Never feed Items to
            # to_existing_album (it does items[0].path on a Model.items() field
            # tuple → crashes the whole import job) nor record their ids into
            # _replace_album_ids (the post-run Trash pass would delete the album
            # that happens to share that id).
            return BeetsDuplicateAction.SKIP
        index = getattr(task, "md_album_index", None)
        if index is None:
            # Defensive: this hook should always follow choose_match.
            index = self._album_index
            self._album_index += 1
        # The current files' first item supplies the "before" cover; record it on
        # the bridge so GET /cover can serve the duplicate panel's "new" side
        # (mirrors choose_match's park). Computed once and reused for the
        # IncomingAlbum's has_current_art (see _to_incoming_album).
        art_source = self._first_item_art_source(list(task.items or []))
        incoming = self._to_incoming_album(task)
        existing = [to_existing_album(self.lib, album) for album in found_duplicates]
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
                return BeetsDuplicateAction.SKIP
            return self._beets_dup_action(dup_action, found_duplicates)
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
            return BeetsDuplicateAction.SKIP
        decision = self.bridge.park_duplicate(prompt, art_source=art_source)
        return self._beets_dup_action(decision.action, found_duplicates)

    def _beets_dup_action(
        self, action: DuplicateAction, found_duplicates: Any
    ) -> BeetsDuplicateAction:
        """Translate our model DuplicateAction into beets' enum (recording the
        replace ids for the post-run Trash on a ``replace``)."""
        if action is DuplicateAction.skip_new:
            return BeetsDuplicateAction.SKIP
        if action is DuplicateAction.merge:
            # Loop-safe: the merged task carries the duplicate's paths, so beets'
            # find_duplicates excludes the old album next time and record_replaced
            # absorbs its rows.
            return BeetsDuplicateAction.MERGE
        if action is DuplicateAction.replace:
            # Import the new album + KEEP the old in the DB, then move the old to
            # the reversible Trash by id AFTER run() — never beets' destructive
            # REMOVE (its hard-delete path).
            self._replace_album_ids.update(int(a.id) for a in found_duplicates)
            return BeetsDuplicateAction.KEEP
        # keep_both: import alongside the existing copy.
        return BeetsDuplicateAction.KEEP

    # ----- import-gate guard: typographic-variant duplicates -----

    def _install_dup_guard(self, task: ImportTask) -> None:
        """Wrap ``task.find_duplicates`` so beets' downstream ``_resolve_duplicates``
        sees typographic-variant duplicates, not just byte-exact ones.

        beets' ``AlbumImportTask.find_duplicates`` (tasks.py) is a byte-exact SQL
        match on albumartist+album, so a typographic twin ("\u2013" en dash vs "-"
        hyphen, or a case variant) sails through and mints a sibling one-track
        album. We install a per-task wrapper over the bound method at the top of
        ``choose_match`` (precedent: the ``md_album_index`` setattr at the same
        spot) so the SAME duplicate machinery the exact case already uses — the
        park prompt, the sweep bank, the unattended SKIP, the four resolution
        actions via ``get_duplicate_action`` — engages unchanged for the variant.

        Idempotent across re-calls on one task: the pristine bound method is
        stashed on a dynamic attribute (``_md_dup_guard_orig``) so a second install
        re-wraps the original, never a prior wrapper. mypy strict (and a re-wrap,
        not recursion) is preserved because we capture the bound method BEFORE the
        instance attribute shadows it.
        """
        pristine = getattr(task, "_md_dup_guard_orig", None)
        if pristine is None:
            pristine = task.find_duplicates
            # Dynamic attr beets' ImportTask does not declare; setattr (not
            # ``task._md_dup_guard_orig = ...``) to dodge mypy attr-defined (same
            # convention as the md_album_index stash above).
            setattr(task, "_md_dup_guard_orig", pristine)  # noqa: B010  # stash the pristine bound method for the wrapper's closure

        def guarded(lib: Any) -> Any:
            return self._guarded_find_duplicates(task, lib, pristine)

        # beets reads find_duplicates in _resolve_duplicates AFTER choose_match (and
        # again in duplicate_items/remove_duplicates), so this per-task shadow is
        # exactly the seam; mypy forbids plain instance method assignment — reason:
        # intentional, beets has no typed override hook, and the closure keeps the
        # byte-exact pass intact (see _guarded_find_duplicates).
        task.find_duplicates = guarded  # type: ignore[method-assign]  # wrap beets' per-task bound method for _resolve_duplicates; see _install_dup_guard

    def _album_table_signature(self, lib: Any) -> tuple[int, int]:
        """(row count, max id) of the library's albums table, one raw aggregate.

        This is the cache-invalidation probe for :meth:`_variant_album_index`,
        and it deliberately uses beets 2.x's PUBLIC raw-read path
        (``Library`` subclasses the dbcore ``Database``; see ``library.py``
        L35) rather than re-materialising ``lib.albums()``: ``COUNT(*)`` +
        ``MAX(id)`` on the id index is single-digit microsecond work (measured
        ~6µs) wherever the library size is, so it is safe to run on EVERY
        guarded call, whereas a full album materialisation is tens of ms at
        20k rows. A row add or delete — how a prior task's library album
        lands, and how an external writer changes the set — changes the
        signature, so the index is rebuilt exactly when the set changed and
        never in the meantime.

        :param lib: the beets Library (``session.lib``).
        :return: the (count, max id) tuple to cache-check against.
        """
        with lib.transaction() as tx:
            row = tx.query(f"SELECT COUNT(*) c, COALESCE(MAX(id), -1) m FROM {Album._table}", [])[0]
        return (int(row["c"]), int(row["m"]))

    def _variant_album_index(self, lib: Any) -> dict[tuple[str, str], tuple[Any, ...]]:
        """Normalized (artist, title) -> album rows, cached per session run.

        Built ONCE (see :meth:`_album_table_signature`) and returned
        read-through until the library's album row set actually changes.
        After the first build, each guarded call costs the aggregate probe
        (µs) plus an O(1) dict lookup and the per-task exclusion over whatever
        rows hash to the lookup key — NOT a full ``lib.albums()`` pass plus
        two ``_fuzzy_part`` calls per row on every task, which was the measured
        regression (a 500-album serial sweep re-paid the full rescan per album;
        at 20k rows one rescan is ~ms, so the old per-album cost was ~ms x the
        whole sweep).

        Invariant this cache must honor: an album added by an earlier task in
        the same run is a NEW album row and therefore a new count + new max id,
        so the next guard call's probe detects the change and rebuilds — the
        later task sees it. (Verified by a test: task 1 imports en-dash twin
        of a hyphen original; after ``task.add`` lands, task 2 on the ORIGINAL
        must find BOTH the original and the newly-added en-dash twin, not the
        stale original-only index.)

        Accepted staleness window (stated exactly): the signature is
        ``COUNT(*), COALESCE(MAX(id), -1)`` — there is no modified-time column
        on beets' albums table to fold in (``when_modified`` does not exist;
        only ``added``). The signature changes on any row ADD or DELETE,
        including delete-then-add pairs (either the count or the max id
        moves). The residual stale window is an IN-PLACE column edit — a
        concurrent album rename through the server API mid-run — which
        changes no row and therefore no signature; the index carries the old
        title until the next add or delete rebuilds it. Bounded consequence:
        the byte-exact beets pass is independent of this cache, so a stale
        index can at worst miss a variant twin (a false negative the user can
        still resolve through the duplicates page) or offer a bucket entry
        whose per-task exclusions still run — never a crash and never a
        wrong (non-twin) resolution applied automatically.

        :param lib: the beets Library (``session.lib``).
        :return: a mapping ``_(artist_key, title_key) -> _tuple of Albums__``.
        """
        # The cached value is (signature, index). It lives on the instance for
        # the session run's lifetime; a fresh WebImportSession is per run, so
        # "at most once per session run" falls out of per-instance state. We
        # do not declare it in __init__ — the gate's tests construct the
        # session via __new__ and set attrs manually, and beets ImportTask
        # precedent already uses dynamic attrs the same way (mypy attr-
        # defined is a known trade-off for these beets-side seams).
        cached: tuple[tuple[int, int], dict[tuple[str, str], tuple[Any, ...]]] | None
        cached = getattr(self, "_variant_index_cache", None)
        sig = self._album_table_signature(lib)
        if cached is not None and cached[0] == sig:
            return cast("dict[tuple[str, str], tuple[Any, ...]]", cached[1])

        index: dict[tuple[str, str], list[Any]] = {}
        for existing in lib.albums():
            artist_key = _fuzzy_part(str(getattr(existing, "albumartist", "") or ""))
            title_key = _fuzzy_part(str(getattr(existing, "album", "") or ""))
            index.setdefault((artist_key, title_key), []).append(existing)
        final: dict[tuple[str, str], tuple[Any, ...]] = {
            key: tuple(bucket) for key, bucket in index.items()
        }

        setattr(self, "_variant_index_cache", (sig, final))  # noqa: B010
        return final

    def _guarded_find_duplicates(self, task: ImportTask, lib: Any, exact_find: Any) -> list[Any]:
        """beets' byte-exact ``find_duplicates`` plus a normalized-variant scan.

        The exact pass runs FIRST and UNCHANGED — byte-identical duplicates
        keep working exactly as before (the existing duplicate tests are the
        net), and it is also independent of the variant index's cache (it is
        beets' own bound method), so a stale index can never corrupt the
        byte-exact path. We then add library albums the exact match missed
        because the incoming artist/album differs only in the ways
        ``app.beets.duplicates``'s ``_fuzzy_part`` folds (dash glyph, case,
        parentheticals, feat clauses, whitespace) — the SAME signal the fuzzy
        duplicate finder groups on (``_grouping_signals``), so the gate and
        fuzzy detection cannot disagree about which titles are twins.

        We resolve candidates by LOOKING UP the incoming (artist_key,
        title_key) in the cached :meth:`_variant_album_index` (an O(1) dict
        access after a single µs aggregate probe) rather than rescan the whole
        library per task; the per-task work is then just the exclusions below
        over the (usually small) candidate bucket.

        The exclusions beets bakes into ``find_duplicates`` are mirrored for
        the variant hits (invariant: mirror beets' own exclusions):

        * as-is / no-artist — beets returns ``[]`` when ``info["artist"]`` is
          None; we do the same for the variant scan, so an artistless album
          still imports.
        * re-import — an existing album whose files are ALL in the task is
          being re-imported (replaced), not duplicated, so it is not flagged.
          Mirrored as ``existing_paths <= task_paths`` — a subset of the task
          (INCLUDING the empty set) is excluded, exactly like beets — so a
          childless library album row in the (artist, title) bucket never
          surfaces as a false "album-level" twin prompt.
        * symbol-only / whitespace-only title — a value ``_fuzzy_part`` leaves
          empty is a "no usable key" on that half, so it contributes no match
          (never "matches every other such album"), mirroring
          ``_grouping_signals``'s ``artist or title`` guard.

        :param task: the album task being resolved.
        :param lib: the beets Library (``session.lib``).
        :param exact_find: the pristine beets bound method (already captured
          by :meth:`_install_dup_guard` before the wrapper shadows it).
        :return: beets' exact hits, plus any variant twins it missed.
        """
        exact: list[Any] = list(exact_find(lib))
        info = task.chosen_info()
        artist = info.get("artist")
        album = info.get("album")
        if artist is None or album is None:
            # as-is/no-artist guard (mirrors beets) and no album name: nothing
            # to compare on the variant side.
            return exact
        artist_key = _fuzzy_part(str(artist))
        title_key = _fuzzy_part(str(album))
        if not artist_key or not title_key:
            # a half that normalizes to empty is a "no usable key", not "matches
            # everything" (symbol/whitespace-only titles).
            return exact
        task_paths: set[Any] = {i.path for i in task.items if i}
        known: set[Any] = {getattr(a, "id", None) for a in exact}
        out: list[Any] = list(exact)
        for existing in self._variant_album_index(lib).get((artist_key, title_key), ()):
            existing_id = getattr(existing, "id", None)
            if existing_id is None or existing_id in known:
                continue
            # beets' re-import exclusion (tasks.py find_duplicates): an album
            # whose files are ALL in the task (or has none at all, i.e. empty
            # set ⊆ any set) is being re-imported / isn't a file-bearing twin,
            # not duplicated.
            existing_paths: set[Any] = {i.path for i in existing.items()}
            if existing_paths <= task_paths:
                continue
            known.add(existing_id)
            out.append(existing)
        return out

    def choose_match(self, task: ImportTask) -> Any:
        """Auto-apply a strong match; otherwise park and await the user.

        Emits exactly one AlbumOutcome for the album (applied / skipped /
        needs_review) so the API can show it in the live feed. Returns either an
        ``AlbumMatch`` (to apply) or an ``Action`` constant.
        """
        # Pause lands here first: abort BEFORE this album claims a feed index.
        self._check_pause()
        # The duplicate gate MUST be armed before beets' _resolve_duplicates runs
        # (a later stage in the same album task), because it reads
        # task.find_duplicates. Installed exactly ONCE per album task: this is the
        # single choose seam a task passes through before beets hands it to
        # _resolve_duplicates, so no task is armed twice in the normal flow.
        # (_install_dup_guard keeps a pristine stash purely as a cheap safety net
        # so a hypothetical re-install re-wraps the original, not a wrapper; that
        # is not an expected re-entrancy.)
        self._install_dup_guard(task)
        # Flush the PREVIOUS task's library album id (its task.add has run by
        # now — sequential pipeline) before this album claims the feed.
        self._flush_album_ids()
        # The previous album's astracks singleton window is over by now (serial
        # pipeline): clear the flag so this album's own singletons default to
        # SKIP unless it too is decided astracks below.
        self._astracks_in_flight = False
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
        beets' enter-Id). A ``rescan`` choice re-reads the folder and re-runs the
        default lookup, swapping ``task.items`` only when it yields candidates.
        """
        candidate = first_candidate
        revision = 0
        while True:
            choice = self.bridge.park(
                ParkedAlbum(album_index=index, folder=folder, candidate=candidate),
                art_source=art_source,
            )
            is_search = choice.action is ImportAction.search and choice.search is not None
            is_rescan = choice.action is ImportAction.rescan
            # Stale-client race: a prior search re-parked a DIFFERENT candidate
            # list, but the client still renders the old one and submits an apply
            # against it. Re-confirm instead of letting _apply_choice silently
            # import a release the user never chose. Two detectors: the index is
            # out of range for the current list (a search SHRANK it), or the
            # client-echoed search_revision doesn't match the current park (a
            # search REPLACED it with an equal-or-longer list, where a stale
            # in-range index still looks valid). The revision echo closes that
            # residual for revision-echoing clients; a None revision (a legacy/
            # non-echoing client) degrades to the length-only guard. Only apply
            # is revision-checked — skip/asis/astracks/abort are list-independent
            # decisions and must never be blocked by a stale revision.
            is_stale_apply = choice.action is ImportAction.apply and (
                not self._apply_index_in_range(choice, candidates)
                or (choice.search_revision is not None and choice.search_revision != revision)
            )
            if not is_search and not is_rescan and not is_stale_apply:
                result = self._apply_choice(choice, candidates)
                if result is Action.TRACKS:
                    # Arm the astracks window: the serial pipeline delivers this
                    # task's re-pipelined singletons to choose_item (which reads
                    # the flag -> ASIS) before the next choose_match clears it.
                    self._astracks_in_flight = True
                return result
            revision += 1
            feedback: str | None
            if is_stale_apply:
                # The chosen index no longer exists (a prior search shrank the list
                # out from under the client). Keep the current candidates and re-park
                # so the user re-confirms rather than importing the wrong release.
                feedback = "That release is no longer in the list - please pick again."
            elif is_search:
                assert choice.search is not None  # is_search narrowed it above
                new_candidates, new_rec = relookup(task, choice.search)
                if new_candidates:
                    candidates = new_candidates
                    task.candidates = candidates
                    recommendation = _REC_MAP.get(new_rec, Recommendation.none)
                    feedback = None
                else:
                    feedback = "No release found. Showing your previous matches."
            elif not folder or not self._under_toppath(folder):
                # Rescan guard: an empty folder, or one outside every session
                # toppath (a MERGE task's library-spanning ancestor), must never
                # be os.walk'd — that can traverse the whole library mount and
                # swap task.items to every file under it. Refuse instead.
                feedback = "Rescan isn't available for this album."
            else:
                # Rescan: the user changed the folder on purpose — re-read it
                # from disk and re-run beets' DEFAULT first-scan lookup.
                new_items = _read_items(Path(folder))
                if not new_items:
                    feedback = "No audio files remain in the folder. Skip or Abort."
                else:
                    cur_artist, cur_album, new_candidates, new_rec = lookup_items(new_items, None)
                    if not new_candidates:
                        # The live payload cannot represent a candidate-less park,
                        # and a half-swap would let Apply import deleted files —
                        # keep the task fully consistent on its original scan.
                        feedback = (
                            "No release matched the rescanned folder; "
                            "showing the album as originally scanned."
                        )
                    else:
                        task.items = new_items
                        task.cur_artist = cur_artist
                        task.cur_album = cur_album
                        candidates = new_candidates
                        task.candidates = candidates
                        recommendation = _REC_MAP.get(new_rec, Recommendation.none)
                        # The deleted file may have carried the embedded cover —
                        # re-detect so the re-park and the cover endpoint stay true.
                        art_source = self._first_item_art_source(new_items)
                        has_current_art = (
                            art_source is not None and embedded_art(art_source) is not None
                        )
                        feedback = None
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

    @staticmethod
    def _apply_index_in_range(choice: ImportChoice, candidates: list[Any]) -> bool:
        """True iff an apply choice's index selects one of the CURRENT candidates.

        A ``None`` index means "apply the top" and is in range whenever any
        candidate exists; an explicit index must fall within the current list,
        which a prior search may have shortened out from under the client.
        """
        return 0 <= (choice.candidate_index or 0) < len(candidates)

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
            # Defensive net only: _park_with_research now intercepts an out-of-range
            # apply as a stale submit and re-parks, so this is unreachable for the
            # attended path — but any other caller still degrades to the top match.
            return candidates[0]
        if choice.action is ImportAction.asis:
            return Action.ASIS
        if choice.action is ImportAction.astracks:
            return Action.TRACKS
        return Action.SKIP

    def _under_toppath(self, path: str) -> bool:
        """True iff ``path`` is, or lives inside, one of the session toppaths (the
        user-chosen import-source roots). Distinguishes a real source folder from
        the library-spanning ancestor a MERGE task's mixed paths would produce."""
        for raw in self.paths:
            top = os.fsdecode(raw)
            if path == top or path.startswith(top + os.sep):
                return True
        return False

    def _task_folder(self, task: ImportTask) -> str:
        # The album's source folder = the common parent of the task's paths. For a
        # one-folder album this is that folder; for a multi-disc task whose paths
        # are [CD1, CD2, CD3] (a deemix layout excludes the parent) it is the
        # album dir — NOT paths[0]=CD1, which would bank/re-import only disc 1.
        #
        # A MERGE decision makes beets rebuild the task as ImportTask(None,
        # source_paths + duplicate LIBRARY file paths); the naive common-parent of
        # an inbox folder and a library file escapes to a bogus ancestor ("/"),
        # which the feed would show and a Rescan would os.walk across the whole
        # library. Scope to the paths under a session toppath so the folder stays
        # the real incoming source; fall back to the full set only when nothing is
        # under a toppath (a degenerate / library-reimport shape).
        if not task.paths:
            return ""
        decoded = [os.fsdecode(p) for p in task.paths]
        scoped = [p for p in decoded if self._under_toppath(p)]
        candidates = scoped or decoded
        try:
            return os.path.commonpath(candidates)
        except ValueError:  # mixed/relative paths — never happens for beets toppaths
            return candidates[0]


def run_import_worker(
    session: WebImportSession,
    *,
    move: bool | None = None,
    sweep: bool = False,
    directive: BankApplyDirective | None = None,
) -> None:
    """Run one import session serially on the calling (worker) thread.

    Binds ``lib.music_dir_context()`` around the whole body so the rows this
    import writes are stored music-dir-relative, exactly as ``beet import``
    stores them. This is the shared chokepoint of both callers — the job runner's
    thread target AND ``trash_manage.restore_album``, which calls this directly —
    so it is the one place that can cover them together; see the inline comment.

    Forces single-threaded execution, ``import.duplicate_action: ask`` (so the
    duplicate hook always fires, regardless of the user's config — the web review
    IS the "ask") and ``import.autotag: yes``, then runs beets. After run()
    returns, moves any album the Replace action recorded to the reversible Trash,
    by stable id — beets imports the new album first, so the old copy is only
    touched once the new one is safe.

    ``autotag`` is forced (snapshot/restore, like the flags below) because beets
    swaps the ``lookup_candidates`` + ``user_query`` stages for ``import_asis``
    when it is off, and ``user_query`` is the ONLY stage that calls
    ``choose_match``. That hook is where every album outcome, bank row and
    landed-album-id follow-up originates, so a user config of ``autotag: no``
    (settable from MusicDrop's own config editor) makes beets import the files
    for real while the app records nothing: an empty review feed, a sweep that
    banks nothing yet history-marks every folder done, and a Trash restore that
    reports ``could_not_restore`` after it has already emptied the folder. Same
    silent-loss class as the ``singletons`` forcing below, but total.

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
    # Bind the music dir for the WHOLE body: beets relativises an item's path
    # on write only when its ``music_dir`` ContextVar is set, and ``Library``
    # arms that var solely in the context that OPENED the library (the FastAPI
    # lifespan). Every caller reaches this on a worker thread, which inherits
    # nothing -- so without this bind beets stores absolute paths and the
    # library stops resolving the day the music dir moves. Both callers are
    # covered here: the job runner AND trash_manage.restore_album, which calls
    # this directly. Nesting is safe (beets binds via a ContextVar token).
    with session.lib.music_dir_context():
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
        orig_autotag = config["import"]["autotag"].get(bool)
        config["import"]["autotag"] = True
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
            config["import"]["autotag"] = orig_autotag
        # The album is in the library the moment session.run() returns; a failure
        # moving a Replace-superseded copy to Trash must annotate, not invalidate.
        # Reporting a committed import as failed would re-trigger duplicate
        # detection against the just-imported album on retry.
        try:
            _trash_replaced_albums(session)
        except Exception:
            logger.exception("post-import Trash cleanup failed; the old copy stayed in place")


def _trash_replaced_albums(session: WebImportSession) -> None:
    """Move every Replace-recorded existing album to Trash (post-run, by id).

    Synchronous library primitive on the worker thread — NOT the async
    resolve_duplicates_op (which gates on has_active_job + the swap lock and would
    deadlock/409 against this in-flight import). A missing album (already gone) is
    skipped, not an error.

    Binds ``lib.music_dir_context()`` for the loads + moves: beets expands
    DB-relative item paths via a ``ContextVar`` set when the ``Library`` is opened
    (the main thread). This runs on the import worker thread, which does not
    inherit that ``ContextVar``, so without the bind ``Album.move`` gets a relative
    source path and raises ``FileNotFoundError`` (same root cause as the /duplicates
    resolve path).

    Its sole caller (``run_import_worker``) now binds the same context around its
    whole body, so this bind is nested and redundant *today*. It is kept, not
    removed: nesting costs nothing (beets binds via a ContextVar token, so the
    inner ``with`` restores rather than clears), and keeping it means this
    library primitive stays correct on its own terms instead of silently
    depending on a caller that a future refactor could change.
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
