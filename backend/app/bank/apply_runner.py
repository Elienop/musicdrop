"""The bank apply runner — one daemon thread draining decided rows FIFO.

Each ``queued`` row runs as a real import through the SAME single slot every
other import uses (``registry.start``, origin ``bank_apply``): the runner
consumes the existing gate union, it is never a new mutex participant — the
acquisition queue's exact posture. The durable queue IS the bank directory:
every loop pass picks the oldest-decided ``queued`` row off disk, so starting
the thread is the whole restart story (rows queued before a crash drain with
no re-posted decision; ``reconcile_interrupted`` has already reverted any row
caught mid-apply). Every per-row failure lands in the row's ``error`` — the
drain itself never dies. No beets imports (rule 3): the banked decision
travels as a ``BankApplyDirective`` and the registry seam does the rest.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

from app.bank import store as bank_store
from app.bank.fingerprint import folder_fingerprint
from app.import_jobs.gates import import_gate_clear
from app.import_jobs.registry import ImportJobRegistry
from app.models.bank import BankApplyDirective, BankItem, BankStatus
from app.models.import_api import ImportAlbumStatus, ImportJobState, ImportPhase
from app.models.import_models import ImportOptions

logger = logging.getLogger(__name__)

_STALE_CHANGED_ERROR = (
    "the folder changed since it was banked - nothing was imported; re-sweep or remove the row"
)
_STALE_GONE_ERROR = "the banked folder no longer exists - nothing was imported"
_DUP_BLOCKED_ERROR = (
    "the album duplicates one already in your library - decide again with a duplicate action"
)
_NO_ALBUM_ERROR = (
    "the apply produced no library album (the release lookup may have "
    "returned nothing) - decide again to retry"
)
_UNCONFIRMED_ERROR = (
    "the apply result could not be confirmed (the import slot was replaced) - "
    "check the library before re-deciding"
)
_NOTHING_IMPORTED_ERROR = (
    "the apply imported nothing (the lookup may have failed transiently) - decide again"
)


def directive_for(item: BankItem) -> BankApplyDirective:
    """Translate a queued row's decision into the session directive.

    ``apply`` resolves ``candidate_index`` against the BANKED options list
    (None -> top; out-of-range falls back to top, mirroring _apply_choice)
    and carries that option's ``release_id`` as the ``search_ids`` pin.
    No stored id (duplicate rows have no parked payload; rows banked before
    release ids were recorded) -> ``search_id=None``: the run does an
    unpinned lookup and the session takes its top candidate (documented
    caveat - the match is re-run rather than replayed).
    """
    decision = item.decided
    if decision is None:
        # The BankItem validator forbids a queued row without a decision;
        # defensive for a hand-edited row file.
        raise RuntimeError("queued row has no decision")
    if decision.action == "duplicate":
        # "decide once": when the row carries a parked candidate, pin the
        # selected option's release_id so the apply imports exactly the chosen
        # release while resolving the collision. Sweep-banked dup rows (no
        # parked payload) stay unpinned, exactly as before.
        dup_search_id: str | None = None
        if item.parked is not None and item.parked.candidate.options:
            options = item.parked.candidate.options
            idx = decision.candidate_index or 0
            chosen = options[idx] if 0 <= idx < len(options) else options[0]
            dup_search_id = chosen.release_id
        return BankApplyDirective(
            action="duplicate",
            search_id=dup_search_id,
            duplicate_action=decision.duplicate_action,
        )
    if decision.action == "apply":
        search_id: str | None = None
        if item.parked is not None and item.parked.candidate.options:
            options = item.parked.candidate.options
            idx = decision.candidate_index or 0
            chosen = options[idx] if 0 <= idx < len(options) else options[0]
            search_id = chosen.release_id
        return BankApplyDirective(action="apply", search_id=search_id)
    if decision.action == "asis":
        return BankApplyDirective(action="asis")
    if decision.action == "astracks":
        return BankApplyDirective(action="astracks")
    raise RuntimeError(f"a {decision.action} decision is never queued")


class BankApplyRunner:
    """Single-thread FIFO drain of queued bank rows behind the import slot."""

    def __init__(
        self,
        *,
        bank_dir: Path,
        import_registry: ImportJobRegistry,
        swap_lock: asyncio.Lock | None = None,
        poll_interval: float = 0.5,
        busy_backoff: float = 1.0,
        idle_poll: float = 2.0,
    ) -> None:
        self._bank_dir = bank_dir
        self._import_registry = import_registry
        self._swap_lock = swap_lock
        self._poll_interval = poll_interval
        self._busy_backoff = busy_backoff
        # How long the drain sleeps when the bank has no queued rows; poke()
        # cuts the wait short, so this is a correctness backstop, not latency.
        self._idle_poll = idle_poll
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # ----- lifecycle -----

    def start(self) -> None:
        """Spawn the drain daemon thread. The first loop pass scans the bank,
        so rows queued before a restart drain immediately - the startup kick
        is starting the thread, nothing else."""
        self._thread = threading.Thread(
            target=self._drain, name="musicdrop-bank-apply", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal shutdown, unblock the drain, and join it. Safe to call twice.

        A row caught mid-apply stays ``applying``; the next startup's
        ``reconcile_interrupted`` reverts it with a note (never blind-requeued).
        """
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def poke(self) -> None:
        """Wake the drain now (called after a decision queues a row)."""
        self._wake.set()

    # ----- drain -----

    def _drain(self) -> None:
        while not self._stop.is_set():
            item = bank_store.next_queued(self._bank_dir)
            if item is None:
                self._wake.wait(self._idle_poll)
                self._wake.clear()
                continue
            try:
                self._apply_one(item)
            # Broad by design: every per-row crash must land in the row's
            # error, never kill the drain thread.
            except Exception as exc:
                logger.exception("bank apply failed for %s", item.folder)
                bank_store.set_status(
                    self._bank_dir,
                    item.id,
                    "failed",
                    error=str(exc) or exc.__class__.__name__,
                )

    def _apply_one(self, item: BankItem) -> None:
        if not self._wait_for_gate():
            return  # shutting down; the row stays queued
        # CAS claim: only a still-queued row may flip to applying. A row
        # deleted OR re-banked (reset to needs_review, decided=None) between
        # the pick and the claim returns None - skip it; the drain moves on.
        claimed = bank_store.set_status(self._bank_dir, item.id, "applying", expected="queued")
        if claimed is None:
            return
        try:
            current = folder_fingerprint(Path(claimed.folder))
        except FileNotFoundError:
            bank_store.set_status(self._bank_dir, item.id, "stale", error=_STALE_GONE_ERROR)
            return
        if current != claimed.fingerprint:
            bank_store.set_status(self._bank_dir, item.id, "stale", error=_STALE_CHANGED_ERROR)
            return
        directive = directive_for(claimed)
        try:
            job_id = self._import_registry.start(
                claimed.folder,
                # default operation = the user's beets config, exactly like a
                # manual import; the worker's in-library guard force-corrects
                # in-library folders to move (spec §3/§6).
                options=ImportOptions(operation="default"),
                origin="bank_apply",
                directive=directive,
            )
        except RuntimeError:
            # The slot was claimed between the gate check and start() (TOCTOU,
            # acquisition's defer posture): revert, back off, retry next pass.
            bank_store.set_status(self._bank_dir, item.id, "queued")
            self._stop.wait(self._busy_backoff)
            return
        try:
            state = self._wait_for_result(job_id)
        except KeyError:
            # The slot was replaced before the result could be read (tiny
            # window): never assume success.
            bank_store.set_status(self._bank_dir, item.id, "failed", error=_UNCONFIRMED_ERROR)
            return
        if state is None:
            return  # shutting down mid-apply; startup reconciliation reverts
        status, error, album_id = self._classify(claimed, state)
        bank_store.set_status(self._bank_dir, item.id, status, error=error, album_id=album_id)

    def _wait_for_gate(self) -> bool:
        """Block until the import gate is free. ``False`` when shutting down."""
        while not self._stop.is_set():
            if import_gate_clear(self._import_registry, self._swap_lock):
                return True
            self._stop.wait(self._poll_interval)
        return False

    def _wait_for_result(self, job_id: str) -> ImportJobState | None:
        """Poll the apply job to a terminal phase. ``None`` when shutting down.

        Raises ``KeyError`` (from ``registry.state``) if a new import claimed
        the slot before the terminal state could be read.
        """
        while not self._stop.is_set():
            state = self._import_registry.state(job_id)
            if state.phase in (ImportPhase.done, ImportPhase.failed):
                return state
            self._stop.wait(self._poll_interval)
        return None

    @staticmethod
    def _classify(
        item: BankItem, state: ImportJobState
    ) -> tuple[BankStatus, str | None, int | None]:
        """Map the finished apply job onto the row's terminal status.

        Decision-aware, and ``done`` always needs POSITIVE evidence (a
        transient lookup failure makes the session SKIP while the job still
        finishes phase=done - phase alone proves nothing landed):

        * ``duplicate`` -> done only when the dup outcome is on the feed (its
          resolution was auto-answered) OR an album id landed (the library
          copy vanished, so the album just imported); a clean SKIP run failed.
        * any other decision that surfaced a duplicate was SKIPped by the
          session -> failed with re-decide guidance.
        * ``astracks`` lands singletons (no album entity) -> done with
          album_id None, but only when its applied outcome reached the feed.
        * ``apply``/``asis`` without a landed album id failed (a pinned id
          that resolved nothing, an unreadable folder).
        """
        if state.phase is ImportPhase.failed:
            return "failed", state.error or "import failed", None
        album_id = next((a.album_id for a in state.albums if a.album_id is not None), None)
        decision = item.decided
        action = decision.action if decision is not None else "apply"
        dup_resolution_ran = any(
            a.status is ImportAlbumStatus.needs_dup_resolution for a in state.albums
        )
        if action == "duplicate":
            if dup_resolution_ran or album_id is not None:
                return "done", None, album_id
            return "failed", _NOTHING_IMPORTED_ERROR, None
        if dup_resolution_ran:
            return "failed", _DUP_BLOCKED_ERROR, None
        if action == "astracks":
            if any(a.status is ImportAlbumStatus.applied for a in state.albums):
                return "done", None, album_id
            return "failed", _NOTHING_IMPORTED_ERROR, None
        if album_id is None:
            return "failed", _NO_ALBUM_ERROR, None
        return "done", None, album_id
