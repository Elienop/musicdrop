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
travels as a ``BankApplyDirective``, the one library read this module needs
goes through a typed adapter function, and the registry seam does the rest.

A ``duplicate`` decision is ENFORCED from the banked prompt's stored library
album ids, not from beets' name-keyed re-detection at apply time — beets calls
its duplicate hook only when ``task.find_duplicates()`` hits on the CHOSEN
release's albumartist+album, so a renamed library copy (or a release named
differently) makes the decision evaporate silently. ``skip_new`` is enforced
HERE (below), ``replace`` in the session (it needs the library at trash time),
and ``merge`` cannot be forced at all — it is reported honestly instead. What
CANNOT be enforced is still reported honestly rather than as ``done``: a merge
that landed a second copy, and a replace whose banked copies had all gone (so
nothing was trashed and the row must not claim one was).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from pathlib import Path

from app.bank import store as bank_store
from app.bank.fingerprint import folder_fingerprint
from app.beets.library import LibraryHandle, surviving_duplicate_album_ids
from app.import_jobs.gates import import_gate_clear
from app.import_jobs.registry import ImportJobRegistry
from app.models.bank import BankApplyDirective, BankItem, BankStatus
from app.models.import_api import ImportAlbumStatus, ImportJobState, ImportPhase
from app.models.import_models import DuplicateAction, ExistingAlbum, ImportOptions

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
# A merge cannot be forced after the fact: beets rebuilds and re-imports the
# combined album INSIDE its duplicate hook, and that hook never ran here. The
# album is already in the library as a second copy, so "decide again" - the
# guidance every other failure gives - would import a third. Steer to removing
# one copy instead; the Duplicates page keeps one and trashes the rest (it does
# not merge). One of the two errors carried with ``error_retryable=False`` (the
# other is the un-replaced replace below), so the banner above it drops the
# "decide again to retry" headline it contradicts.
_MERGE_NOT_MERGED_ERROR = (
    "the album landed in your library as a second copy and the merge never ran (your "
    "library copy no longer matched it) - deciding again would import it a third time; "
    "remove one of the two copies instead, from its album page or the Duplicates page"
)
# The replace that replaced nothing. A ``replace`` IS forceable - the session
# trashes the banked ids itself - but only while one of them still survives;
# when every stored copy has gone or drifted out of identity, the session logs
# "left in place" and trashes nothing, and beets' own re-detection missed too
# (no ``needs_dup_resolution`` on the feed). What actually happened is "the new
# album was imported and nothing was replaced", so reporting ``done`` would
# render the Review page's "Replaced - the old copy was moved to Trash": a false
# claim about a DESTRUCTIVE operation. Same not-retryable posture as the merge
# arm - the album is already in the library, so a re-decide imports another.
_REPLACE_NOT_REPLACED_ERROR = (
    "the album was imported but your old copy was not moved to Trash (it was gone, or it no "
    "longer matched the copy you decided about) - deciding again would import another copy; "
    "check whether a leftover copy is still there and remove it from its album page or the "
    "Duplicates page"
)


def directive_for(item: BankItem) -> BankApplyDirective:
    """Translate a queued row's decision into the session directive.

    ``apply`` and ``duplicate`` both resolve ``candidate_index`` against the
    BANKED options list (None -> top; out-of-range falls back to top,
    mirroring _apply_choice) and carry that option's ``release_id`` as the
    ``search_ids`` pin: the apply REPLAYS the banked match instead of
    re-running it.
    No stored id -> ``search_id=None``: the run does an unpinned lookup and
    the session takes its top candidate. That is the LEGACY case only - rows
    banked before the sweep stored its matched release, rows whose task had
    no match to store, and options from a source that carries no release id.
    Those rows drain by user decision; nothing back-fills them.

    A ``replace`` resolution additionally carries the banked prompt's colliding
    albums (``replace_existing``) so the session can trash them by STORED id
    whether or not beets' own re-detection fires. Every other action carries an
    empty list - only ``replace`` acts on a library album the user did not just
    import.
    """
    decision = item.decided
    if decision is None:
        # The BankItem validator forbids a queued row without a decision;
        # defensive for a hand-edited row file.
        raise RuntimeError("queued row has no decision")
    if decision.action == "duplicate":
        # "decide once": the row carries the release it was matched to (the
        # sweep banks it alongside the prompt), so pin that option's
        # release_id and import exactly it while resolving the collision. The
        # duplicate screen posts no candidate_index, so this resolves to
        # options[0] - which the sweep stores as the matched release. Legacy
        # rows with no parked payload stay unpinned.
        return BankApplyDirective(
            action="duplicate",
            search_id=_resolve_search_id(item, decision.candidate_index),
            duplicate_action=decision.duplicate_action,
            replace_existing=_replace_existing(item, decision.duplicate_action),
        )
    if decision.action == "apply":
        return BankApplyDirective(
            action="apply",
            search_id=_resolve_search_id(item, decision.candidate_index),
        )
    if decision.action == "asis":
        return BankApplyDirective(action="asis")
    if decision.action == "astracks":
        return BankApplyDirective(action="astracks")
    raise RuntimeError(f"a {decision.action} decision is never queued")


def _resolve_search_id(item: BankItem, candidate_index: int | None) -> str | None:
    """Resolve the chosen option's ``release_id`` as the search pin.

    ``candidate_index`` is resolved against the BANKED options list
    (None -> top; out-of-range falls back to top, mirroring _apply_choice).
    No parked payload, or a payload with no options -> ``None``: the run
    does an unpinned lookup and the session takes its top candidate. Only
    LEGACY rows land there now (see ``directive_for``); on such a row the
    match IS re-run rather than replayed.
    """
    parked = item.parked
    if parked is None or not parked.candidate.options:
        return None
    options = parked.candidate.options
    idx = candidate_index or 0
    chosen = options[idx] if 0 <= idx < len(options) else options[0]
    return chosen.release_id


def _replace_existing(
    item: BankItem, duplicate_action: DuplicateAction | None
) -> list[ExistingAlbum]:
    """The banked collision a ``replace`` must trash, or ``[]``.

    Only ``replace`` populates it: it is the sole resolution that touches a
    library album the run did not just import, so no other action gets a list
    the session could act on by accident.

    ``[]`` is also the honest answer for the two shapes with no stored evidence
    (invariant 4): a row whose ``duplicate`` prompt is absent - the up-front
    resolver decides a collision the sweep never banked - and a prompt that
    listed no existing album. Both keep today's trust-the-hook behaviour; an
    empty list must never read as "collision confirmed".
    """
    if duplicate_action is not DuplicateAction.replace:
        return []
    prompt = item.duplicate
    if prompt is None:
        return []
    return list(prompt.existing)


class BankApplyRunner:
    """Single-thread FIFO drain of queued bank rows behind the import slot."""

    def __init__(
        self,
        *,
        bank_dir: Path,
        import_registry: ImportJobRegistry,
        library: Callable[[], LibraryHandle],
        swap_lock: asyncio.Lock | None = None,
        poll_interval: float = 0.5,
        busy_backoff: float = 1.0,
        idle_poll: float = 2.0,
    ) -> None:
        self._bank_dir = bank_dir
        self._import_registry = import_registry
        # A GETTER, never a captured handle: the config editor's Apply rebuilds
        # the beets Library and swaps ``app.state.beets_library`` mid-process,
        # so a handle captured at construction would answer the skip_new check
        # from a DB the app no longer uses. Calling it immediately before each
        # read NARROWS that window; it does not close it. Apply's import gate is
        # CHECKED, not held ("TOCTOU acceptable for single-user", config_editor
        # .apply), and the skip_new read happens before this row starts an
        # import, so nothing stops a concurrent Apply running reset_beets_globals
        # between the getter and the read. Accepted on the same single-user
        # stance; a raise from either lands in _drain's per-row catch-all as a
        # failed row (honest - an unverifiable collision must never silently
        # import).
        # Required, not optional: a "None means skip enforcement" mode would
        # degrade to the exact bug this enforcement exists to fix, silently.
        self._library = library
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
            try:
                item = bank_store.next_queued(self._bank_dir)
            # A raise from the pick itself has no row to annotate; log and keep
            # the drain alive. A dead thread would strand every queued row until
            # a process restart. Back off so a persistent fault can't tight-spin.
            except Exception:
                logger.exception("bank apply: reading the next queued row failed")
                self._stop.wait(self._busy_backoff)
                continue
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
        # Enforced skip_new, deliberately BETWEEN the staleness checks and the
        # directive: the row is already CAS-claimed "applying" (so no second
        # drain pass can act on it) and its folder is known unchanged, but no
        # import has been started - which is the whole point, since "skip new"
        # means nothing may be imported at all.
        if self._skip_new_is_enforced(claimed):
            bank_store.set_status(self._bank_dir, item.id, "done", error=None, album_id=None)
            return
        # Read BEFORE the import starts (see the helper): it only feeds
        # classification, so nothing about the run changes either way.
        replace_targets_gone = self._replace_targets_are_gone(claimed)
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
        status, error, album_id, retryable = self._classify(claimed, state, replace_targets_gone)
        bank_store.set_status(
            self._bank_dir,
            item.id,
            status,
            error=error,
            album_id=album_id,
            error_retryable=retryable,
        )

    def _skip_new_is_enforced(self, item: BankItem) -> bool:
        """Whether "keep my copy, import nothing" must short-circuit the import.

        True only when the banked prompt named a colliding library album that
        STILL SURVIVES - present under its stored id AND identity-matching what
        was banked (ids are reused rowids; see ``_album_identity_matches``).
        Then the honest execution of the decision is to import nothing at all,
        so no import is started and the row resolves ``done`` with no album id -
        the shape the Review page already renders as "Kept your existing copy".

        False keeps TODAY'S path entirely - the run starts, and beets' own
        re-detection may still fire and SKIP (which also honestly means
        "imported nothing", and stays ``done``). Four ways to get there - one
        scoping, two defensive fall-throughs, and one real verdict:

        * not a ``skip_new`` duplicate decision - out of scope, nothing to
          enforce;
        * ``item.duplicate is None`` - the up-front-resolver flow decides a
          collision that was never banked, so there is no stored id to check;
        * ``prompt.existing == []`` - an empty stored list means "none survive",
          never "collision confirmed";
        * none of the stored albums survived - the VERDICT, not a fall-through:
          the copy the user chose to keep is gone (or that id now names a
          different album), so refusing the import would be enforcing a
          decision about something that no longer exists.
        """
        decision = item.decided
        if decision is None or decision.action != "duplicate":
            return False
        if decision.duplicate_action is not DuplicateAction.skip_new:
            return False
        prompt = item.duplicate
        if prompt is None or not prompt.existing:
            return False
        surviving = surviving_duplicate_album_ids(self._library(), prompt.existing)
        if not surviving:
            logger.info(
                "bank apply skip_new: none of the %d banked library copies survive for %s; "
                "letting the import run",
                len(prompt.existing),
                item.folder,
            )
            return False
        return True

    def _replace_targets_are_gone(self, item: BankItem) -> bool:
        """Whether a banked ``replace`` has NOTHING left to trash.

        True for exactly one shape: a ``replace`` decision whose banked prompt
        listed stored library albums, of which NONE still survives - present
        under its stored id AND identity-matching what was banked (ids are
        reused rowids; see ``_album_identity_matches``). The session's seed
        re-derives survivors itself and does the trashing; this pre-check feeds
        CLASSIFICATION only, and is the one signal that separates "replaced"
        from "imported a second copy and trashed nothing" - beets' hook missing
        the collision looks identical from the feed.

        Read BEFORE the import starts, deliberately: afterwards a reused rowid
        can make a stored id look like a survivor when it is really the album
        this run just landed (the seed's own third guard). The window between
        this read and the seed is the accepted single-user TOCTOU class already
        documented on ``self._library`` - a config Apply can swap the library in
        between, nothing here holds a lock, and a raise from the getter lands in
        _drain's catch-all as an honest failed row.

        False for everything else, including the two no-evidence shapes that
        keep today's path untouched: no banked prompt (``item.duplicate is
        None`` - the up-front resolver's flow) and a prompt that listed no
        existing album. An empty stored list must never read as "the copies
        vanished", or every such replace would report a failure.
        """
        decision = item.decided
        if decision is None or decision.action != "duplicate":
            return False
        if decision.duplicate_action is not DuplicateAction.replace:
            return False
        prompt = item.duplicate
        if prompt is None or not prompt.existing:
            return False
        surviving = surviving_duplicate_album_ids(self._library(), prompt.existing)
        if surviving:
            return False
        logger.info(
            "bank apply replace: none of the %d banked library copies survive for %s; "
            "the import will land a copy that replaces nothing",
            len(prompt.existing),
            item.folder,
        )
        return True

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
        item: BankItem, state: ImportJobState, replace_targets_gone: bool
    ) -> tuple[BankStatus, str | None, int | None, bool]:
        """Map the finished apply job onto the row's terminal status.

        ``replace_targets_gone`` is ``_replace_targets_are_gone``'s pre-import
        verdict, threaded in because it cannot be re-derived here: after the run
        has landed an album, "no stored copy survives" and "the survivor IS the
        album we just imported" are indistinguishable.

        Returns ``(status, error, album_id, error_retryable)``. The last is
        False for exactly TWO outcomes - the merge that landed a second copy and
        the replace that replaced nothing, both below - because those are the
        only failures whose error tells the user NOT to decide again; every
        other outcome's recovery IS a re-decide, so the banner's "decide again
        to retry" headline stays true.

        Decision-aware, and ``done`` always needs POSITIVE evidence (a
        transient lookup failure makes the session SKIP while the job still
        finishes phase=done - phase alone proves nothing landed):

        * ``merge`` that landed an album WITHOUT the resolution hook having run
          -> failed, honestly: a merge is performed BY that hook (beets rebuilds
          and re-imports the combined album inside it), so this state is "the
          album imported as a second copy and nothing was merged". It cannot be
          forced afterwards the way skip_new and replace can, and reporting it
          ``done`` would tell the user their library was merged when it was
          split. ``dup_resolution_ran`` is the hook's own evidence channel: the
          ``needs_dup_resolution`` feed row is emitted at the top of
          ``get_duplicate_action`` and nowhere else.
        * ``replace`` that landed an album with the hook NOT having run AND no
          banked copy left to trash -> failed, for the same reason: the session
          trashed nothing, so "Replaced - the old copy was moved to Trash" would
          be a false claim about a destructive step. Unlike merge this is a
          NARROW arm: a surviving copy is trashed by the seed and stays ``done``.
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
            return "failed", state.error or "import failed", None, True
        album_id = next((a.album_id for a in state.albums if a.album_id is not None), None)
        decision = item.decided
        action = decision.action if decision is not None else "apply"
        dup_resolution_ran = any(
            a.status is ImportAlbumStatus.needs_dup_resolution for a in state.albums
        )
        if action == "duplicate":
            dup_action = decision.duplicate_action if decision is not None else None
            return BankApplyRunner._classify_duplicate(
                dup_action, album_id, dup_resolution_ran, replace_targets_gone
            )
        if dup_resolution_ran:
            return "failed", _DUP_BLOCKED_ERROR, None, True
        if action == "astracks":
            if any(a.status is ImportAlbumStatus.applied for a in state.albums):
                return "done", None, album_id, True
            return "failed", _NOTHING_IMPORTED_ERROR, None, True
        if album_id is None:
            return "failed", _NO_ALBUM_ERROR, None, True
        return "done", None, album_id, True

    @staticmethod
    def _classify_duplicate(
        dup_action: DuplicateAction | None,
        album_id: int | None,
        dup_resolution_ran: bool,
        replace_targets_gone: bool,
    ) -> tuple[BankStatus, str | None, int | None, bool]:
        """The ``duplicate``-decision arm of ``_classify`` (same return contract)."""
        landed_unresolved = album_id is not None and not dup_resolution_ran
        if dup_action is DuplicateAction.merge and landed_unresolved:
            # album_id stays off the row on purpose: ``album_id`` is the
            # store's "the apply landed THIS" field, only ever written with
            # ``done``, and what landed here is the copy the user has to
            # clean up - the error string is where that belongs.
            return "failed", _MERGE_NOT_MERGED_ERROR, None, False
        if dup_action is DuplicateAction.replace and landed_unresolved and replace_targets_gone:
            # Same reasoning as the merge arm for keeping album_id off the row:
            # what landed is a copy the user may have to clean up, not a
            # "the apply landed THIS" success.
            return "failed", _REPLACE_NOT_REPLACED_ERROR, None, False
        if dup_resolution_ran or album_id is not None:
            return "done", None, album_id, True
        return "failed", _NOTHING_IMPORTED_ERROR, None, True
