"""The bank store — one JSON row per set-aside album under ``<beets_dir>/bank/``.

Same recipe as the playlists store (uuid-hex ids + id-regex traversal guard +
``write_atomic_text``), with one addition: a module-level mutation lock,
because chunk 3's import worker thread writes rows while the API thread reads
and mutates them. Pure filesystem I/O — no beets imports.
"""

from __future__ import annotations

import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.models.bank import (
    BankDecision,
    BankItem,
    BankItemSummary,
    BankReason,
    BankSource,
    BankStatus,
)
from app.models.import_models import DuplicatePrompt, ParkedAlbum
from app.playlists.atomic import write_atomic_text

_VALID_ID = re.compile(r"\A[0-9a-f]{32}\Z")

# The Review page's default "needs attention" view: in-flight rows stay
# visible, resolved rows (done/ignored) don't.
ACTIVE_STATUSES: frozenset[str] = frozenset(
    {"needs_review", "queued", "applying", "failed", "stale"}
)

# One lock for all mutations: the sweep worker (chunk 3) and the API thread
# both write; per-row files keep contention negligible. It ALSO guards the
# in-memory summary index below (reads included) so a listing never races a
# write-through update.
_LOCK = threading.Lock()

# In-memory summary index — the antidote to the O(N^2) full-dir re-glob every
# list/count/upsert used to pay. Two dicts per bank dir, keyed by the resolved
# path: id -> summary (the list-row projection) and folder -> id (upsert's O(1)
# dedupe lookup). Built lazily with ONE glob+parse pass, then kept coherent
# WRITE-THROUGH by every mutation (all writes funnel through ``_write``, all
# removals through the unlink in ``delete_item``). CONSTRAINT: the app is the
# single writer of the bank dir — rows added out-of-band are seen only after
# ``reset_bank_index()`` (or a restart). All index helpers assume the caller
# already holds ``_LOCK``.
_INDEX: dict[str, dict[str, BankItemSummary]] = {}
_FOLDER: dict[str, dict[str, str]] = {}

# The heavy fields a summary drops (kept identical to what ``list_page`` needs).
# A plain set so it satisfies pydantic ``model_dump(exclude=...)``'s IncEx type.
_SUMMARY_EXCLUDE: set[str] = {
    "parked",
    "duplicate",
    "decided",
    "fingerprint",
    "decided_at",
    "resolved_at",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _row_path(bank_dir: Path, item_id: str) -> Path:
    return bank_dir / f"{item_id}.json"


def _index_key(bank_dir: Path) -> str:
    return str(Path(bank_dir).resolve())


def _summary_of(item: BankItem) -> BankItemSummary:
    return BankItemSummary(**item.model_dump(exclude=_SUMMARY_EXCLUDE))


def _ensure_index(bank_dir: Path) -> tuple[dict[str, BankItemSummary], dict[str, str]]:
    """Return (id->summary, folder->id) for ``bank_dir``, building on first use.

    The one and only place ``_all_items`` (the full glob+parse) runs; every
    later call reuses the cached dicts.
    """
    key = _index_key(bank_dir)
    by_id = _INDEX.get(key)
    if by_id is not None:
        return by_id, _FOLDER[key]
    by_id = {}
    by_folder: dict[str, str] = {}
    for item in _all_items(bank_dir):
        by_id[item.id] = _summary_of(item)
        by_folder[item.folder] = item.id
    _INDEX[key] = by_id
    _FOLDER[key] = by_folder
    return by_id, by_folder


def _index_put(bank_dir: Path, item: BankItem) -> None:
    by_id, by_folder = _ensure_index(bank_dir)
    by_id[item.id] = _summary_of(item)
    by_folder[item.folder] = item.id


def _index_drop(bank_dir: Path, item_id: str) -> None:
    by_id, by_folder = _ensure_index(bank_dir)
    summary = by_id.pop(item_id, None)
    if summary is not None and by_folder.get(summary.folder) == item_id:
        del by_folder[summary.folder]


def _index_forget(bank_dir: Path, item_id: str) -> None:
    """Stale-entry drop when a file backing an indexed id is gone.

    Never builds the index (no glob): a missing file for an id we never indexed
    is a no-op. Callers MUST hold ``_LOCK`` — a concurrent ``list_page`` iterates
    these dicts under it, and an unlocked pop could break that iteration.
    """
    key = _index_key(bank_dir)
    by_id = _INDEX.get(key)
    if by_id is None:
        return
    summary = by_id.pop(item_id, None)
    if summary is not None:
        by_folder = _FOLDER.get(key)
        if by_folder is not None and by_folder.get(summary.folder) == item_id:
            del by_folder[summary.folder]


def reset_bank_index() -> None:
    """Drop the in-memory index so the next read rebuilds from disk.

    For tests (per-test tmp dirs share this module global) and the rare case
    where rows were written to the bank dir out-of-band.
    """
    _INDEX.clear()
    _FOLDER.clear()


def _write(bank_dir: Path, item: BankItem) -> None:
    write_atomic_text(_row_path(bank_dir, item.id), item.model_dump_json(indent=2))
    _index_put(bank_dir, item)


def create_item(
    bank_dir: Path,
    *,
    folder: str,
    source: BankSource,
    reason: BankReason,
    fingerprint: str,
    artist: str | None = None,
    album: str | None = None,
    recommendation: str | None = None,
    confidence: float | None = None,
    parked: ParkedAlbum | None = None,
    duplicate: DuplicatePrompt | None = None,
) -> BankItem:
    item = BankItem(
        id=uuid.uuid4().hex,
        folder=folder,
        source=source,
        reason=reason,
        artist=artist,
        album=album,
        recommendation=recommendation,
        confidence=confidence,
        parked=parked,
        duplicate=duplicate,
        fingerprint=fingerprint,
        status="needs_review",
        banked_at=_now(),
    )
    with _LOCK:
        _write(bank_dir, item)
    return item


def get_item(bank_dir: Path, item_id: str) -> BankItem | None:
    if not _VALID_ID.match(item_id):
        return None
    try:
        raw = _row_path(bank_dir, item_id).read_text(encoding="utf-8")
    except OSError:
        # No index cleanup here: get_item is called WITHOUT _LOCK from the API,
        # and the index may only be mutated under it. A missing-file-for-indexed
        # -id state can't arise under the single-writer invariant anyway; true
        # out-of-band edits are handled by reset_bank_index().
        return None
    try:
        return BankItem.model_validate_json(raw)
    except ValueError:
        return None


def _all_items(bank_dir: Path) -> list[BankItem]:
    if not bank_dir.exists():
        return []
    items: list[BankItem] = []
    for child in bank_dir.glob("*.json"):
        try:
            items.append(BankItem.model_validate_json(child.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue  # unreadable/corrupt rows never break the listing
    # Deterministic order for index building; the DISPLAY order (newest banked
    # first) is applied in list_page.
    items.sort(key=lambda item: (item.banked_at, item.id))
    return items


def list_page(
    bank_dir: Path,
    *,
    status: BankStatus | None = None,
    active_only: bool = False,
    reason: BankReason | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[BankItemSummary], int, int]:
    """One index pass -> (page, total_filtered, total_all).

    ``total_all`` counts rows of ANY status (drives the Review page's section
    visibility so resolved history stays reachable when the active view empties
    out). A specific ``status`` wins over ``active_only`` (the router never
    sends both); ``reason`` ANDs with whichever status narrowing is in effect.
    """
    with _LOCK:
        by_id, _ = _ensure_index(bank_dir)
        # Display order: newest banked first (the Review page reads top-down);
        # id breaks timestamp ties. The APPLY order is next_queued's
        # oldest-decided FIFO — a queue, not this display sort.
        rows = sorted(by_id.values(), key=lambda s: (s.banked_at, s.id), reverse=True)
        total_all = len(rows)
        if status is not None:
            rows = [s for s in rows if s.status == status]
        elif active_only:
            rows = [s for s in rows if s.status in ACTIVE_STATUSES]
        if reason is not None:
            rows = [s for s in rows if s.reason == reason]
        total = len(rows)
        page = rows[offset : offset + limit]
    return page, total, total_all


def list_items(
    bank_dir: Path,
    *,
    status: BankStatus | None = None,
    active_only: bool = False,
    offset: int = 0,
    limit: int = 50,
) -> list[BankItemSummary]:
    page, _total, _total_all = list_page(
        bank_dir, status=status, active_only=active_only, offset=offset, limit=limit
    )
    return page


def count_items(
    bank_dir: Path,
    *,
    status: BankStatus | None = None,
    active_only: bool = False,
) -> int:
    _page, total, _total_all = list_page(
        bank_dir, status=status, active_only=active_only, offset=0, limit=0
    )
    return total


class InvalidTransitionError(RuntimeError):
    """A decision/delete that the row's current status forbids (API -> 409)."""


# Statuses a new decision may leave from: fresh rows, failed applies (retry),
# and stale rows (the user re-decides after a re-scan; chunk 4 sets stale).
_DECIDABLE: frozenset[str] = frozenset({"needs_review", "failed", "stale"})


def decide_item(bank_dir: Path, item_id: str, decision: BankDecision) -> BankItem | None:
    with _LOCK:
        item = get_item(bank_dir, item_id)
        if item is None:
            return None
        if item.status not in _DECIDABLE:
            raise InvalidTransitionError(
                f"row is {item.status}; decisions need one of {sorted(_DECIDABLE)}"
            )
        item.decided = decision
        item.decided_at = _now()
        item.error = None
        if decision.action == "ignore":
            item.status = "ignored"
            item.resolved_at = _now()
        else:
            item.status = "queued"
        _write(bank_dir, item)
        return item


# Statuses a search may re-park from: fresh rows and failed applies. Stale is
# deliberately narrower than _DECIDABLE: a searched payload must describe the
# banked files, and a stale row's folder no longer does — its rescue is the
# stale screen's attended re-scan.
_SEARCHABLE: frozenset[str] = frozenset({"needs_review", "failed"})


def research_item(
    bank_dir: Path,
    item_id: str,
    *,
    parked: ParkedAlbum,
    artist: str | None,
    album: str | None,
    recommendation: str | None,
    confidence: float | None,
) -> BankItem | None:
    """Replace a row's candidate payload with a fresh search result.

    The API pre-checks unlocked; this locked re-check wins any race with a
    concurrent decision. Reason flips to needs_review (a no_match row gains
    its first payload); STATUS is preserved (failed stays failed — the
    banner's "decide again" stays true). ``decided``/``error`` untouched.
    """
    with _LOCK:
        item = get_item(bank_dir, item_id)
        if item is None:
            return None
        if item.status not in _SEARCHABLE or item.reason == "needs_dup_resolution":
            raise InvalidTransitionError(
                f"row is {item.status}/{item.reason}; a search needs an undecided match row"
            )
        item.parked = parked
        item.reason = "needs_review"
        item.artist = artist
        item.album = album
        item.recommendation = recommendation
        item.confidence = confidence
        _write(bank_dir, item)
        return item


# Statuses a rescan may act from — like _SEARCHABLE plus stale: the rescan IS
# the stale row's in-place rescue (the folder changed; re-read it and bless
# the new state with a fresh fingerprint).
_RESCANNABLE: frozenset[str] = frozenset({"needs_review", "failed", "stale"})


def rescan_item(
    bank_dir: Path,
    item_id: str,
    *,
    fingerprint: str,
    parked: ParkedAlbum | None,
    artist: str | None,
    album: str | None,
    recommendation: str | None,
    confidence: float | None,
) -> BankItem | None:
    """Replace a row's payload AND fingerprint after a deliberate folder rescan.

    ``parked`` None means the default lookup matched nothing — the row
    honestly becomes a ``no_match`` row. Any old duplicate prompt is cleared
    (the up-front collision check re-flags it if still real). A ``stale`` row
    resets to ``needs_review`` with ``decided``/``error`` cleared (the
    re-bank precedent); ``needs_review``/``failed`` keep their status.
    """
    with _LOCK:
        item = get_item(bank_dir, item_id)
        if item is None:
            return None
        if item.status not in _RESCANNABLE:
            raise InvalidTransitionError(f"row is {item.status}; a rescan needs an undecided row")
        item.fingerprint = fingerprint
        item.parked = parked
        item.duplicate = None
        item.reason = "needs_review" if parked is not None else "no_match"
        item.artist = artist
        item.album = album
        item.recommendation = recommendation
        item.confidence = confidence
        if item.status == "stale":
            item.status = "needs_review"
            item.decided = None
            item.error = None
        _write(bank_dir, item)
        return item


def set_status(
    bank_dir: Path,
    item_id: str,
    status: BankStatus,
    *,
    error: str | None = None,
    album_id: int | None = None,
    expected: BankStatus | None = None,
) -> BankItem | None:
    """Bookkeeping transition (chunk 4's apply runner + reconciliation use it).

    ``album_id`` is only ever supplied with ``done`` (the apply landed an
    album); None leaves the field untouched so failure paths never erase a
    previously recorded id. ``expected`` makes the write a compare-and-set:
    when given and the row's current status differs, return None WITHOUT
    writing — the apply runner's queued->applying claim uses it so a row
    re-banked under a stale reference (reset to needs_review, decided=None)
    is never blind-overwritten into a validator-rejected state (row loss).
    """
    with _LOCK:
        item = get_item(bank_dir, item_id)
        if item is None:
            return None
        if expected is not None and item.status != expected:
            return None
        item.status = status
        item.error = error
        if album_id is not None:
            item.album_id = album_id
        if status in ("done", "failed", "ignored"):
            item.resolved_at = _now()
        _write(bank_dir, item)
        return item


def delete_item(bank_dir: Path, item_id: str) -> bool:
    with _LOCK:
        item = get_item(bank_dir, item_id)
        if item is None:
            return False
        if item.status == "applying":
            raise InvalidTransitionError("row is applying; wait for the apply to finish")
        try:
            _row_path(bank_dir, item_id).unlink()
        except FileNotFoundError:
            _index_forget(bank_dir, item_id)  # already gone: keep the index honest
            return False
        _index_drop(bank_dir, item_id)
        return True


def bulk_ignore(bank_dir: Path, ids: list[str]) -> int:
    """Ignore every listed row still in ``needs_review``; skip the rest."""
    flipped = 0
    for item_id in ids:
        with _LOCK:
            item = get_item(bank_dir, item_id)
            if item is None or item.status != "needs_review":
                continue
            item.status = "ignored"
            item.resolved_at = _now()
            _write(bank_dir, item)
            flipped += 1
    return flipped


def bulk_delete(bank_dir: Path, ids: list[str]) -> int:
    """Delete every listed deletable row; return how many were actually removed.

    Reuses ``delete_item``'s rules (id validation, file unlink) but never lets
    one bad id abort the batch: an ``applying`` row (``delete_item`` raises
    ``InvalidTransitionError``) and a missing id (returns False) are skipped.
    """
    deleted = 0
    for item_id in ids:
        try:
            if delete_item(bank_dir, item_id):
                deleted += 1
        except InvalidTransitionError:
            continue  # an applying row can't be deleted - skip, don't abort
    return deleted


def upsert_by_folder(
    bank_dir: Path,
    *,
    folder: str,
    source: BankSource,
    reason: BankReason,
    fingerprint: str,
    artist: str | None = None,
    album: str | None = None,
    recommendation: str | None = None,
    confidence: float | None = None,
    parked: ParkedAlbum | None = None,
    duplicate: DuplicatePrompt | None = None,
) -> BankItem:
    """Bank a folder, deduplicating on (folder): same fingerprint refreshes
    ``banked_at``; a changed fingerprint replaces the payload and resets the
    row to ``needs_review`` (the spec's dedupe rule — a re-banked folder is a
    fresh decision)."""
    with _LOCK:
        _by_id, by_folder = _ensure_index(bank_dir)
        existing_id = by_folder.get(folder)
        existing = get_item(bank_dir, existing_id) if existing_id is not None else None
        if existing is None:
            pass  # fall through to create below (outside the lock reuse)
        elif existing.fingerprint == fingerprint:
            existing.banked_at = _now()
            _write(bank_dir, existing)
            return existing
        else:
            replaced = existing.model_copy(
                update={
                    "source": source,
                    "reason": reason,
                    "artist": artist,
                    "album": album,
                    "recommendation": recommendation,
                    "confidence": confidence,
                    "parked": parked,
                    "duplicate": duplicate,
                    "fingerprint": fingerprint,
                    "status": "needs_review",
                    "decided": None,
                    "error": None,
                    "banked_at": _now(),
                    "decided_at": None,
                    "resolved_at": None,
                }
            )
            _write(bank_dir, replaced)
            return replaced
    return create_item(
        bank_dir,
        folder=folder,
        source=source,
        reason=reason,
        fingerprint=fingerprint,
        artist=artist,
        album=album,
        recommendation=recommendation,
        confidence=confidence,
        parked=parked,
        duplicate=duplicate,
    )


def reconcile_interrupted(bank_dir: Path) -> int:
    """Startup pass: rows stuck in ``applying`` (process died mid-apply) revert
    to ``needs_review`` with a note. Never blind-requeues (spec §5/§8)."""
    flipped = 0
    with _LOCK:
        by_id, _ = _ensure_index(bank_dir)
        applying_ids = [s.id for s in by_id.values() if s.status == "applying"]
    for item_id in applying_ids:
        with _LOCK:
            fresh = get_item(bank_dir, item_id)
            if fresh is None or fresh.status != "applying":
                continue
            fresh.status = "needs_review"
            fresh.error = "apply interrupted by a restart - decide again"
            _write(bank_dir, fresh)
            flipped += 1
    return flipped


def next_queued(bank_dir: Path) -> BankItem | None:
    """The apply runner's FIFO head: the oldest-decided ``queued`` row.

    Ordered by ``decided_at`` (the spec's apply order — decision time, not
    banking time), id as the tie-break. ``decided_at`` is always set on a
    queued row (decide_item stamps it); ``banked_at`` is a defensive fallback
    for a hand-edited row file.
    """
    with _LOCK:
        by_id, _ = _ensure_index(bank_dir)
        queued_ids = [s.id for s in by_id.values() if s.status == "queued"]
        # decided_at lives only on the full row (not the summary), so load the
        # queued rows — a small set — to order by it.
        queued = [
            row
            for row in (get_item(bank_dir, item_id) for item_id in queued_ids)
            if row is not None and row.status == "queued"
        ]
        if not queued:
            return None
        return min(queued, key=lambda item: (item.decided_at or item.banked_at, item.id))
