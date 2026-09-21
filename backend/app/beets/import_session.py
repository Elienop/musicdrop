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
import stat
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, Literal, NoReturn, TypeVar, cast

from beets import config
from beets.autotag.match import Recommendation as BeetsRec
from beets.importer.actions import Action
from beets.importer.actions import DuplicateAction as BeetsDuplicateAction

# ``ImportAbortError as ImportAbortError`` is an explicit re-export (mypy --strict
# has no implicit ones): the fake import runner models beets' run() catching it,
# and must not import beets itself (CLAUDE.md rule 3).
from beets.importer.session import ImportAbortError as ImportAbortError
from beets.importer.session import ImportSession

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

# Re-exported: the predicate moved to ``app.fsutil`` on 2026-09-19 so
# ``app.beets.library`` could ask it too (that arrow cannot point back here).
# Its callers — the runner's copy-mode guard, ``run_import_worker`` — keep
# naming it from this module.
from app.fsutil import is_in_library_source as is_in_library_source
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

#: Operator-facing records go to ``uvicorn.error``: under the Dockerfile CMD
#: uvicorn's LOGGING_CONFIG leaves app-namespace loggers at WARNING, so an app
#: INFO record never reaches ``docker logs`` (same trap as ``main._boot_log``).
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


_ReplyT = TypeVar("_ReplyT")


class _StopRequested:
    """What a blocked park is handed when a stop is requested — not a decision."""


#: The one instance ``request_stop`` puts into every park it releases. ``park``
#: and ``park_duplicate`` raise ``ImportAbortError`` on seeing it rather than
#: handing it to beets as an answer.
_STOP_REQUESTED = _StopRequested()


@dataclass
class _ReplySlot(Generic[_ReplyT]):
    """One park's rendezvous: the worker's reply queue plus whether it was answered.

    ``answered`` is set by the CONSUMER, in the same critical section as the put,
    and read by ``has_unanswered_park``. Reading the queue itself instead would
    misreport the gap between ``reply.get()`` returning and the worker deleting
    the slot: the queue is empty again there, yet the answer has landed and
    nobody is waiting on a person. A slot is released by identity when its park
    returns, so a re-park at the same index starts out unanswered again.

    A stop is the second thing that can land here (``_STOP_REQUESTED``), which is
    why the queue carries the sentinel as well as the decision type.
    """

    reply: queue.Queue[_ReplyT | _StopRequested]
    answered: bool = False


def _release_on_stop(slot: _ReplySlot[_ReplyT]) -> None:
    """Wake the park blocked on ``slot`` so it can abort (caller holds the lock).

    A slot that already holds a decision is left alone: that answer was accepted
    first and the worker acts on it, then stops at its next abort point (a hook,
    or the re-park a ``search``/``rescan`` answer leads back to).
    """
    if slot.answered:
        return
    slot.reply.put_nowait(_STOP_REQUESTED)
    slot.answered = True


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
        # Stop flag: set by the registry's request_stop (consumer side), read by
        # the session at the top of every decision hook and by the two parks
        # (worker side). It lives on the bridge because the bridge is the one
        # object both sides already share - the registry never holds the session.
        self._stop = threading.Event()
        # Set by abort_now() at every site that raises beets' abort, and by
        # nothing else. Separate from _stop because the two answer different
        # questions: _stop says a stop was ACCEPTED, this says the run was
        # actually cut short. A stop accepted after the last abort point (the
        # final album's placement, the post-run Trash pass) leaves this clear,
        # and the verdict readers that would otherwise call a fully-landed run
        # failed read this instead (ImportJobRegistry.job_aborted).
        self._aborted = threading.Event()
        # Folders beets' task factory skipped as already imported (incremental
        # history). Monotone; every job state reports it, sweep or not.
        self._known_skips = 0

    # ----- worker side -----

    def park(self, parked: ParkedAlbum, art_source: str | None = None) -> ImportChoice:
        """Push a parked album and block until a choice arrives for it.

        Raises beets' ``ImportAbortError`` instead when a stop is in force. The
        registration AND the queueing share ``request_stop``'s critical section,
        so the two orderings are the only two: the stop is already set and this
        park neither registers nor reaches the consumer, or both steps ran and
        the stop releases the registered slot with ``_STOP_REQUESTED``. Queueing
        outside the lock left a third: a stop landing in the gap released a slot
        whose album the worker then handed to the consumer anyway.
        """
        slot: _ReplySlot[ImportChoice] = _ReplySlot(queue.Queue(maxsize=1))
        with self._lock:
            if self._stop.is_set():
                self.abort_now()
            self._replies[parked.album_index] = slot
            if art_source is not None:
                self._art_source[parked.album_index] = art_source
            self._pending += 1
            # Unbounded queue with its own mutex below this lock, so the put
            # cannot block and adds no ordering (see _answer's docstring).
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
        if isinstance(choice, _StopRequested):
            self.abort_now()
        return choice

    def park_duplicate(
        self, prompt: DuplicatePrompt, art_source: str | None = None
    ) -> DuplicateDecision:
        """Push a duplicate prompt and block until a decision arrives for it.

        Raises beets' ``ImportAbortError`` under a stop, both orderings, exactly
        as :meth:`park` does — the duplicate question is a park like any other.
        """
        slot: _ReplySlot[DuplicateDecision] = _ReplySlot(queue.Queue(maxsize=1))
        with self._lock:
            if self._stop.is_set():
                self.abort_now()
            self._dup_replies[prompt.album_index] = slot
            if art_source is not None:
                self._art_source[prompt.album_index] = art_source
            self._pending += 1
            self._dup_out.put(prompt)  # inside the lock, for park()'s reason
        decision = slot.reply.get()  # blocks the worker thread
        with self._lock:
            # By identity, for the reason spelled out in park().
            if self._dup_replies.get(prompt.album_index) is slot:
                del self._dup_replies[prompt.album_index]
            self._pending -= 1
        if isinstance(decision, _StopRequested):
            self.abort_now()
        return decision

    def publish_duplicate(self, prompt: DuplicatePrompt) -> None:
        """Push a duplicate prompt that NOBODY will answer (non-blocking).

        No reply slot and no ``_pending`` increment, so ``has_unanswered_park``
        stays False; same queue as :meth:`park_duplicate`, so the feed row gets
        the prompt either way. Used by the stale-consent refusal, whose remedy IS
        the refreshed prompt (:meth:`WebImportSession._consent_covers`): the job
        row then shows ``needs_dup_resolution`` with a prompt that 404s when
        answered, and the bank row is where it is decided again.
        """
        self._dup_out.put(prompt)

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

    def _refuse_under_stop(self, album_index: int) -> None:
        """Raise once a stop is in force (caller holds the lock).

        A released park stays REGISTERED with an empty queue until the woken
        worker retakes the lock to delete it, so without this a decision landing
        in that gap is accepted: the registry marks the row ``decided`` for a
        worker that is already unwinding. ``KeyError`` is what the late decision
        gets once the slot is gone, and the API maps it to the same 404.
        """
        if self._stop.is_set():
            raise KeyError(f"album {album_index}: the run is stopping")

    def push_choice(self, album_index: int, choice: ImportChoice) -> None:
        """Deliver a decision to the worker blocked on ``album_index``."""
        with self._lock:
            self._refuse_under_stop(album_index)
            slot = self._replies.get(album_index)
            if slot is None:
                raise KeyError(f"no album parked at index {album_index}")
            _answer(slot, choice, f"album {album_index} already has a pending choice")

    def push_duplicate_decision(self, album_index: int, decision: DuplicateDecision) -> None:
        """Deliver a duplicate decision to the worker blocked on ``album_index``."""
        with self._lock:
            self._refuse_under_stop(album_index)
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
          not proof its worker is still waiting: a choice pushed before that pop
          is delivered to a live slot, and the pop then describes a worker that
          has already run on
          (``test_awaiting_decision_clears_when_a_choice_beats_the_drain_to_the_park``).
        """
        with self._lock:
            return any(not slot.answered for slot in self._replies.values()) or any(
                not slot.answered for slot in self._dup_replies.values()
            )

    def pending_count(self) -> int:
        with self._lock:
            return self._pending

    def request_stop(self) -> None:
        """Stop the run: arm every abort point, then free a worker already parked.

        Both steps hold the lock a park registers under, so a park cannot slip
        between them and block forever. The event is what the decision hooks and
        the two parks read; the release is for the worker that is ALREADY
        blocked, which no flag on its own reaches.
        """
        with self._lock:
            self._stop.set()
            for slot in self._replies.values():
                _release_on_stop(slot)
            for dup_slot in self._dup_replies.values():
                _release_on_stop(dup_slot)

    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def abort_now(self) -> NoReturn:
        """Record that the run is being cut short, then raise beets' abort.

        THE one raise site for ``ImportAbortError``, so the recording cannot be
        forgotten at a new one. Three callers: the session's ``_check_stop``
        (a worker between questions) and the two parks, each on both orderings
        (the stop already set at registration, or the release sentinel).
        """
        self._aborted.set()
        raise ImportAbortError

    def abort_raised(self) -> bool:
        """Whether ``abort_now`` fired — i.e. something was actually cut short."""
        return self._aborted.is_set()

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
#: A Replace whose old copy is links to nowhere. beets' ``unique_path`` asks
#: ``os.path.exists``, so placement writes THROUGH the link, outside the library.
_REPLACE_BROKEN_LINKS = "The old copy's files are broken links. Nothing was imported."
#: A banked Replace whose collision no longer matches what the prompt named:
#: stale consent, so the user decides again (``_consent_covers``).
_REPLACE_STALE_CONSENT = "The library changed since this was set aside. Decide again."


def _replace_partial_note(completed: int, total: int) -> str:
    """Why a Replace imported nothing — naming how many old copies did move.

    A failure on the second of two duplicates leaves the first in Trash, so
    "Replace failed" alone would hide a folder that has left the library.

    ``completed`` counts the calls that PUT a container in Trash — not a ghost
    row-drop, and not an album whose only present row was a file this import is
    reading, because the mover rmdirs the container it made
    (``test_a_replace_whose_only_move_put_nothing_in_trash_says_so``).
    ``trash_album`` can raise after the files moved, so the zero case says the
    step failed rather than claiming nothing moved.
    """
    if completed:
        return (
            f"Replace moved {completed} of {total} old copies to Trash, then failed."
            " Nothing was imported."
        )
    return "Replace failed while moving the old copy to Trash. Nothing was imported."


def _store_layout_ok(lib: Any, *, trash_dir: Path, origins_dir: Path, refusal: str) -> bool:
    """Is the music / beets / Trash / origins layout still one we will move into?

    Re-checked per Replace: the pair was resolved once, at lifespan or after an
    Apply, and an import can run hours later. ``BEETSDIR``, not
    ``settings.beets_dir``, whose default is the RELATIVE "data/beets" and would
    resolve against a worker thread's CWD. The two routes' consequences differ,
    so the caller supplies the whole ``refusal`` sentence.
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
        # ``"%s", refusal``: the sentence is the caller's, so a ``%`` in it must
        # not reach logging's formatter.
        logger.warning("%s", refusal, exc_info=True)
        return False
    return True


#: One directory entry: the file's ``(st_dev, st_ino)`` and its holding
#: directory's. See :func:`_file_identities` for why both halves are needed.
_EntryKey = tuple[int, int, int, int]


def _album_file_paths(album: Any) -> list[Any]:
    """Every path the album names: its tracks' files, plus its cover art.

    For the IDENTITY question only (:func:`_file_identities`), where the art
    counts because moving an album takes it along. Presence asks the items alone
    (:func:`_album_file_state`).
    """
    paths = [item.path for item in album.items()]
    paths.append(getattr(album, "artpath", None))
    return [path for path in paths if path]


def _task_source_paths(task: Any) -> set[Any]:
    """Every file the CURRENT task is reading, as beets' own re-import test reads it.

    Stored-path form, byte-compared — the expression beets uses to exclude a
    re-import from its own duplicates (``importer/tasks.py:388``) and to find the
    rows it deletes at ``task.add``. Not ``task.paths``, which holds the toppath
    DIRECTORIES; what a MOVER may touch asks :class:`_SourceFiles`.
    """
    return {item.path for item in (task.items or []) if item is not None and item.path}


def _entry_key(path: str, *, follow_leaf: bool) -> tuple[_EntryKey | None, bool]:
    """One path as a DIRECTORY ENTRY: ``(key, unreadable)``. Two questions, one shape.

    The key is the leaf's ``(st_dev, st_ino)`` plus its holding directory's. The
    holder needs no ``realpath`` — ``os.stat`` follows a symlinked folder itself,
    so a second spelling of the download dir already keys equal.

    ``follow_leaf`` is the difference between the callers, and on a ``link``-mode
    library entry they MUST answer differently: "does this reach the same bytes?"
    (:func:`_file_identities`, True) vs "is this the entry a move would take?"
    (:class:`_SourceFiles`, False). Only that row differs — the source file and
    an aliased download key equal to the source either way, a hardlink library
    copy equal to neither — measured through the production helpers by
    ``test_the_four_ownership_shapes``. One shared answer for both lost a refiled
    ``link`` album's rows
    (``test_a_link_mode_album_refiled_elsewhere_still_reaches_trash``).

    A missing path answers ``(None, False)``; only an ``OSError`` that is neither
    a missing path nor a non-directory component answers ``(None, True)``.
    """
    leaf = os.path.realpath(path) if follow_leaf else path
    try:
        entry = os.stat(leaf) if follow_leaf else os.lstat(leaf)
        holder = os.stat(os.path.dirname(leaf))
    except (FileNotFoundError, NotADirectoryError):
        return None, False
    except OSError:
        return None, True
    return (entry.st_dev, entry.st_ino, holder.st_dev, holder.st_ino), False


class _SourceFiles:
    """Every file this RUN is reading — by stored path AND by directory entry.

    Ownership asked twice, because one file has more than one spelling: the byte
    set is beets' own (:func:`_task_source_paths`), the entry set is
    :func:`_entry_key` with ``follow_leaf=False``. With bytes alone a row
    rewritten to a symlink ALIAS of the download folder was not recognised as the
    import's own and the mover took the source file to Trash
    (``test_a_replace_leaves_an_aliased_download_row_alone``,
    ``test_a_banked_replace_leaves_an_aliased_download_row_alone``).

    A source file whose key cannot be built keeps its BYTES and adds no entry, so
    an alias of it is not recognised — safe either way, since a gone file has
    nothing to protect and an unreadable one makes :func:`_album_file_state`
    answer ``unknown``, which refuses before disposal
    (``test_a_source_file_that_cannot_be_keyed_still_matches_its_own_bytes``).
    """

    def __init__(self) -> None:
        self.paths: set[Any] = set()
        self.entries: set[_EntryKey] = set()

    def note(self, task: Any) -> None:
        """Record one task's source files. Idempotent; called per task.

        No library needed: a task's item paths are absolute, unlike the LIBRARY
        rows :meth:`covers` is asked about, which can be stored relative to
        ``directory:``.
        """
        for raw in _task_source_paths(task):
            self.paths.add(raw)
            key, unreadable = _entry_key(os.fsdecode(raw), follow_leaf=False)
            if key is not None:
                self.entries.add(key)
            elif unreadable:
                logger.warning(
                    "import: source file %s could not be identified; only its exact path is "
                    "recognised as this import's own",
                    os.fsdecode(raw),
                )

    def covers(self, lib: Any, raw: Any) -> bool:
        """Is this stored row path one of the files the run is reading?"""
        if raw in self.paths:
            return True
        key, _unreadable = _entry_key(_abs_path(lib, raw), follow_leaf=False)
        return key is not None and key in self.entries


def _drop_rows_the_run_is_reading(lib: Any, album: Any, source: _SourceFiles) -> list[str]:
    """Drop the album's rows naming a file THIS run is reading; return the paths.

    Ownership, not location: this moves forward what beets does at ``task.add``,
    in front of the mover. A half-finished import leaves a library row naming the
    download folder, and moving the album wholesale took the import's OWN source
    file to Trash (``test_a_replace_leaves_a_half_finished_imports_download_alone``).
    A row whose file merely lives OUTSIDE the music folder goes to Trash with the
    album (``test_an_album_outside_the_music_folder_still_reaches_trash``).

    The RUN, not this task: beets only deletes the rows of tasks that reach
    ``task.add``, so a skipped sibling leaves its own source rows for this. The
    cost: a library album whose files ARE a skipped task's sources is
    de-registered although beets never replaces it — the files stay put, and a
    re-import or disk scan re-registers them.

    ``with_album=False``: the caller disposes of the album itself.
    """
    dropped: list[str] = []
    for item in album.items():
        if item.path and source.covers(lib, item.path):
            dropped.append(_abs_path(lib, item.path))
            item.remove(delete=False, with_album=False)
    return dropped


def _album_label(lib: Any, album: Any) -> str:
    """``artist - album (folder)``, for a log line about an album being disposed of.

    Read BEFORE the rows go: SQLite frees the rowid and the next insert takes it
    (measured — the replacement album came back as id 1), so a warning naming
    only the id leads nowhere.
    """
    paths = [item.path for item in album.items() if item.path]
    folder = os.path.dirname(_abs_path(lib, paths[0])) if paths else ""
    return f"{album.albumartist} - {album.album} ({folder or 'no folder'})"


class _ReplaceStateChanged(RuntimeError):
    """A duplicate answered something new at its own disposal — so nothing is assumed.

    Reachable: the state is re-read per album, so a folder that turns unreadable
    after the up-front classification answers ``unknown`` there
    (``test_a_duplicate_that_turns_unreadable_mid_pass_stops_the_replace``).
    """


class _FileState(StrEnum):
    """What a duplicate album's files answered when asked where they are."""

    present = "present"
    absent = "absent"
    broken_link = "broken_link"
    unknown = "unknown"


def _album_file_state(lib: Any, album: Any) -> _FileState:
    """Where are this album's TRACK files: here, gone, links to nowhere, or unanswerable?

    Three measured facts, one per state that is not ``present``:

    * ``os.path.exists`` is False for EACCES and EIO as much as for ENOENT, so a
      two-valued test reads an unreadable folder as a ghost and drops the rows of
      an album whose files are all there (measured at 0o444 and 0o000 —
      ``test_an_unreadable_duplicate_refuses_the_whole_replace``).
    * a dangling symlink is neither present nor gone: beets' ``unique_path`` also
      asks ``os.path.exists``, so placement writes THROUGH the link, outside the
      library (``test_a_duplicate_of_dangling_symlinks_refuses_the_replace``).
    * the ITEMS only, ``album.artpath`` unasked (owner ruling): an album with no
      track file left is a ghost whatever its cover says, and the cover is left
      alone
      (``test_a_ghost_whose_cover_survived_is_replaced_and_its_cover_left_alone``).

    Every item row votes, wherever its file lives.
    """
    state = _FileState.absent
    broken = False
    for raw in (item.path for item in album.items() if item.path):
        path = _abs_path(lib, raw)
        try:
            entry = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            return _FileState.unknown
        if stat.S_ISLNK(entry.st_mode):
            try:
                os.stat(path)  # the TARGET: a link to nowhere is neither here nor gone
            except (FileNotFoundError, NotADirectoryError):
                broken = True
                continue
            except OSError:
                return _FileState.unknown
        state = _FileState.present
    return _FileState.broken_link if broken else state


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
        # Its sibling origin store, wired as a PAIR with ``trash_dir``: both
        # Replace paths skip unless BOTH are set, since a copy trashed without a
        # record of where it came from cannot be restored.
        self._trash_origins_dir = trash_origins_dir
        # Banked replace targets the duplicate hook never saw, trashed AFTER
        # run() by _trash_replaced_albums. The hook's own Replace moves its
        # duplicates before placement and records nothing here.
        self._replace_album_ids: set[int] = set()
        # Library album ids the duplicate hook already disposed of. Read by the
        # banked seed so it does not re-enforce a copy that has left.
        self._hook_replaced_album_ids: set[int] = set()
        # Latched by the first refused Replace, so the post-run pass does not
        # trash targets whose replacement was never imported.
        self._replace_was_refused = False
        # Every file this run is READING (:class:`_SourceFiles`), filled at
        # choose_match / choose_item and read by both Replace routes.
        self._source_files = _SourceFiles()
        # Item ids this run dropped from the library, from BOTH replace routes;
        # run_import_worker re-exports the affected playlists once, after the run.
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

    def _check_stop(self) -> None:
        """Abort cleanly when a stop was requested (the sweep's Pause, and Stop).

        Raises beets' own ``ImportAbortError`` through ``bridge.abort_now``,
        which records the cut-short: beets' ``run()`` catches it and stops the
        pipeline at this album boundary. The aborted task was never chosen, so
        it is not finalized into incremental history and the next run picks
        it up again; any pending album-id follow-up still flushes because our
        ``run()`` override flushes after beets swallows the abort.

        This is the arm that covers a worker BETWEEN questions (scanning, a
        lookup in flight, an apply running). A worker parked on a question, or
        about to park, is covered by the bridge's two parks instead.
        """
        if self.bridge.stop_requested():
            self.bridge.abort_now()

    def _mid_astracks_expansion(self) -> bool:
        """True while an "as tracks" album is being re-pipelined into singletons.

        Either arm that reaches ``choose_item``: the attended choice
        (``_astracks_in_flight``, armed by choose_match) or a banked astracks
        directive. Singletons have no other source here - the import worker
        forces ``import.singletons`` off on every run.
        """
        if self._astracks_in_flight:
            return True
        return self._directive is not None and self._directive.action == "astracks"

    def already_imported(self, toppath: Any, paths: Any) -> bool:
        """Count folders beets skips as already imported.

        beets' task factory consults this per prospective album folder BEFORE
        any session hook fires, so history-skipped folders never reach the
        outcome stream - this override is the only seam that sees them. The
        count rides the bridge and every job state reports it.

        beets answers True for a RESUMED folder too, not only a history one
        (``importer/session.py:246-256``). Every arm ``run_import_worker`` forces
        pins ``resume: False``; a run it leaves alone can count a resume skip.
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
        #
        # A stop does NOT land here mid-expansion. beets re-pipelines each file
        # as its own SingletonImportTask and runs it through the remaining
        # stages on its own (stages.py:180-193, :368-385), so each track is
        # placed and history-recorded separately: aborting between track 3 and
        # track 4 leaves one album half in the library and half in the download
        # folder, in move mode with the landed half already gone from the
        # source. The window closes at the next choose_match, which checks the
        # stop before it clears the flag.
        if not self._mid_astracks_expansion():
            self._check_stop()
        # A singleton task carries its one file in ``items`` too.
        self._source_files.note(task)
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
        disposed of the old copy (:meth:`_replace_duplicates_now`), else SKIP.
        """
        if task.is_album:
            # Album tasks only: a singleton reaching this hook is one track of
            # an "as tracks" expansion, and aborting between two of them splits
            # the album across two locations (see choose_item).
            self._check_stop()
        else:
            # A singleton "as tracks" import whose track duplicates a library item.
            # beets 2.12 shares this hook for singletons, but passes Items — not
            # Albums (SingletonImportTask.find_duplicates, tasks.py) — and the
            # prompt / replace machinery below is album-shaped. SKIP the duplicate
            # track (keeps the library copy): the safe, non-destructive resolution,
            # matching beets' own singleton default. Items must not reach
            # to_existing_album (``items[0].path`` on a Model.items() field tuple
            # crashes the job) nor the replace arm (it would Trash the album
            # sharing that id).
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
            return self._beets_dup_action(
                dup_action, found_duplicates, task=task, index=index, prompt=prompt
            )
        if self.unattended:
            if self.sweep:
                self._bank_duplicate_row(
                    task, index=index, prompt=prompt, has_current_art=incoming.has_current_art
                )
            # Unattended: the outcome above records the set-aside; SKIP the new
            # album (keeps the library copy) without parking + blocking.
            return BeetsDuplicateAction.SKIP
        decision = self.bridge.park_duplicate(prompt, art_source=art_source)
        return self._beets_dup_action(
            decision.action, found_duplicates, task=task, index=index, prompt=prompt
        )

    def _bank_duplicate_row(
        self,
        task: ImportTask,
        *,
        index: int,
        prompt: DuplicatePrompt,
        has_current_art: bool,
    ) -> None:
        """Bank the collision a sweep has nobody to park it on.

        The prompt the attended flow would park: the user resolves
        skip/keep/replace/merge later from the Review page, and the release this
        album MATCHED is banked with it so that decision replays the match
        instead of re-running the lookup.
        """
        rec = task.rec if task.rec is not None else BeetsRec.none
        recommendation = _REC_MAP.get(rec, Recommendation.none)
        self._bank_row(
            task,
            reason="needs_dup_resolution",
            recommendation=recommendation,
            confidence=_confidence(task.match.distance) if task.match is not None else 0.0,
            duplicate=prompt,
            parked=self._matched_release_payload(
                task,
                index=index,
                recommendation=recommendation,
                has_current_art=has_current_art,
            ),
        )

    def _beets_dup_action(
        self,
        action: DuplicateAction,
        found_duplicates: Any,
        *,
        task: ImportTask,
        index: int,
        prompt: DuplicatePrompt,
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
            return self._replace_duplicates_now(
                found_duplicates, task=task, index=index, prompt=prompt
            )
        # keep_both: import alongside the existing copy.
        return BeetsDuplicateAction.KEEP

    def _replace_duplicates_now(
        self, found_duplicates: Any, *, task: ImportTask, index: int, prompt: DuplicatePrompt
    ) -> BeetsDuplicateAction:
        """Dispose of the albums the user was shown, then answer beets ``KEEP``.

        **Timing.** beets asks this hook from ``_resolve_duplicates``
        (``importer/stages.py``) and places files later in ``manipulate_files``,
        so disposal happens while the new album is still only in the download
        folder. Under ``hardlink``/``link`` the incoming and library files are one
        file, so beets reuses the old album's paths and a Trash move made
        afterwards would move the NEW album's files
        (``test_replacing_a_duplicate_leaves_an_album_whose_files_exist``).

        **Not beets' own REMOVE.** ``ImportTask.remove_duplicates`` RE-RUNS
        ``find_duplicates``, and ``task.add`` rewrites ``albumartist`` for an ASIS
        task in between, so an as-is compilation shown as duplicating ``('A',
        'Comp X')`` had ``('Various Artists', 'Comp X')`` hard-deleted outside
        Trash
        (``test_an_as_is_compilation_replace_leaves_the_album_the_user_never_saw``).
        Answering KEEP means that method never runs.

        **Order.** The gates — root check, ``unknown``, ``broken_link`` — run over
        the WHOLE live bucket and ahead of consent (:meth:`_consent_covers`),
        because they are about where beets is about to WRITE: scoping them by
        consent left a bucket member unclassified and the new album's audio was
        written through its dangling links, outside the music library
        (``test_a_bucket_member_the_prompt_did_not_name_is_still_classified``).
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
        if any(state is _FileState.broken_link for _, state in states):
            logger.warning(
                "import replace: a library copy's files are links to nowhere; nothing was imported"
            )
            return self._replace_refused(task, index, _REPLACE_BROKEN_LINKS)
        if not self._consent_covers(found_duplicates):
            # The remedy is the prompt: publish the collision as THIS apply saw
            # it, so the bank row carries what the user is now asked about.
            # Without it, "decide again" re-queues the stored prompt and refuses
            # identically forever.
            self.bridge.publish_duplicate(prompt)
            return self._replace_refused(task, index, _REPLACE_STALE_CONSENT)
        note = self._dispose_duplicates_now(states, task=task)
        if note is None:
            return BeetsDuplicateAction.KEEP
        return self._replace_refused(task, index, note)

    def _consent_covers(self, found_duplicates: Any) -> bool:
        """Does the banked prompt still account for EVERY album in the live bucket?

        The attended route is bounded by construction (prompt and disposal read
        the ONE list this hook was handed). A directive is not: built from a bank
        row swept hours earlier and read against the bucket as it is NOW, it
        disposed of an album that appeared on no prompt.

        All or nothing, not an intersection: filtering the bucket by consent puts
        that filter above the gates, and where it did dispose it left an
        unasked-for second copy (``unique_path`` saw the survivor, so the new
        album landed at ``01 Airbag 1.1.flac``). Stale consent refuses the whole
        Replace and the bank row fails retryable
        (``test_a_bank_replace_refuses_when_the_bucket_gained_an_album``); the
        refusing apply publishes the live collision, because deciding again on the
        SAME stored prompt would rebuild the same consent set
        (``test_a_stale_consent_refusal_can_be_decided_again``).

        Identity by ``duplicate_albums_still_present``. Its albumartist+album pair
        adds no discrimination here, so the check is only as strong as a stored
        AND live release id; with neither, a same-named album on a reused rowid
        passes. An empty ``replace_existing`` names nothing and stays unbounded —
        reachable from the up-front resolver's legacy shape and from a rescan.
        """
        directive = self._directive
        if directive is None or not directive.replace_existing:
            return True
        consented = set(duplicate_albums_still_present(self.lib, directive.replace_existing))
        stale = [album for album in found_duplicates if _require_id(album.id) not in consented]
        for album in stale:
            logger.warning(
                "bank apply replace refused: library album %s is in the collision but was not "
                "on the banked prompt (%s)",
                album.id,
                _album_label(self.lib, album),
            )
        return not stale

    def _replace_refused(self, task: ImportTask, index: int, note: str) -> BeetsDuplicateAction:
        """Say why this Replace imported nothing, then answer beets SKIP.

        Also latches ``_replace_was_refused``, so the post-run pass does not
        trash a target this hook already failed on
        (``test_a_refused_hook_latches_that_the_run_replaced_nothing``,
        ``test_a_refused_replace_stops_the_banked_seed_for_the_rest_of_the_run``).
        """
        self._replace_was_refused = True
        self.bridge.note_outcome(self._dup_outcome(index, task).model_copy(update={"note": note}))
        return BeetsDuplicateAction.SKIP

    def _dispose_duplicates_now(
        self, states: list[tuple[Any, _FileState]], *, task: ImportTask
    ) -> str | None:
        """Trash or empty each classified duplicate. ``None`` on success, else why not.

        Order and freshness, both measured:

        * the albums WITH files go first and the ghosts last, so a failed move
          never costs a ghost its rows. In the other order a ghost's rows went,
          the next move failed, and the note said "moved 1 of 2 old copies to
          Trash" with Trash empty.
        * each album's state is RE-READ immediately before its own disposal: two
          duplicates can name one set of files (a ``hardlink`` keep-both does it
          by construction), and the first move leaves the second a ghost. The
          up-front pass answers the all-or-nothing refusals, not the arm
          (``test_two_duplicates_over_one_file_set_replace_together``).

        The arms fail closed — a state that is neither ``present`` nor ``absent``
        raises. No rollback: if ``trash_album`` raises after
        :func:`_drop_rows_the_run_is_reading` the row is gone, DB metadata for a
        file still where it was, which beets drops itself at ``task.add``.

        Two duties beyond the disposal: re-check the store layout, and record
        each album's item ids BEFORE disposing of it, so a raise after the files
        moved still leaves the re-export knowing which playlists broke.
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
        moved = 0
        # Defensive re-note, for a caller that reaches the hook without passing
        # choose_match.
        self._source_files.note(task)
        # Files first, ghosts last (stable, so two ghosts keep their order).
        ordered = sorted(states, key=lambda pair: pair[1] is not _FileState.present)
        try:
            for album, _classified in ordered:
                self._dropped_item_ids.update(_require_id(item.id) for item in album.items())
                album_id = _require_id(album.id)
                state = _album_file_state(lib, album)
                if state is _FileState.present:
                    for path in _drop_rows_the_run_is_reading(lib, album, self._source_files):
                        logger.warning(
                            "import replace: library album %s has a row naming %s, which this "
                            "import is reading; the row was dropped and the file left alone",
                            album_id,
                            path,
                        )
                    # Re-read AFTER the drop: an album whose only present row was
                    # dropped takes the mover's ghost arm, which rmdirs the
                    # container it made, so counting it would make
                    # _replace_partial_note name a container that is not there.
                    lands_in_trash = _album_file_state(lib, album) is _FileState.present
                    trash_album(lib, album, trash_dir=trash_dir, origins_dir=origins_dir)
                    if lands_in_trash:
                        moved += 1
                elif state is _FileState.absent:
                    # No track file left: beets' own remove drops the rows and
                    # touches no file, so a cover the user kept stays put.
                    album.remove(delete=False)
                else:
                    raise _ReplaceStateChanged(f"album {album_id} answered {state} at disposal")
                self._hook_replaced_album_ids.add(album_id)
        except Exception:
            logger.exception("import replace: disposing of a library copy failed")
            return _replace_partial_note(moved, len(states))
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
        task_paths = _task_source_paths(task)
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
        # A stop lands here first: abort BEFORE this album claims a feed index.
        self._check_stop()
        # The duplicate gate MUST be armed before beets' _resolve_duplicates runs
        # (a later stage in the same album task), because it reads
        # task.find_duplicates. Installed exactly ONCE per album task: this is the
        # single choose seam a task passes through before beets hands it to
        # _resolve_duplicates, so no task is armed twice in the normal flow.
        # (_install_dup_guard keeps a pristine stash purely as a cheap safety net
        # so a hypothetical re-install re-wraps the original, not a wrapper; that
        # is not an expected re-entrancy.)
        self._install_dup_guard(task)
        # Record what this task is reading while it is still in the download
        # folder: every task passes this hook, landed or skipped, before beets
        # places any file. AFTER the stop check on purpose - a stopped task is
        # discarded before it reads anything, and note() stats every item, so
        # the run's source set stays the set of files it actually touched. The
        # one consumer that could want the wider set is the post-run replace
        # disposal, whose seed (_seed_replace_from_directive) returns early
        # unless an album landed.
        self._source_files.note(task)
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
        # is revision-checked — skip/asis/astracks are list-independent
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
        The banked prompt recorded WHICH albums collide, by id, so this seeds the
        post-run pass from it (``_trash_replaced_albums`` dedupes and skips
        missing albums).

        Each guard below stops the pass moving files on a premise that does not
        hold:

        * **a Replace this run already refused enforces nothing** — the hook
          answered SKIP for those duplicates, and another task landing would
          otherwise let the pass trash them
          (``test_a_refused_replace_stops_the_banked_seed_for_the_rest_of_the_run``).
        * **something must have landed.** A directive whose pinned lookup
          resolves nothing SKIPs and ``run()`` returns normally, so an ungated
          seed would Trash the user's only copies while importing nothing.
        * **identity, not just presence.** beets ids are reused SQLite rowids, so
          ``duplicate_albums_still_present`` re-checks each one and a mismatch is
          skipped with a warning.
        * **never an id this run just landed.** A copy deleted outside the app
          can hand the import its exact rowid, which passes the identity check
          too; trashing it would Trash the album we just imported.

        The filter before them is not a guard: an entry the hook already disposed
        of would otherwise trip the reused-id warning on a healthy run.
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

        Every action resolves THIS album. Ending the run is the stop endpoint,
        which arms the bridge - a per-album abort action was a second, silent
        stop that left ``ImportJobState.stopped`` false, so it was dropped.
        """
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
# ``run_import_worker``, the post-run Trash pass included, and it must NOT be
# narrowed to the config mutation it is named for: ``_trash_replaced_albums``
# moves albums and drops rows through the same ``Library`` handle after the
# config has been restored, so releasing earlier lets a Trash restore -- which
# runs ``run_import_worker`` on a request thread and claims no job slot -- start
# a second import while the first is still writing. The restore refuses while an
# import holds its slot (``library_busy.raise_if_swap_blocked_by_job``); this
# lock does not depend on when that slot is released. The swap lock the restore
# holds serialises nothing here: imports never take it.
#
# Without it two overlapping calls interleave: the second snapshots the first's
# FORCED values and its finally writes them in as the user's. beets re-reads that
# global late (``ImportTask.finalize`` -> ``cleanup`` at
# ``importer/tasks.py:307-311``), so an explicit MOVE can reach finalize reading
# another run's copy+delete.
_CONFIG_FORCE_LOCK = threading.Lock()

#: A contended acquire REFUSES rather than waits, because an attended import
#: holds this for the length of a human review: ``run()`` does not return until
#: the browser answers, and ``park`` ends in an untimed ``slot.reply.get()``
#: (:meth:`ImportBridge.park`). The other caller, the Trash restore
#: (``api/trash.py`` -> ``trash_manage._restore_by_import``), refuses while any
#: import holds its slot, checked under the claim lock after it takes the swap
#: lock, so the two do not meet (reasoned from the claim order, 2026-09-21; see
#: BACKLOG). The timeout stays so a new caller fails instead of hanging: a daemon
#: worker blocked in ``acquire()`` is silent -- no traceback, no log.
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


def _history_flags(
    forced: Mapping[str, object],
    *,
    directive: BankApplyDirective | None,
    sweep: bool,
    incremental: Literal[False] | None,
) -> dict[str, object]:
    """beets' import history for one run, from the four exclusive arms in order.

    A folder is recorded when ``incremental`` is on and the album was not
    SKIPped-with-``incremental_skip_later`` (``importer/tasks.py:301-305``), and
    a recorded folder is skipped before any hook fires
    (``importer/session.py:246-256``).

    A ``directive`` run is non-incremental; a ``sweep`` is incremental; then the
    ``incremental`` flag, whose ``False`` is ``beet import -I``
    (``ui/commands/import_/__init__.py:280-286``); then the file operation
    ``forced`` resolves to — a run that HARDLINKS leaves the download in place,
    so history is what stops the same folder meeting the album a second time,
    with ``incremental_skip_later`` on so a SKIPped album is offered again.
    Anything else (an inbox move, in_place, a ``link``/``reflink``/``copy``
    config) leaves the history keys to the user.

    All four pin ``resume: False``: where history is ON that repeats what beets
    does, and where it is OFF the pin is the only thing stopping a resume record
    from skipping the folder.
    """
    if directive is not None:
        return {
            "incremental": False,
            "resume": False,
            "search_ids": [directive.search_id] if directive.search_id else [],
        }
    if sweep:
        # A user's ``incremental_skip_later: yes`` stops a sweep recording
        # the folders it banked or SKIPped, so every later sweep re-banks
        # them. The bank is their re-entry path, not a re-sweep.
        return {"incremental": True, "incremental_skip_later": False, "resume": False}
    if incremental is not None:
        # The per-run override: ``False`` is ``beet import -I``, how a kept
        # folder gets re-imported after its album left the library.
        # ``resume`` off with it — beets leaves it alone when ``incremental``
        # is off, and the same task-factory check skips a folder held by a
        # resume record (``session.py:246-256``).
        # ``incremental_skip_later`` stays the user's.
        return {"incremental": incremental, "resume": False}
    if forced_file_operation(forced) == "hardlink":
        # A hardlink leaves the download in place, so history is what stops
        # the same folder meeting the album again; skipping an album must
        # not record it, so the user is offered it next time.
        #
        # ``hardlink`` alone, not every operation that keeps the download:
        # it is the spelling MusicDrop's keep-downloads setting writes into
        # beets' config (``decisions`` #53, BACKLOG "Download providers").
        # A ``link``/``reflink``/``copy`` config is the user's own, and
        # turning history on would change what their setup does.
        return {"incremental": True, "incremental_skip_later": True, "resume": False}
    return {}


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
    while recording import history. A Replace moves the old copy to Trash inside
    the duplicate hook, before beets places anything; after run() returns, this
    moves the one shape that hook never sees — a banked replace whose library
    copy was renamed since banking.

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
    clears it itself when ``incremental`` is on
    (``importer/session.py:100-101``, read live by ``want_resume`` at ``:140``),
    so ours is redundant today and kept so the behaviour does not depend on that
    coupling surviving a bump: without it an aborted sweep would re-enter the
    resume path on the next run. ``singletons`` off (forced unconditionally —
    see the ``Forces`` paragraph above) — a ``singletons: yes`` user config
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

    ``incremental`` is the per-run override, the third of the four exclusive
    arms :func:`_history_flags` decides the history keys from.
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
        # turned a non-bool in the USER's config into OUR failure; beets reads
        # these with ``.get(bool)`` itself (``importer/tasks.py:307-311``). The
        # set is a superset of the keys beets' own ``set_config`` writes and
        # never restores.
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
        # Fail before the force, not inside beets' finalize: ``copy`` and
        # ``move`` are the file flags a default import leaves to the user, and
        # beets reads both with ``.get(bool)`` at ``importer/tasks.py:307-311``,
        # AFTER ``manipulate_files`` has filed the album. Without this, ``copy:
        # 1`` in a hand-edited config files the album and then fails the job.
        for validated in ("copy", "move"):
            config["import"][validated].get(bool)
        forced: dict[str, object] = {
            "duplicate_action": "ask",
            "autotag": True,
            # A DEFAULT review import is album-shaped too: under a
            # ``singletons: yes`` user config it otherwise skips every album
            # while recording import history.
            "singletons": False,
            # No import path destroys a source
            # (``test_default_operation_pins_delete_off_and_leaves_filing_to_the_user``).
            # beets keeps ``delete`` alive whenever ``copy`` survives
            # (``importer/session.py:136-138``) and then removes the originals
            # (``importer/tasks.py:326-333``), so a default import under a user
            # ``delete: yes`` is a move wearing the word "copy"; the config
            # editor advises that the key is ignored.
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
        forced.update(
            _history_flags(forced, directive=directive, sweep=sweep, incremental=incremental)
        )
        # ONE source per phase, not one per key: ``config[...][k] = v`` is
        # ``RootView.set``, which inserts a source that is never removed, so a
        # per-key shape leaves ~2N permanent overlays per import behind. A single
        # set is equivalent (keys absent from the dict still fall through to the
        # user's config) and leaves no mixed state for a concurrent reader.
        config.set({"threaded": False, "import": forced})
        try:
            # The record of what beets did to the user's files, and the signal
            # that an inbox import overrode their ``hardlink: yes`` (a
            # per-request override no config advisory can carry). On
            # ``uvicorn.error`` because an app-namespace INFO record emitted
            # nothing in the shipped container (see ``operator_logger``). Read
            # after the force, so it reports what beets will resolve.
            operator_logger.info("import file operation: %s", configured_file_operation())
            session.run()
            # The album is in the library the moment run() returns, so a failed
            # Trash move annotates rather than invalidates: reporting a committed
            # import as failed would re-trigger duplicate detection on retry.
            # Skipped when run() raises — this pass moves files on the strength
            # of an import that finished.
            try:
                _trash_replaced_albums(session)
            except Exception:
                logger.exception("post-import Trash cleanup failed; the old copy stayed in place")
        finally:
            config.set({"threaded": orig_threaded, "import": orig_import})
            # In the finally because the hook drops rows during run(): a raise
            # afterwards must not leave an export naming files that moved.
            _reexport_replaced_playlists(session)


def _trash_replaced_albums(session: WebImportSession) -> None:
    """Move every BANKED replace target to Trash (post-run, by id).

    The duplicate hooks move their own copies BEFORE beets places anything
    (``_replace_duplicates_now``); this pass covers the one shape that never
    reaches the hook — a banked replace whose library copy was renamed since
    banking, so beets' own ``find_duplicates`` misses it
    (``_seed_replace_from_directive``, vault decision 25). It runs after
    ``session.run()``: moving first would Trash the user's only copy while a bank
    apply that resolved nothing imported nothing.

    Running after placement is what makes the identity check necessary: under
    ``hardlink``/``link`` the incoming and library files are one file, so beets
    files the new album on the old album's paths and moving that album by id
    would move the new album's files. Asked per ALBUM by
    :func:`_file_identities` — same DIRECTORY ENTRY, not same inode (a refiled
    ``hardlink`` copy is one inode at two paths) and not the stored string:
    ``test_a_refiled_hardlink_sibling_still_reaches_trash``,
    ``test_a_banked_replace_sharing_a_file_drops_rows_and_moves_nothing``,
    ``test_a_symlinked_library_root_is_still_the_same_file``,
    ``test_a_link_mode_import_onto_a_symlinked_source_shares_the_file``. A
    sharing album has its rows dropped and its files left in place; a path that
    cannot be stat-ed leaves its album alone, rows and files
    (``test_an_unreadable_file_leaves_the_banked_copy_in_place``,
    ``test_an_unreadable_landed_file_skips_the_whole_pass``).

    Every row goes with the album, wherever its file LIVES
    (``test_a_banked_replace_takes_a_row_outside_the_music_folder_with_it``),
    except a row naming a file the RUN is reading: beets' own
    ``record_replaced`` covers only the byte-identical spelling and only tasks
    that reached ``task.add``.

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

    The `.m3u8` collateral is repaired once for the whole run: the ids are read
    BEFORE the rows go, and ``_reexport_replaced_playlists`` renders the exports
    after this pass.

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
            # Before anything: the ids so the run's re-export knows what left,
            # and the label, whose rowid is about to be free for reuse.
            session._dropped_item_ids.update(_require_id(item.id) for item in album.items())
            label = _album_label(lib, album)
            own, unreadable = _file_identities(lib, album)
            if unreadable:
                logger.warning(
                    "post-import Trash cleanup: %s has a file that could not be read, so it "
                    "was left in place",
                    label,
                )
                continue
            if own & landed:
                # Its files are the ones beets just filed: dropping the rows
                # keeps them from showing twice, moving them would move the new
                # album out from under itself.
                logger.warning(
                    "post-import Trash cleanup: %s shares a file with an album this import "
                    "just landed; its rows were dropped and the files left in the library, "
                    "untracked",
                    label,
                )
                album.remove(delete=False)
            else:
                dropped = _drop_rows_the_run_is_reading(lib, album, session._source_files)
                for path in dropped:
                    logger.warning(
                        "post-import Trash cleanup: %s has a row naming %s, which this import "
                        "read; the row was dropped and the file left alone",
                        label,
                        path,
                    )
                # An album left with no rows still goes to the mover: it drops
                # the album row and makes no container (``trash_album``).
                trash_album(lib, album, trash_dir=trash_dir, origins_dir=origins_dir)


def _file_identities(lib: Any, album: Any) -> tuple[set[_EntryKey], bool]:
    """The album's files as DIRECTORY ENTRIES, and whether one could not be read.

    :func:`_entry_key` with ``follow_leaf=True`` — two entries are equal exactly
    when moving one would move or break the other. Measured:

    * two hardlinks of one file in DIFFERENT folders: not equal — the normal
      ``hardlink`` refile, where the old copy is a sibling safe to move.
    * a symlinked music root, and a ``link``-mode chain (landed symlink →
      download symlink → the old album's real file): equal.
    * two hardlinks of one file in ONE folder: equal. The acknowledged
      over-refusal — that album is left in place instead of moved.
    * a dangling link contributes nothing, like a row naming a deleted file.

    A path that is simply absent is not "could not be read" — only an ``OSError``
    that is neither a missing path nor a non-directory component is.
    ``follow_leaf=True`` MUST disagree with the ownership question
    (:class:`_SourceFiles`), since moving the file breaks a library symlink.
    """
    identities: set[_EntryKey] = set()
    unreadable = False
    for raw in _album_file_paths(album):
        key, missed = _entry_key(_abs_path(lib, raw), follow_leaf=True)
        if key is not None:
            identities.add(key)
        unreadable = unreadable or missed
    return identities, unreadable


def _landed_file_identities(lib: Any, landed_album_ids: set[int]) -> tuple[set[_EntryKey], bool]:
    """The same, over every album this run imported."""
    identities: set[_EntryKey] = set()
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

    One point for the whole run: both Replace routes record their dropped ids on
    the session, so the exports are rendered once from the final store rather
    than twice from two halves of it.

    Best-effort and swallowing, like its caller's Trash pass — a committed import
    reported as failed would re-trigger duplicate detection on retry. The count
    is LOGGED, not returned: carrying it would widen the ImportRunner protocol,
    the registry and ``ImportJobState`` for a figure the UI cannot show.
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
