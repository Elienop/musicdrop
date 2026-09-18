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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar, cast

from beets import config
from beets.autotag.match import Recommendation as BeetsRec
from beets.importer.actions import Action
from beets.importer.actions import DuplicateAction as BeetsDuplicateAction
from beets.importer.session import ImportAbortError, ImportSession

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
from app.beets.import_operation import (
    configured_file_operation,
    file_flags,
    forced_file_operation,
)
from app.beets.library import (
    LibraryRootUnavailableError,
    _abs_path,
    _require_id,
    duplicate_albums_still_present,
    require_library_root,
)
from app.beets.merge_preview import build_merge_preview
from app.beets.release_identity import release_identity
from app.beets.relookup import relookup
from app.beets.research import _read_items, lookup_items
from app.beets.store_layout import (
    StoreLayoutError,
    check_store_layout,
    lib_music_and_library,
)
from app.beets.trash import album_format_bitrate, trash_album
from app.config import settings
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
from app.playlists.reexport import reexport_playlists_containing_sync

if TYPE_CHECKING:
    from beets.importer.tasks import ImportTask

logger = logging.getLogger(__name__)

#: Operator-facing records go to ``uvicorn.error``, not this module's logger:
#: under the Dockerfile CMD uvicorn's LOGGING_CONFIG leaves app-namespace
#: loggers at WARNING, so an app-namespace INFO record is dropped entirely
#: and never reaches ``docker logs`` (``main._boot_log`` documents the same
#: trap). ``logger`` keeps the warnings/exceptions, which do get through.
operator_logger = logging.getLogger("uvicorn.error")


class ImportConfigBusyError(RuntimeError):
    """Another import owns the process-global beets import config right now."""


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


_ReplyT = TypeVar("_ReplyT")


@dataclass
class _ReplySlot(Generic[_ReplyT]):
    """One park's rendezvous: the worker's reply queue plus whether it was answered.

    ``answered`` is set by the CONSUMER, in the same critical section as the put,
    and read by ``has_unanswered_park``. Reading the queue itself instead would
    misreport the gap between ``reply.get()`` returning and the worker deleting
    the slot: the queue is empty again there, yet the answer has landed and
    nobody is waiting on a person. A slot is released by identity when its park
    returns, so a re-park at the same index starts out unanswered again.
    """

    reply: queue.Queue[_ReplyT]
    answered: bool = False


def _answer(slot: _ReplySlot[_ReplyT], answer: _ReplyT, taken: str) -> None:
    """Deliver ``answer`` into ``slot`` and mark it answered (caller holds the lock).

    One helper for both channels because the two steps belong together: the mark
    shares the put's critical section, so a drain that pops this park afterwards
    cannot read the woken worker as still blocked. ``put_nowait`` on a maxsize-1
    queue does not block, and the queue's own mutex sits below the bridge lock,
    so holding that lock across the put adds no ordering. ``taken`` is the
    RuntimeError message for a slot that already holds an unconsumed answer
    (already ``answered``; the API maps it to 409).
    """
    try:
        slot.reply.put_nowait(answer)
    except queue.Full:
        raise RuntimeError(taken) from None
    slot.answered = True


class ImportBridge:
    """Thread-safe bridge between the import worker and an async consumer.

    The worker (running beets) pushes a ``ParkedAlbum`` to ``_out`` and blocks on
    a per-album reply slot. A consumer drains ``_out``, presents the candidate,
    and calls ``push_choice`` to unblock the worker with an ``ImportChoice``.
    """

    def __init__(self) -> None:
        self._out: queue.Queue[ParkedAlbum] = queue.Queue()
        self._outcomes: queue.Queue[AlbumOutcome] = queue.Queue()
        self._replies: dict[int, _ReplySlot[ImportChoice]] = {}
        # NOT popped on unblock (unlike _replies): it serves GET /cover during the
        # parked review window. Growth is bounded - single-slot registry, one
        # active job, a fresh ImportBridge per import is GC'd with the old job.
        self._art_source: dict[int, str] = {}
        # Parallel park channel for duplicate prompts — same maxsize-1 reply
        # rendezvous as the candidate channel, kept separate so the two payload
        # types (ParkedAlbum vs DuplicatePrompt) stay typed.
        self._dup_out: queue.Queue[DuplicatePrompt] = queue.Queue()
        self._dup_replies: dict[int, _ReplySlot[DuplicateDecision]] = {}
        self._lock = threading.Lock()
        self._pending = 0
        # Sweep pause flag: set by the registry's request_pause (consumer
        # side), read by the session at the top of every decision hook (worker
        # side). It lives on the bridge because the bridge is the one object
        # both sides already share - the registry never holds the session.
        self._pause = threading.Event()
        # Folders beets' task factory skipped as already imported (incremental
        # history). Monotone; every job state reports it, sweep or not.
        self._known_skips = 0

    # ----- worker side -----

    def park(self, parked: ParkedAlbum, art_source: str | None = None) -> ImportChoice:
        """Push a parked album and block until a choice arrives for it."""
        slot: _ReplySlot[ImportChoice] = _ReplySlot(queue.Queue(maxsize=1))
        with self._lock:
            self._replies[parked.album_index] = slot
            if art_source is not None:
                self._art_source[parked.album_index] = art_source
            self._pending += 1
        self._out.put(parked)
        choice = slot.reply.get()  # blocks the worker thread
        with self._lock:
            # Release this slot by IDENTITY, not by key: the slot standing at
            # this index may not be ours. A re-park registers its own slot
            # before queueing it, so a pop by key alone can delete a LIVE slot -
            # its push_choice then raises KeyError, its worker never unblocks,
            # and ``has_unanswered_park`` reads the blocked worker as nobody
            # waiting. Reaching that takes two threads parking one index, which
            # today's single worker cannot do on its own (it re-parks only after
            # this line); the gated tests in test_import_session construct it.
            if self._replies.get(parked.album_index) is slot:
                del self._replies[parked.album_index]
            self._pending -= 1
        return choice

    def park_duplicate(
        self, prompt: DuplicatePrompt, art_source: str | None = None
    ) -> DuplicateDecision:
        """Push a duplicate prompt and block until a decision arrives for it."""
        slot: _ReplySlot[DuplicateDecision] = _ReplySlot(queue.Queue(maxsize=1))
        with self._lock:
            self._dup_replies[prompt.album_index] = slot
            if art_source is not None:
                self._art_source[prompt.album_index] = art_source
            self._pending += 1
        self._dup_out.put(prompt)
        decision = slot.reply.get()  # blocks the worker thread
        with self._lock:
            # By identity, for the reason spelled out in park().
            if self._dup_replies.get(prompt.album_index) is slot:
                del self._dup_replies[prompt.album_index]
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
            slot = self._replies.get(album_index)
            if slot is None:
                raise KeyError(f"no album parked at index {album_index}")
            _answer(slot, choice, f"album {album_index} already has a pending choice")

    def push_duplicate_decision(self, album_index: int, decision: DuplicateDecision) -> None:
        """Deliver a duplicate decision to the worker blocked on ``album_index``."""
        with self._lock:
            slot = self._dup_replies.get(album_index)
            if slot is None:
                raise KeyError(f"no duplicate parked at index {album_index}")
            _answer(slot, decision, f"duplicate {album_index} already has a pending decision")

    def has_unanswered_park(self) -> bool:
        """True while a park is registered and no answer has been delivered into it.

        The signal behind ``ImportJobState.awaiting_decision``: a worker sitting
        in ``park``/``park_duplicate`` with nothing on its way. Both channels
        count — an attended duplicate blocks the worker exactly as an uncertain
        match does. Two things this deliberately does NOT read:

        * ``pending_count``, which the WORKER decrements after ``reply.get()``
          returns, so a poll fired straight after a choice can still see a
          working import as blocked. ``answered`` flips on the consumer's push
          instead, so the falling edge lands with the answer.
        * the park queues, which the consumer pops one-shot. Popping a park is
          not proof its worker is still waiting: a choice pushed between the
          registration and the queueing above is delivered to a live slot, and
          the pop that follows then describes a worker that has already run on.
        """
        with self._lock:
            return any(not slot.answered for slot in self._replies.values()) or any(
                not slot.answered for slot in self._dup_replies.values()
            )

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


#: A Replace that never got as far as moving anything: the Trash pair is not
#: wired, or the store layout is refused.
_REPLACE_NO_TRASH = "Replace could not use the Trash folder. Nothing was imported."
#: A Replace whose old copy could not even be CLASSIFIED: some path under it
#: answered neither "here" nor "gone" (EACCES, EIO, a stale handle).
_REPLACE_UNREADABLE = (
    "Replace could not read the old copy's files, so nothing was moved or imported."
)


def _replace_partial_note(completed: int, total: int) -> str:
    """Why a Replace imported nothing — naming how many old copies did move.

    The partial state is real: a failure on the second of two duplicates leaves
    the first in Trash with its origin record, so "Replace failed" alone would
    hide a folder that has left the library.

    ``completed`` counts albums whose ``trash_album`` RETURNED. That call can
    also raise after the files have already moved (the origin-record write, or
    ``album.remove``), so the zero case says the step failed rather than
    claiming nothing moved — it cannot tell, and neither can the caller.
    """
    if completed:
        return (
            f"Replace moved {completed} of {total} old copies to Trash, then failed."
            " Nothing was imported."
        )
    return "Replace failed while moving the old copy to Trash. Nothing was imported."


def _store_layout_ok(lib: Any, *, trash_dir: Path, origins_dir: Path, refusal: str) -> bool:
    """Is the music / beets / Trash / origins layout still one we will move into?

    Both Replace routes re-check it for the reason the request paths do: the pair
    was resolved once, when the registry was handed the library at lifespan or
    after an Apply, and an import can run hours later.

    ``BEETSDIR``, not ``settings.beets_dir``: the setting's default is the
    RELATIVE "data/beets", which would resolve against a worker thread's CWD.
    ``setup_beets`` exports the resolved dir and re-exports it on every Apply.

    The cause is shared, the CONSEQUENCE is not — one route imports nothing, the
    other leaves an already-imported album's old copy in the library — so the
    caller supplies the whole sentence. It is logged here, where the exception is
    still live and ``exc_info`` can name which pair was refused.
    """
    try:
        music_dir, library_path = lib_music_and_library(lib)
        check_store_layout(
            music_dir=music_dir,
            beets_dir=Path(os.environ.get("BEETSDIR") or settings.beets_dir),
            trash_dir=trash_dir,
            origins_dir=origins_dir,
            library_path=library_path,
            settings=settings,
        )
    except StoreLayoutError:
        logger.warning(refusal, exc_info=True)
        return False
    return True


def _album_file_paths(album: Any) -> list[Any]:
    """Every path the album names: its tracks' files, plus its cover art.

    For the IDENTITY question only (:func:`_file_identities`) — "does this album
    reach any of the same bytes as one this run landed?" — where the art counts,
    because moving an album takes its art with it. Presence is a different
    question and asks the items alone (:func:`_album_file_state`).
    """
    paths = [item.path for item in album.items()]
    paths.append(getattr(album, "artpath", None))
    return [path for path in paths if path]


class _FileState(StrEnum):
    """What a duplicate album's files answered when asked where they are."""

    present = "present"
    absent = "absent"
    unknown = "unknown"


def _album_file_state(lib: Any, album: Any) -> _FileState:
    """Three-valued: are this album's TRACK files here, gone, or unanswerable?

    ``os.path.exists`` collapses all three into False — it is False for EACCES,
    EIO and a stale handle as much as for ENOENT, and False for a dangling
    symlink whose link file is right there. Reading any of those as "gone" is
    what lets a Replace drop the rows of an album whose files are all present
    (measured by the code seat at 0o444 and 0o000).

    ``os.lstat``, so a dangling symlink counts as PRESENT: the entry exists and
    is the album's, whatever it points at.

    The ITEMS only, deliberately, and ``album.artpath`` is not asked (owner
    ruling, fix round 1b). An album with no track file left is a ghost even when
    its cover survived — "deleted the bad rip, kept cover.jpg, imported the good
    one, pressed Replace" has to work. The ghost arm drops the rows without
    touching any file, so that cover stays exactly where the user left it: it is
    never moved and never deleted (``Album.remove(delete=False)`` skips the art
    path, ``library/models.py:396-400``), which is what the security finding
    asked for. Counting it instead made the Replace REFUSE, because the mover
    cannot move art on its own either
    (``test_a_ghost_whose_cover_survived_is_replaced_and_its_cover_left_alone``).
    """
    state = _FileState.absent
    for raw in (item.path for item in album.items() if item.path):
        try:
            os.lstat(_abs_path(lib, raw))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            return _FileState.unknown
        state = _FileState.present
    return state


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
        trash_origins_dir: Path | None = None,
        unattended: bool = False,
        sweep: bool = False,
        bank_dir: Path | None = None,
        directive: BankApplyDirective | None = None,
        playlists_dir: Path | None = None,
    ) -> None:
        super().__init__(lib, loghandler, paths, query)
        self.bridge = bridge
        # Counter that assigns each parked album a stable index for replies.
        self._album_index = 0
        # Where Replace moves the old copies (reversible Trash). None = unwired,
        # and a Replace then refuses rather than importing a second copy.
        self._trash_dir = trash_dir
        # Its sibling origin store. Wired as a PAIR with ``trash_dir`` from the
        # one resolve point (the runner / the restore path), and both Replace
        # paths skip unless BOTH are set — a Replace that trashed the old copies
        # without recording where they came from would leave rows that can only
        # ever be re-imported.
        self._trash_origins_dir = trash_origins_dir
        # Banked replace targets the duplicate hook never saw, trashed AFTER
        # run() by _trash_replaced_albums. The hook's own Replace moves its
        # duplicates BEFORE beets places anything and records nothing here.
        self._replace_album_ids: set[int] = set()
        # Library album ids the duplicate hook already disposed of. Read by the
        # banked seed so it does not re-enforce a copy that has left.
        self._hook_replaced_album_ids: set[int] = set()
        # Latched by the first refused Replace. The banked seed enforces nothing
        # once it is set: a Replace that failed half-way must not have its
        # remaining targets trashed by the post-run pass while nothing was
        # imported for them.
        self._replace_was_refused = False
        # Item ids this run dropped from the library, from BOTH replace routes.
        # run_import_worker re-exports the affected playlists once, after the
        # run, when the ids the new album took are settled.
        self._dropped_item_ids: set[int] = set()
        # Library album ids beets actually ADDED during this run (filled by
        # _flush_album_ids from task.album). Gates the banked-replace seed:
        # a pinned lookup that resolves nothing SKIPs while run() still returns
        # normally, and trashing the library copies then would leave the user
        # with no copy at all.
        self._landed_album_ids: set[int] = set()
        # The owned-playlist store, threaded from the runner exactly like
        # trash_dir. Set = a Replace also re-exports the `.m3u8` of every
        # playlist that held a track from a replaced album; None = unwired
        # (tests, fakes) and the re-export is skipped.
        self._playlists_dir = playlists_dir
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
        """Count folders beets skips as already imported.

        beets' task factory consults this per prospective album folder BEFORE
        any session hook fires, so history-skipped folders never reach the
        outcome stream - this override is the only seam that sees them. The
        count rides the bridge and every job state reports it.

        beets answers True for a RESUMED folder too, not only a history one
        (``importer/session.py:246-256``), and that arm needs the user's own
        ``resume: yes`` (``should_resume`` above returns False, so ``ask`` does
        not reach it). Every arm ``run_import_worker`` forces pins
        ``resume: False``; a run it leaves alone can count a resume skip here.
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
        # mode (inbox, sweep, non-astracks directives) keeps SKIP; the import
        # worker forces import.singletons off on EVERY run so a singletons:yes
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
        the bank. The release the task was MATCHED to is banked with it (see
        _matched_release_payload) so that later decision replays this match
        instead of re-running the lookup.

        Our four model actions map onto beets' enum:
        skip_new→SKIP, keep_both→KEEP, merge→MERGE, and replace→KEEP once WE have
        disposed of the old copy (see :meth:`_replace_duplicates_now`), or SKIP
        when it could not be disposed of.
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
            # tuple → crashes the whole import job) nor hand their ids to the
            # replace arm (it would Trash the album that shares that id).
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
            return self._beets_dup_action(dup_action, found_duplicates, task=task, index=index)
        if self.unattended:
            if self.sweep:
                # Bank the prompt the attended flow would park: the user
                # resolves skip/keep/replace/merge later from the Review page.
                rec = task.rec if task.rec is not None else BeetsRec.none
                recommendation = _REC_MAP.get(rec, Recommendation.none)
                self._bank_row(
                    task,
                    reason="needs_dup_resolution",
                    recommendation=recommendation,
                    confidence=_confidence(task.match.distance) if task.match is not None else 0.0,
                    duplicate=prompt,
                    # ...and the release this album was MATCHED to, so the
                    # apply replays it instead of re-running the lookup.
                    parked=self._matched_release_payload(
                        task,
                        index=index,
                        recommendation=recommendation,
                        has_current_art=incoming.has_current_art,
                    ),
                )
            # Unattended: the outcome above records the set-aside; SKIP the new
            # album (keeps the library copy) without parking + blocking.
            return BeetsDuplicateAction.SKIP
        decision = self.bridge.park_duplicate(prompt, art_source=art_source)
        return self._beets_dup_action(decision.action, found_duplicates, task=task, index=index)

    def _beets_dup_action(
        self,
        action: DuplicateAction,
        found_duplicates: Any,
        *,
        task: ImportTask,
        index: int,
    ) -> BeetsDuplicateAction:
        """Translate our model DuplicateAction into beets' enum.

        ``replace`` is the only arm that does work of its own: it moves the old
        copies to Trash before beets is answered (:meth:`_replace_duplicates_now`).
        """
        if action is DuplicateAction.skip_new:
            return BeetsDuplicateAction.SKIP
        if action is DuplicateAction.merge:
            # Loop-safe: the merged task carries the duplicate's paths, so beets'
            # find_duplicates excludes the old album next time and record_replaced
            # absorbs its rows.
            return BeetsDuplicateAction.MERGE
        if action is DuplicateAction.replace:
            return self._replace_duplicates_now(found_duplicates, task=task, index=index)
        # keep_both: import alongside the existing copy.
        return BeetsDuplicateAction.KEEP

    def _replace_duplicates_now(
        self, found_duplicates: Any, *, task: ImportTask, index: int
    ) -> BeetsDuplicateAction:
        """Dispose of the albums the user was shown, then answer beets ``KEEP``.

        **Timing.** beets resolves duplicates at ``importer/stages.py:276`` and
        places files at ``:293``, so the disposal here happens while the new album
        is still only in the download folder. That is what fixes the same-file
        case: under ``hardlink``/``link`` the incoming file and the library file
        are one file, so beets reuses the old album's paths for the new rows, and
        a Trash move made afterwards moves the new album's own files (pinned by
        ``test_replacing_a_duplicate_leaves_an_album_whose_files_exist``).

        **Why not beets' own REMOVE.** ``ImportTask.remove_duplicates``
        (``tasks.py:246``) RE-RUNS ``find_duplicates`` rather than reusing the
        list this hook was handed, and the query key can move in between:
        ``task.add`` → ``align_album_level_fields`` rewrites ``albumartist`` for
        an ASIS task, so an as-is compilation shown as duplicating ``('A', 'Comp
        X')`` had ``('Various Artists', 'Comp X')`` hard-deleted outside Trash
        (measured by the security seat; pinned by
        ``test_an_as_is_compilation_replace_leaves_the_album_the_user_never_saw``).
        Answering KEEP means that method never runs, so the set disposed of here
        is exactly the set the user consented to.

        **The disposal.** Every duplicate is classified FIRST
        (:func:`_album_file_state`), so a refusal costs nothing: an album whose
        files cannot be read at all refuses the whole Replace before anything
        moves. Then, in one all-or-nothing pass, an album with track files goes
        to ``trash_album`` and one with none has its rows dropped by beets'
        documented ``album.remove(delete=False)``, which touches no file — so a
        cover left behind in that folder is neither moved nor deleted. Any
        failure answers SKIP: nothing is imported and whatever has not moved
        stays in the library.

        The root check comes first, at the decision moment
        ``require_library_root`` names: "this album has no file on disk" is
        exactly the reading that goes library-wide wrong when the share drops.
        """
        try:
            require_library_root(self.lib)
        except LibraryRootUnavailableError as exc:
            # With the root gone EVERY duplicate reads as file-less, and the
            # file-less arm drops rows on a library that is merely unreachable.
            logger.warning("import replace: %s", exc)
            return self._replace_refused(task, index, f"{exc} Nothing was imported.")
        states = [(album, _album_file_state(self.lib, album)) for album in found_duplicates]
        if any(state is _FileState.unknown for _, state in states):
            logger.warning(
                "import replace: a library copy's files could not be read; nothing was imported"
            )
            return self._replace_refused(task, index, _REPLACE_UNREADABLE)
        note = self._dispose_duplicates_now(states)
        if note is None:
            return BeetsDuplicateAction.KEEP
        return self._replace_refused(task, index, note)

    def _replace_refused(self, task: ImportTask, index: int, note: str) -> BeetsDuplicateAction:
        """Say why this Replace imported nothing, then answer beets SKIP.

        Also latches ``_replace_was_refused`` for the run: the banked seed must
        not enforce a replace target this hook already failed on, or the post-run
        pass would trash the user's copy while its replacement was never
        imported (pinned by
        ``test_a_refused_replace_stops_the_banked_seed_for_the_rest_of_the_run``).
        """
        self._replace_was_refused = True
        self.bridge.note_outcome(self._dup_outcome(index, task).model_copy(update={"note": note}))
        return BeetsDuplicateAction.SKIP

    def _dispose_duplicates_now(self, states: list[tuple[Any, _FileState]]) -> str | None:
        """Trash or empty each classified duplicate. ``None`` on success, else why not.

        Two duties beyond the disposal itself: re-check the store layout (the
        Trash pair was resolved when the registry was handed the library, possibly
        hours ago), and read each album's item ids BEFORE its rows go, so the one
        playlist re-export at the end of the run knows what left.
        """
        trash_dir = self._trash_dir
        origins_dir = self._trash_origins_dir
        if trash_dir is None or origins_dir is None:
            logger.warning("import replace: no Trash folder is wired; nothing was imported")
            return _REPLACE_NO_TRASH
        lib = self.lib
        if not _store_layout_ok(
            lib,
            trash_dir=trash_dir,
            origins_dir=origins_dir,
            refusal="import replace: the store layout is refused; nothing was imported",
        ):
            return _REPLACE_NO_TRASH
        completed = 0
        try:
            for album, state in states:
                item_ids = [_require_id(item.id) for item in album.items()]
                if state is _FileState.present:
                    trash_album(lib, album, trash_dir=trash_dir, origins_dir=origins_dir)
                else:
                    # No track file left: beets' own remove drops the rows and
                    # touches no file, so a cover the user kept in that folder
                    # stays where it is — not moved, not deleted.
                    album.remove(delete=False)
                self._hook_replaced_album_ids.add(int(album.id))
                self._dropped_item_ids.update(item_ids)
                completed += 1
        except Exception:
            logger.exception("import replace: disposing of a library copy failed")
            return _replace_partial_note(completed, len(states))
        return None

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

    def _library_change_signature(self, lib: Any) -> tuple[int, int]:
        """(lib.revision, PRAGMA data_version): the cache-invalidation probe.

        Two components, complementary by construction (both verified by
        direct measurement against beets 2.13.1 / SQLite):

        * ``lib.revision`` — beets' in-memory mutation counter,
          ``dbcore/db.py``: every ``Transaction.__exit__`` adds ``_mutated``
          (nested transactions included, so one logical operation may bump it
          by 2-6; reads and no-op stores add 0). It catches every mutation
          made THROUGH THIS ``Library`` object — including an in-place album
          rename and the delete-then-reinsert a merge performs, where SQLite
          reuses the deleted max rowid and no row-set aggregate can see the
          swap (``id INTEGER PRIMARY KEY`` without AUTOINCREMENT reuses ids).
          A stale index across such a swap is not a cosmetic miss: the cached
          Album object is a ghost bound to an id that now names a different
          album, a duplicate prompt built from it shows one album, and a
          ``replace`` decision would trash the other.
        * ``PRAGMA data_version`` — SQLite's own signal for exactly the
          remaining case: it changes when ANY OTHER connection commits (a
          stray ``beet`` CLI, another process — including a foreign
          delete-then-reinsert that leaves every aggregate identical, and
          foreign in-place edits), never for this connection's own writes
          (those are ``revision``'s job). Single value, so the probe cannot
          tear; O(1) regardless of table size (~6µs measured); the VALUE is
          documented as unpredictable, so it is only ever compared for
          change, which is all the equality tuple does.

        The ``Library`` object is swapped only by the config editor's Apply,
        which is import-gated — within one session run the object is stable,
        and a swap resets ``revision`` to a value that compares unequal, the
        safe direction (spurious rebuild).

        :param lib: the beets Library (``session.lib``).
        :return: the (revision, data_version) pair to cache-check against.
        """
        with lib.transaction() as tx:
            data_version = int(tx.query("PRAGMA data_version")[0][0])
        return (int(lib.revision), data_version)

    def _variant_album_index(self, lib: Any) -> dict[tuple[str, str], tuple[Any, ...]]:
        """Normalized (artist, title) -> album rows, cached per session run.

        Rebuilt whenever :meth:`_library_change_signature` changes, returned
        read-through while it does not. Two workloads, stated honestly (both
        measured by review):

        * A run whose tasks mostly SKIP (an all-duplicate sweep, a re-scan):
          the library never changes, the index builds once, later guarded
          calls cost the ~µs probe plus an O(1) lookup. Review measured the
          win at 5,000x-26,000x per call (3k-20k albums, sparse rows) — the
          exact ratio depends on row width, so treat it as "rebuild cost
          amortized away", not a fixed number.
        * A run whose tasks mostly APPLY: every ``task.add`` mutates the
          library, so the NEXT guarded call rebuilds — one full
          ``lib.albums()`` pass per applied album, the same order of work the
          pre-cache code paid. ``_fuzzy_part`` is memoized at the module level
          (see ``duplicates.py``), so repeat rebuilds re-fetch rows but do not
          re-run the regex ladder on unchanged strings; the fetch, not the
          normalization, is the remaining cost.

        Invariant this cache must honor: an album added (or merged away and
        re-minted) by an earlier task in the same run must be visible to later
        tasks' guards — ``lib.revision`` in the signature guarantees rebuild
        after every in-process mutation, and ``PRAGMA data_version`` after any
        other connection's commit, so the index never serves an album object
        whose row has since been rewritten by anyone.

        :param lib: the beets Library (``session.lib``).
        :return: a mapping ``(artist_key, title_key) -> tuple of Albums``.
        """
        # The cached value is (signature, index). It lives on the instance for
        # the session run's lifetime; a fresh WebImportSession is per run, so
        # "at most once per session run" falls out of per-instance state. We
        # do not declare it in __init__ — the gate's tests construct the
        # session via __new__ and set attrs manually, and beets ImportTask
        # precedent already uses dynamic attrs the same way (mypy attr-
        # defined is a known trade-off for these beets-side seams). The cast
        # asserts the shape mypy cannot see through getattr; only this method
        # reads and writes the attribute, keeping the assertion one-sided.
        cached = cast(
            "tuple[tuple[int, int], dict[tuple[str, str], tuple[Any, ...]]] | None",
            getattr(self, "_variant_index_cache", None),
        )
        sig = self._library_change_signature(lib)
        if cached is not None and cached[0] == sig:
            return cached[1]

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
        access after the ~µs change probe) rather than rescan the whole
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
            is_stale_apply = self._park_stale_apply(choice, candidates, revision)
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
                candidates, recommendation, feedback = self._park_search(
                    task, choice, candidates, recommendation
                )
            else:
                (
                    candidates,
                    recommendation,
                    art_source,
                    has_current_art,
                    feedback,
                ) = self._park_rescan(
                    task, folder, candidates, recommendation, art_source, has_current_art
                )
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

    def _park_stale_apply(self, choice: ImportChoice, candidates: list[Any], revision: int) -> bool:
        """Stale-apply detector for the attended re-park loop (see _park_with_research)."""
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
        return choice.action is ImportAction.apply and (
            not self._apply_index_in_range(choice, candidates)
            or (choice.search_revision is not None and choice.search_revision != revision)
        )

    def _park_search(
        self,
        task: ImportTask,
        choice: ImportChoice,
        candidates: list[Any],
        recommendation: Recommendation,
    ) -> tuple[list[Any], Recommendation, str | None]:
        """Search choice: re-run the lookup on this worker thread and re-park.

        A successful relookup swaps BOTH the local candidates and task.candidates
        and re-maps the recommendation via _REC_MAP; an empty result keeps the
        previous candidates and sets the exact "No release found." feedback.
        Returns the updated (candidates, recommendation) + the search_feedback.
        """
        assert choice.search is not None  # is_search narrowed it above
        new_candidates, new_rec = relookup(task, choice.search)
        if new_candidates:
            candidates = new_candidates
            task.candidates = candidates
            recommendation = _REC_MAP.get(new_rec, Recommendation.none)
            return candidates, recommendation, None
        return candidates, recommendation, "No release found. Showing your previous matches."

    def _park_rescan(
        self,
        task: ImportTask,
        folder: str,
        candidates: list[Any],
        recommendation: Recommendation,
        art_source: str | None,
        has_current_art: bool,
    ) -> tuple[list[Any], Recommendation, str | None, bool, str | None]:
        """Rescan choice: guard the folder, re-read it, re-run the default lookup.

        Swaps task state (items / cur_artist / cur_album / candidates / art) only
        on a successful candidate-yielding lookup — never a half-swap. Returns the
        updated (candidates, recommendation, art_source, has_current_art) plus the
        search_feedback string (None on success).
        """
        if not folder or not self._under_toppath(folder):
            # Rescan guard: an empty folder, or one outside every session
            # toppath (a MERGE task's library-spanning ancestor), must never
            # be os.walk'd — that can traverse the whole library mount and
            # swap task.items to every file under it. Refuse instead.
            return (
                candidates,
                recommendation,
                art_source,
                has_current_art,
                "Rescan isn't available for this album.",
            )
        # Rescan: the user changed the folder on purpose — re-read it from disk and
        # re-run beets' DEFAULT first-scan lookup.
        new_items = _read_items(Path(folder))
        if not new_items:
            return (
                candidates,
                recommendation,
                art_source,
                has_current_art,
                "No audio files remain in the folder. Skip or Abort.",
            )
        cur_artist, cur_album, new_candidates, new_rec = lookup_items(new_items, None)
        if not new_candidates:
            # The live payload cannot represent a candidate-less park, and a
            # half-swap would let Apply import deleted files — keep the task
            # fully consistent on its original scan.
            return (
                candidates,
                recommendation,
                art_source,
                has_current_art,
                "No release matched the rescanned folder; showing the album as originally scanned.",
            )
        task.items = new_items
        task.cur_artist = cur_artist
        task.cur_album = cur_album
        candidates = new_candidates
        task.candidates = candidates
        recommendation = _REC_MAP.get(new_rec, Recommendation.none)
        # The deleted file may have carried the embedded cover — re-detect so the
        # re-park and the cover endpoint stay true.
        art_source = self._first_item_art_source(new_items)
        has_current_art = art_source is not None and embedded_art(art_source) is not None
        return candidates, recommendation, art_source, has_current_art, None

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
        """Run the import, flush the final task's album id, then seed Replace.

        beets' run() drives the whole sequential pipeline; the LAST task's
        ``task.add`` happens inside it with no later choose_match to flush it,
        so the follow-up is emitted here. Safe after an abort too: beets'
        run() catches ImportAbortError internally, and an aborted task never
        gained ``task.album``, so the flush drops it.

        The banked-replace seed runs AFTER the flush and never before it: it is
        gated on what that flush recorded as landed.
        """
        super().run()
        self._flush_album_ids()
        self._seed_replace_from_directive()

    def _seed_replace_from_directive(self) -> None:
        """Union the BANKED replace targets into the post-run Trash set.

        beets consults ``get_duplicate_action`` only when its own
        ``find_duplicates`` hits, and that query keys on the CHOSEN release's
        albumartist+album. When the library copy has been renamed since banking
        (or the pinned release names it differently) the hook never fires, so
        ``_beets_dup_action`` never records anything and a ``replace`` imports
        the new album while trashing NOTHING - two copies, decision discarded.
        The banked prompt already recorded WHICH albums collide, by id, so this
        seeds the post-run pass from it (``_trash_replaced_albums`` dedupes and
        skips missing albums).

        Three guards protect data; the fourth only silences a log line.

        * **a Replace this run already refused enforces nothing.** Given
          duplicates [A, B] where A was disposed of and B raised, the hook
          answered SKIP and imported nothing — but if some OTHER task in the run
          landed, every guard below passes and the pass would trash B while its
          replacement was never imported (security seat [7]).
        * **something must have landed.** A directive whose pinned lookup
          resolves nothing SKIPs, ``run()`` returns normally, and the trash pass
          still executes - an ungated seed would move the user's only copies to
          Trash while importing nothing.
        * **identity, not just presence.** beets ids are reused SQLite rowids,
          so a stored id whose album was deleted can now name a different
          album; ``duplicate_albums_still_present`` re-checks each one and a
          mismatch is skipped with a warning rather than trashed.
        * **never an id this run just landed.** If the old copy was deleted
          outside the app, the import can be handed its exact rowid - and since
          it is the same album, it would pass the identity check too. Trashing
          it would Trash the album we just imported.

        And a fourth that is a noise filter, not a safety guard: an entry the
        hook already disposed of is dropped first, because SQLite hands the new
        album the freed rowid (measured ``[1] -> landed [1]``) and every such
        entry would otherwise trip the reused-id warning on a healthy run.
        """
        directive = self._directive
        if directive is None or directive.duplicate_action is not DuplicateAction.replace:
            return
        if self._replace_was_refused:
            logger.warning(
                "bank apply replace: a Replace in this run was refused, so the banked "
                "library copies were left in place"
            )
            return
        banked = directive.replace_existing
        if not banked:
            return  # legacy/up-front-resolver row: nothing banked to enforce
        stored = [e for e in banked if e.album_id not in self._hook_replaced_album_ids]
        if not stored:
            return  # the duplicate hook already disposed of every banked copy
        if not self._landed_album_ids:
            logger.warning(
                "bank apply replace: nothing landed, so the %d banked library copy/copies "
                "were left in place",
                len(stored),
            )
            return
        surviving = set(duplicate_albums_still_present(self.lib, stored))
        for entry in stored:
            if entry.album_id not in surviving:
                logger.warning(
                    "bank apply replace: library album %d is gone or no longer matches the "
                    "banked copy (%s - %s); left in place",
                    entry.album_id,
                    entry.album_artist,
                    entry.album,
                )
        reused = surviving & self._landed_album_ids
        for album_id in sorted(reused):
            logger.warning(
                "bank apply replace: library album %d is the album this run just imported "
                "(its id was reused); left in place",
                album_id,
            )
        self._replace_album_ids.update(surviving - self._landed_album_ids)

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
            self._landed_album_ids.add(int(album_id))
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

    def _matched_release_payload(
        self,
        task: ImportTask,
        *,
        index: int,
        recommendation: Recommendation,
        has_current_art: bool,
    ) -> ParkedAlbum | None:
        """The release this task was matched to, as a needs_review row's payload.

        A sweep-banked DUPLICATE row is a decision the pipeline had already
        made: beets only reaches the duplicate hook after the choice is set, so
        ``task.match`` is the exact release this album would have been imported
        as. Persisting it in the SAME ParkedAlbum shape a needs_review row
        carries is what lets ``directive_for`` pin ``import.search_ids`` when
        the user later resolves the collision — the bank's "decide once"
        promise. Without it the apply re-runs the lookup and takes whatever the
        metadata sources rank first that day, which need not be the release the
        user reviewed.

        ``task.match`` is None for the ASIS/RETAG tasks that share this hook
        (beets' ``_resolve_duplicates`` fires for choice_flag in
        ASIS/APPLY/RETAG, and ``set_choice`` nulls the match for those): nothing
        was matched, so there is nothing to pin and the row stays honestly
        unpinned. ``task.candidates`` may still be populated there — pinning its
        top would be exactly the re-run-instead-of-replay bug in reverse.

        The match LEADS the option list so ``options[0]`` is the matched
        release by construction, not by assuming beets left the chosen match at
        the head of ``task.candidates`` (``set_choice`` does not reorder it).
        That matters because the duplicate screen posts no ``candidate_index``,
        and ``_resolve_search_id`` resolves an index-less decision to
        ``options[0]``.
        """
        # Album-shaped by the singleton early-return at the top of this hook, so
        # the match and every candidate is an AlbumMatch; typed as Any because
        # beets types both as the wider AlbumMatch | TrackMatch union (the same
        # reason choose_match annotates its candidate list that way).
        match: Any = task.match
        if match is None:
            return None
        others: list[Any] = [c for c in (task.candidates or []) if c is not match]
        candidate = map_album_match(
            match,
            cur_artist=task.cur_artist,
            cur_album=task.cur_album,
            options=map_candidate_options([match, *others]),
            recommendation=recommendation,
            has_current_art=has_current_art,
        )
        return ParkedAlbum(album_index=index, folder=self._task_folder(task), candidate=candidate)

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


# "One import owns beets right now." Held for the WHOLE of
# ``run_import_worker``, the post-run Trash pass included -- deliberately wider
# than the config mutation it is named for, and it must NOT be narrowed to just
# that. Narrowing it was tried and reverted: ``_trash_replaced_albums`` moves
# albums and drops rows through the same ``Library`` handle after the config has
# already been restored, so releasing between ``run()`` and that pass lets a
# Trash restore -- which reaches ``run_import_worker`` on a request thread
# through the check-then-act window ``library_busy`` documents against itself --
# start a second import while the first is still writing. The import job slot
# refuses any LATER request (``on_finish`` runs after this function returns), so
# that window is the only way in, and in it the restore is already HOLDING the
# swap lock, which therefore serialises nothing. This lock is the only thing
# left.
#
# Without it two overlapping calls
# interleave: the second snapshots the first's FORCED values and its finally
# writes them in as the user's, permanently rewriting the live global -- and
# beets re-reads that global late (``ImportTask.finalize`` -> ``cleanup`` at
# ``importer/tasks.py:307-311``), so an explicit MOVE can reach finalize
# reading another run's copy+delete.
_CONFIG_FORCE_LOCK = threading.Lock()

#: A contended acquire REFUSES rather than waits, because an attended import
#: holds this for the length of a human review: ``run()`` does not return until
#: the browser answers, and ``park`` ends in an untimed ``slot.reply.get()``
#: (:meth:`ImportBridge.park`). The other caller is the Trash restore, which
#: arrives on a request thread while holding the swap lock
#: (``api/trash.py`` -> ``trash_manage._restore_by_import``), so a blocking
#: acquire there would 409 every library-mutating route for the length of
#: someone's review, with nothing naming the cause. The window it needs is the
#: check-then-act one ``library_busy`` documents against itself.
#:
#: This is also what makes a plain ``Lock`` (not ``RLock``) defensible: the
#: comment used to say a nested acquire "should deadlock loudly", but a daemon
#: worker blocked in ``acquire()`` is silent -- no traceback, no log, the job
#: just never finishes. Only a timeout is loud.
_CONFIG_FORCE_TIMEOUT_S = 5.0


@contextmanager
def _config_force_lock() -> Iterator[None]:
    """Hold :data:`_CONFIG_FORCE_LOCK`, or refuse."""
    if not _CONFIG_FORCE_LOCK.acquire(timeout=_CONFIG_FORCE_TIMEOUT_S):
        raise ImportConfigBusyError("another import is in progress; try again when it finishes")
    try:
        yield
    finally:
        _CONFIG_FORCE_LOCK.release()


def run_import_worker(
    session: WebImportSession,
    *,
    move: bool | None = None,
    in_place: bool = False,
    sweep: bool = False,
    incremental: Literal[False] | None = None,
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
    IS the "ask"), ``import.autotag: yes`` and ``import.singletons: no`` — the
    last one forced for EVERY MusicDrop-driven import (review included), not just
    sweep/apply branches: a ``singletons: yes`` user config would route every
    DEFAULT review album into ``choose_item``'s SKIP funnel, importing NOTHING
    while recording import history. A Replace moves the old copy to the
    reversible Trash inside the duplicate hook, before beets places anything;
    after run() returns, this moves the one shape that hook never sees — a
    banked replace whose library copy was renamed since banking.

    ``autotag`` is forced (snapshot/restore, like the flags below) because beets
    swaps the ``lookup_candidates`` + ``user_query`` stages for ``import_asis``
    when it is off, and ``user_query`` is the ONLY stage that calls
    ``choose_match``. That hook is where every album outcome, bank row and
    landed-album-id follow-up originates, so a user config of ``autotag: no``
    (settable from MusicDrop's own config editor) makes beets import the files
    for real while the app records nothing: an empty review feed, a sweep that
    banks nothing yet history-marks every folder done, and a Trash restore that
    reports ``could_not_restore`` after it has already emptied the folder. Same
    silent-loss class as the ``singletons`` forcing, but total.

    ``move`` scopes the file operation to this one run: ``True`` forces a move
    (``copy=False``), ``False`` forces a copy (``move=False``). Because
    ``config["import"]`` is a process-global confuse singleton, the prior
    move/copy values are snapshotted and restored in a ``finally`` so an inbox
    move never leaks into the next manual import. ``None`` touches nothing — the
    manual-import default falls through to the user's beets config untouched.

    In-library sources are always forced to move-mode (explicit copy raises
    ``InLibraryCopyError``): beets' no-duplicate guarantee is DB-based and does
    not protect files the DB doesn't know yet.

    ``in_place`` is the third file operation the ``move`` flag cannot express:
    NEITHER move nor copy, so beets adds the files exactly where they already
    are. It exists for the Trash move-back restore, which has already put the
    folder back at its recorded origin and must not then have beets re-file it
    by path template — the whole point of recording the origin. Mutually
    exclusive with ``move`` (``ValueError``), and it deliberately skips the
    in-library move-forcing above: the source is in the library BY DESIGN here,
    and the duplication that forcing prevents cannot happen when nothing is
    copied. ``link``/``hardlink``/``reflink`` are snapshotted and forced off with
    it, because beets' stage picks the operation by falling through those in
    order (``importer/stages.py:278-291``) — with move and copy off, a user
    config of ``link: yes`` would otherwise symlink the album into the templated
    path. Nothing is deleted either way: ``ImportTask.cleanup`` removes originals
    only when ``copy and delete`` (``importer/tasks.py:326-333``).

    ``sweep`` scopes the banking sweep's beets flags to this one run (same
    snapshot/restore discipline as move/copy): ``incremental`` on — beets'
    taghistory then skips every folder a previous sweep finished OR banked
    (SKIPped tasks are recorded too: ``incremental_skip_later`` is forced
    ``no`` so a user's ``yes`` cannot stop that recording, which left every
    later sweep re-banking the same folders; the bank is the re-entry path for
    a banked folder, not a re-sweep). ``resume`` off EXPLICITLY — beets 2.13.1
    already excludes it when
    ``incremental`` is on (``importer/session.py:100-101``), and that exclusion
    is LIVE: ``want_resume`` at ``:140`` reads ``set_config``'s *parameter*,
    which is ``config["import"]``, not the module global. (An earlier note here
    claimed the opposite. It cannot have been true of any version —
    ``config_default.yaml`` has no top-level ``resume``, so a global read would
    raise ``NotFoundError`` on every import.) Setting it ourselves is therefore
    redundant today and kept anyway, so the behaviour does not depend on that
    coupling surviving a bump: without it an aborted sweep would re-enter the
    resume path on the next run. ``singletons`` off (forced unconditionally now — see the
    ``Forces`` paragraph above) — a ``singletons: yes`` user config
    would route every file through choose_item -> SKIP and history-mark it
    done WITHOUT a bank row (silent loss); album-shaped tasks are the only
    thing the bank can review.

    ``directive`` scopes a bank apply run's beets flags (same snapshot/restore
    discipline): ``incremental`` off EXPLICITLY — the sweep recorded every
    banked folder in taghistory (SKIPped tasks included) and the user's own
    config may say ``incremental: yes``, so without this beets' task factory
    skips the banked folder before any hook fires and the apply silently does
    nothing; ``resume`` off for the sweep's reasons; ``singletons`` is forced
    unconditionally (see above — astracks singletons arrive deliberately via
    the TRACKS re-pipeline, not the singletons flag); ``search_ids`` pinned to
    the chosen release id for an
    ``apply`` directive (consumed by beets' lookup_candidates stage ->
    ``tag_album(search_ids=...)``: candidates come ONLY from that id) and
    cleared otherwise so a stale user pin can never hijack the run. The
    ``search_ids`` snapshot/restore is unconditional so a directive pin never
    leaks into the next manual import. ``sweep`` and ``directive`` are never
    both set (the runner builds one or the other).

    ``incremental`` is the per-run override, and the four arms below are
    exclusive and ordered: a ``directive`` run is non-incremental; a ``sweep``
    is incremental; then this flag, whose ``False`` is ``beet import -I``
    (``ui/commands/import_/__init__.py:280-286``); then the resolved file
    operation — a run that HARDLINKS the files leaves the download in place, so
    history is what stops the same folder meeting the album a second time, and
    ``incremental_skip_later`` goes on with it so a SKIPped album is offered
    again. All four pin ``resume: False``, for two different reasons: the sweep
    and hardlink arms turn history ON, so beets clears ``resume`` itself and
    ours is the redundancy the sweep paragraph describes; the directive arm and
    a ``False`` override turn it OFF, where beets leaves ``resume`` alone and
    the pin is the only thing stopping a resume record from skipping the
    folder. Anything else (an inbox move, in_place, a
    ``link``/``reflink``/``copy`` config) leaves the history keys to the user.
    """
    # Bind the music dir for the WHOLE body: beets relativises an item's path
    # on write only when its ``music_dir`` ContextVar is set, and ``Library``
    # arms that var solely in the context that OPENED the library (the FastAPI
    # lifespan). Every caller reaches this on a worker thread, which inherits
    # nothing -- so without this bind beets stores absolute paths and the
    # library stops resolving the day the music dir moves. Both callers are
    # covered here: the job runner AND trash_manage.restore_album, which calls
    # this directly. Nesting is safe (beets binds via a ContextVar token).
    with session.lib.music_dir_context(), _config_force_lock():
        # Above the snapshots, with the other early exits: anything assigned
        # before a raise leaks into the process-global beets config, because the
        # finally that restores it never runs.
        if in_place and move is not None:
            raise ValueError("run_import_worker: in_place and move are mutually exclusive")
        # Snapshot BEFORE mutation, restore verbatim in the finally below.
        # The MUTATIONS all sit below the in-library guard: its raise is the
        # last early exit, and anything assigned above a raise leaks into the
        # process-global beets config (and the "Effective config" panel, which
        # flattens the live global) because the finally never runs.
        # In-library sources MUST move (same-dataset rename; samefile no-op):
        # with a fresh DB, copy-mode would duplicate any file whose computed
        # destination differs from its current path. Explicit copy is refused;
        # default/None and move pass through forced to move.
        sources = [os.fsdecode(p) for p in session.paths]
        # ``not in_place`` is a REDUNDANCY, not a load-bearing branch, and saying
        # so beats letting the next reader assume otherwise: the ``if in_place``
        # arm below takes precedence over the ``elif move is not None`` this
        # would feed, so the forcing has no effect on an in-place run either way.
        # It is kept so the two modes do not silently depend on that ordering,
        # and because probing every source's ancestry with ``samefile`` is work
        # an in-place run has no use for. No test can kill it; it is equivalent.
        if not in_place and any(
            is_in_library_source(session.lib.directory, src) for src in sources
        ):
            if move is False:
                raise InLibraryCopyError(
                    "Refusing to copy-import a folder inside the music library: "
                    "copy-mode would duplicate the files. Use move instead."
                )
            move = True
        # Every value read verbatim, so the restore below cannot coerce one:
        # ``resume`` is bool OR "ask", ``reflink`` bool OR "auto", plus a
        # ``search_ids`` list. ``.get(bool)`` VALIDATES rather than coerces
        # (``delete: 1`` raises ConfigTypeError), so snapshotting through it
        # turned a non-bool in the USER's config into OUR failure. beets reads
        # these with ``.get(bool)`` itself (``importer/tasks.py:307-311``), so a
        # non-bool is beets' error to raise, not a value to smuggle past it.
        # The set is a strict superset of the 8 keys beets' own ``set_config``
        # writes and never restores.
        orig_threaded = config["threaded"].get()
        orig_import = {
            key: config["import"][key].get()
            for key in (
                "duplicate_action",
                "autotag",
                "singletons",
                "incremental",
                "incremental_skip_later",
                "resume",
                "search_ids",
                "move",
                "copy",
                "link",
                "hardlink",
                "reflink",
                "delete",
            )
        }
        # Fail before the force, not inside beets' finalize. ``copy`` and
        # ``move`` are the only file flags a default import leaves to the user,
        # and beets reads both with ``.get(bool)`` at
        # ``importer/tasks.py:307-311`` -- which runs AFTER ``manipulate_files``
        # has already filed the album. Without this, ``copy: 1`` in a
        # hand-edited config files the album and then fails the job.
        for validated in ("copy", "move"):
            config["import"][validated].get(bool)
        forced: dict[str, object] = {
            "duplicate_action": "ask",
            "autotag": True,
            # Hoisted OUT of the sweep/directive branches: a DEFAULT review
            # import (no sweep, no directive) must be album-shaped too — under a
            # ``singletons: yes`` user config it previously skipped every album
            # while recording import history.
            "singletons": False,
            # MusicDrop never destroys a source, on ANY path. beets keeps
            # ``delete`` alive whenever ``copy`` survives
            # (``importer/session.py:136-138``) and then removes the originals
            # (``importer/tasks.py:326-333``), so a default import under a user
            # ``delete: yes`` is a move wearing the word "copy" and the download
            # is gone. Every UI path is a default import, so this is the arm
            # that matters; ``config_editor`` advises the user it is ignored.
            "delete": False,
        }
        # The five filing flags are pinned only when a caller NAMES an
        # operation: copy-vs-move is the user's filing preference, and a
        # default import leaves it to their config. An explicit request gets
        # all five, because a user ``hardlink: yes`` beats a lone ``copy: yes``.
        if in_place:
            forced.update(file_flags("in_place"))
        elif move is not None:
            forced.update(file_flags("move" if move else "copy"))
        # beets' import history, in one place because the arms are exclusive.
        # A folder is recorded when ``incremental`` is on and the album was not
        # SKIPped-with-``incremental_skip_later`` (``importer/tasks.py:301-305``),
        # and a recorded folder is skipped before any hook fires
        # (``importer/session.py:246-256``).
        if directive is not None:
            forced["incremental"] = False
            forced["resume"] = False
            forced["search_ids"] = [directive.search_id] if directive.search_id else []
        elif sweep:
            forced["incremental"] = True
            # A user's ``incremental_skip_later: yes`` stops a sweep recording
            # the folders it banked or SKIPped, so every later sweep re-banks
            # them. The bank is their re-entry path, not a re-sweep.
            forced["incremental_skip_later"] = False
            forced["resume"] = False
        elif incremental is not None:
            # The per-run override: ``False`` is ``beet import -I``, which is
            # how a kept folder gets re-imported after its album left the
            # library. ``resume`` off with it: beets leaves ``resume`` alone
            # when ``incremental`` is off, and the same task-factory check
            # skips a folder held by a resume record (``session.py:246-256``),
            # so "import it now" must clear that too.
            # ``incremental_skip_later`` stays the user's.
            forced["incremental"] = incremental
            forced["resume"] = False
        elif forced_file_operation(forced) == "hardlink":
            # A hardlink leaves the download in place, so adding the same
            # folder again would meet the album a second time. History is what
            # stops that, and skipping an album must not record it: the user
            # gets offered it again next time. ``resume`` off explicitly, for
            # the sweep arm's reason.
            #
            # ``hardlink`` alone, not every operation that keeps the download:
            # it is the spelling MusicDrop's keep-downloads setting writes into
            # beets' config (``decisions`` #53, BACKLOG "Download providers"). A
            # ``link``/``reflink``/``copy`` config keeps the download too and is
            # left to the user — they did not opt into this, and turning
            # history on would change what their setup already does.
            forced["incremental"] = True
            forced["incremental_skip_later"] = True
            forced["resume"] = False
        # ONE source per phase, not one per key: ``config[...][k] = v`` is
        # ``RootView.set``, which inserts a source that is never removed, so the
        # old per-key shape appended ~2N permanent overlays per import and made
        # every unoverlaid read and every "Effective config" flatten slower for
        # the life of the process. A single set is byte-identical in effect
        # (keys absent from the dict still fall through to the user's config)
        # and leaves no mixed state for a concurrent reader to observe.
        config.set({"threaded": False, "import": forced})
        try:
            # The only record of what beets did to the user's files, and the
            # only signal a user gets that an inbox import overrode their
            # ``hardlink: yes`` (that override is per-request, so no config
            # advisory can carry it). On ``uvicorn.error`` for the reason
            # ``main._boot_log`` documents: under the Dockerfile CMD an
            # app-namespace INFO record is dropped entirely (uvicorn's
            # LOGGING_CONFIG leaves the app logger at WARNING), so this line
            # emitted nothing in the shipped container. Read after the force
            # and before ``run()``, where it reports what beets will actually
            # resolve rather than what the user asked for.
            operator_logger.info("import file operation: %s", configured_file_operation())
            session.run()
            # The album is in the library the moment session.run() returns; a
            # failure moving a Replace-superseded copy to Trash must annotate,
            # not invalidate. Reporting a committed import as failed would
            # re-trigger duplicate detection against the just-imported album on
            # retry. Skipped when run() raises: the banked pass moves files on
            # the strength of an import that then did not finish.
            try:
                _trash_replaced_albums(session)
            except Exception:
                logger.exception("post-import Trash cleanup failed; the old copy stayed in place")
        finally:
            config.set({"threaded": orig_threaded, "import": orig_import})
            # One re-export point for the whole run. Both routes that drop item
            # rows -- the duplicate hook and the banked pass above -- record the
            # ids on the session, so the exports are rendered once, from the
            # store as it finally is. In the finally because the hook drops rows
            # during run(): a raise afterwards must not leave an export naming
            # files that moved. Swallows its own failures.
            _reexport_replaced_playlists(session)


def _trash_replaced_albums(session: WebImportSession) -> None:
    """Move every BANKED replace target to Trash (post-run, by id).

    The attended and directive duplicate hooks move their own copies BEFORE beets
    places anything (``_replace_duplicates_now``); this pass exists for the one
    shape that never reaches the hook — a banked replace whose library copy was
    renamed since banking, so beets' own ``find_duplicates`` misses it
    (``_seed_replace_from_directive``, vault decision 25). It therefore runs after
    ``session.run()``, where a bank apply that resolved nothing has already proved
    it: moving first would Trash the user's only copy while importing nothing.

    Running after placement is what makes the identity check below necessary.
    Under ``hardlink``/``link`` the incoming file and the library file are one
    file, so beets files the new album on the old album's paths — and moving the
    old album by id would move the new album's own files. The question is asked
    per ALBUM and by file identity (``st_dev``, ``st_ino``, through symlinks),
    not per row by stored string: an album that shares any file with an album
    this run landed has its rows dropped and every file left where it is.
    Identity because the same bytes carry more than one spelling — a symlinked
    music root, a ``link``-mode symlink onto the source. Pinned by
    ``test_a_banked_replace_sharing_a_file_drops_rows_and_moves_nothing`` and
    ``test_a_symlinked_library_root_is_still_the_same_file``.

    A path that cannot be stat-ed leaves its album alone, rows and files: unread
    is not unshared, and one EACCES blinds both sides of the comparison at once
    (``test_an_unreadable_file_leaves_the_banked_copy_in_place``).

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

    Finally, the `.m3u8` collateral: a replaced album's files are now in Trash and
    its rows are gone, so every playlist that held one of its tracks has an export
    naming a file that is not there. The item ids are read BEFORE the rows go and
    recorded on the session; ``run_import_worker`` renders the exports once, after
    this pass, for every route in the run.

    The store layout is re-checked here for the reason the request paths re-check
    it: this pair was resolved once, when the registry was handed the library at
    lifespan or after an Apply, and an import can run hours later. The pair moves
    files rather than deleting them, so a refused layout costs a replaced album's
    files relocated inside the music library with its DB rows dropped, not an
    ``rmtree``. WARNING and skip, like the orphan sweep's arm: the import itself
    has already committed, and leaving the old copy in the library is the
    recoverable side of the choice.
    """
    trash_dir = session._trash_dir
    origins_dir = session._trash_origins_dir
    if trash_dir is None or origins_dir is None or not session._replace_album_ids:
        return
    lib = session.lib
    if not _store_layout_ok(
        lib,
        trash_dir=trash_dir,
        origins_dir=origins_dir,
        refusal="post-import Trash cleanup skipped: the store layout is refused",
    ):
        return
    with lib.music_dir_context():
        landed, landed_unreadable = _landed_file_identities(lib, session._landed_album_ids)
        if landed_unreadable:
            logger.warning(
                "post-import Trash cleanup skipped: a file of the album this import just "
                "landed could not be read, so a shared file could not be ruled out"
            )
            return
        for album_id in session._replace_album_ids:
            album = lib.get_album(album_id)
            if album is None:
                continue  # already gone — nothing to trash
            item_ids = [_require_id(item.id) for item in album.items()]
            own, unreadable = _file_identities(lib, album)
            if unreadable:
                logger.warning(
                    "post-import Trash cleanup: library album %s has a file that could not be "
                    "read, so it was left in place",
                    album_id,
                )
                continue
            if own & landed:
                # Its files are the ones beets just filed. Dropping the rows is
                # what keeps them from showing twice; moving them would move the
                # new album out from under itself.
                logger.warning(
                    "post-import Trash cleanup: library album %s shares a file with an album "
                    "this import just landed; its rows were dropped and the files left alone",
                    album_id,
                )
                album.remove(delete=False)
            else:
                trash_album(lib, album, trash_dir=trash_dir, origins_dir=origins_dir)
            session._dropped_item_ids.update(item_ids)


def _file_identities(lib: Any, album: Any) -> tuple[set[tuple[int, int]], bool]:
    """The album's files as ``(st_dev, st_ino)``, and whether one could not be read.

    ``os.stat``, which follows symlinks: the question is whether two albums reach
    the same bytes, and under ``link`` mode the file beets filed is a symlink onto
    the file it came from. A path that is simply absent contributes nothing and is
    not "could not be read" — only an ``OSError`` that is not a missing path is.
    """
    identities: set[tuple[int, int]] = set()
    unreadable = False
    for raw in _album_file_paths(album):
        try:
            stat_result = os.stat(_abs_path(lib, raw))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            unreadable = True
            continue
        identities.add((stat_result.st_dev, stat_result.st_ino))
    return identities, unreadable


def _landed_file_identities(
    lib: Any, landed_album_ids: set[int]
) -> tuple[set[tuple[int, int]], bool]:
    """The same, over every album this run imported."""
    identities: set[tuple[int, int]] = set()
    unreadable = False
    for album_id in landed_album_ids:
        album = lib.get_album(album_id)
        if album is None:
            continue
        found, missed = _file_identities(lib, album)
        identities |= found
        unreadable = unreadable or missed
    return identities, unreadable


def _reexport_replaced_playlists(session: WebImportSession) -> None:
    """Rewrite the `.m3u8` of every playlist holding a replaced album's track.

    One point for the whole run. Both Replace routes drop rows — the duplicate
    hook before beets places anything, the banked pass after — and each records
    the item ids it dropped on the session, so the exports are rendered once from
    the store as it finally is rather than twice from two halves of it.

    Best-effort and swallowing: ``run_import_worker`` already treats a post-run
    Trash failure as an annotation rather than an invalidated import (reporting a
    committed import as failed would re-trigger duplicate detection on retry), and
    this collateral has even less claim to fail the run. It is also called from a
    ``finally``, where a raise would replace whatever the run was already raising.

    The count is LOGGED rather than returned: the only channel out of the worker
    is ``on_finish()``, which takes no arguments, and carrying the number would
    widen the ImportRunner protocol, the registry and ``ImportJobState`` for a
    figure the import UI has nowhere to show.
    """
    playlists_dir = session._playlists_dir
    dropped_item_ids = session._dropped_item_ids
    if playlists_dir is None or not dropped_item_ids:
        return
    try:
        count = reexport_playlists_containing_sync(dropped_item_ids, session.lib, playlists_dir)
    except Exception:
        logger.exception("post-import .m3u8 re-export failed after a Replace")
    else:
        operator_logger.info("Replace collateral: re-exported %d playlist .m3u8 file(s)", count)
