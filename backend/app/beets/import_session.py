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
from typing import TYPE_CHECKING, Any

from beets import config
from beets.autotag.match import Recommendation as BeetsRec
from beets.importer.session import ImportAbortError, ImportSession
from beets.importer.tasks import Action

from app.beets.import_mapping import map_album_match, map_candidate_options
from app.models.import_models import (
    ImportAction,
    ImportChoice,
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
        self._replies: dict[int, queue.Queue[ImportChoice]] = {}
        self._lock = threading.Lock()
        self._pending = 0

    # ----- worker side -----

    def park(self, parked: ParkedAlbum) -> ImportChoice:
        """Push a parked album and block until a choice arrives for it."""
        reply: queue.Queue[ImportChoice] = queue.Queue(maxsize=1)
        with self._lock:
            self._replies[parked.album_index] = reply
            self._pending += 1
        self._out.put(parked)
        choice = reply.get()  # blocks the worker thread
        with self._lock:
            self._replies.pop(parked.album_index, None)
            self._pending -= 1
        return choice

    # ----- consumer side -----

    def get_parked(self, timeout: float | None = None) -> ParkedAlbum | None:
        """Pop the next parked album, or None on timeout."""
        try:
            return self._out.get(timeout=timeout)
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
    ) -> None:
        super().__init__(lib, loghandler, paths, query)
        self.bridge = bridge
        # Counter that assigns each parked album a stable index for replies.
        self._album_index = 0

    # ----- the four decision hooks -----

    def should_resume(self, path: Any) -> bool:
        # Chunk 1 exposes nothing fancy: never resume interactively.
        return False

    def choose_item(self, task: ImportTask) -> Action:
        # Singletons are out of scope for chunk 1; skip them.
        return Action.SKIP

    def resolve_duplicate(self, task: ImportTask, found_duplicates: Any) -> None:
        # Duplicate resolution UI is a later chunk; take beets' config default by
        # leaving the choice intact (no-op).
        return None

    def choose_match(self, task: ImportTask) -> Any:
        """Auto-apply a strong match; otherwise park and await the user.

        Returns either an ``AlbumMatch`` (to apply) or an ``Action`` constant.
        beets' ``set_choice`` turns an AlbumMatch into ``Action.APPLY``.
        """
        # This hook only fires for album tasks, so every candidate is an
        # AlbumMatch; typed as Any since beets' task.candidates is the wider
        # list[AlbumMatch | TrackMatch] union (singletons go through choose_item).
        candidates: list[Any] = list(task.candidates or [])
        if task.rec == BeetsRec.strong and candidates:
            # Mirror beets' auto-apply of a strong recommendation.
            return candidates[0]

        if not candidates:
            # Nothing to choose from: skip (an empty match can't be applied).
            return Action.SKIP

        # Park: map the top match + the ranked alternatives, push, block.
        # Serial-only: this session relies on config["threaded"] = False, so
        # choose_match runs on a single worker thread and _album_index is an
        # unlocked single-writer counter. Sharing a session or enabling
        # threading would race two albums onto the same reply slot.
        index = self._album_index
        self._album_index += 1
        top = candidates[0]
        options = map_candidate_options(candidates)
        rec = task.rec if task.rec is not None else BeetsRec.none
        candidate = map_album_match(
            top,
            cur_artist=task.cur_artist,
            cur_album=task.cur_album,
            options=options,
            recommendation=_REC_MAP.get(rec, Recommendation.none),
        )
        folder = self._task_folder(task)
        choice = self.bridge.park(
            ParkedAlbum(album_index=index, folder=folder, candidate=candidate)
        )
        return self._apply_choice(choice, candidates)

    # ----- helpers -----

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

    Forces single-threaded execution before delegating to beets' run loop, so
    the global beets config/plugin singletons are never touched concurrently.
    Intended to be the target of a dedicated worker thread started by the API
    layer (chunk 2); here it is the clean, tested entry point.
    """
    config["threaded"] = False
    session.run()
