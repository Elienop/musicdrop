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

# One lock for all mutations: the sweep worker (chunk 3) and the API thread
# both write; per-row files keep contention negligible.
_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now(UTC)


def _row_path(bank_dir: Path, item_id: str) -> Path:
    return bank_dir / f"{item_id}.json"


def _write(bank_dir: Path, item: BankItem) -> None:
    write_atomic_text(_row_path(bank_dir, item.id), item.model_dump_json(indent=2))


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
    # FIFO review order: oldest banked first; id breaks timestamp ties.
    items.sort(key=lambda item: (item.banked_at, item.id))
    return items


def list_items(
    bank_dir: Path,
    *,
    status: BankStatus | None = None,
    offset: int = 0,
    limit: int = 50,
) -> list[BankItemSummary]:
    items = _all_items(bank_dir)
    if status is not None:
        items = [item for item in items if item.status == status]
    page = items[offset : offset + limit]
    summaries: list[BankItemSummary] = []
    for item in page:
        data = item.model_dump(
            exclude={"parked", "duplicate", "decided", "fingerprint", "decided_at", "resolved_at"}
        )
        summaries.append(BankItemSummary(**data))
    return summaries


def count_items(bank_dir: Path, *, status: BankStatus | None = None) -> int:
    items = _all_items(bank_dir)
    if status is not None:
        items = [item for item in items if item.status == status]
    return len(items)


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
            return True
        except FileNotFoundError:
            return False


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
        existing = next((i for i in _all_items(bank_dir) if i.folder == folder), None)
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
    for item in _all_items(bank_dir):
        if item.status != "applying":
            continue
        with _LOCK:
            fresh = get_item(bank_dir, item.id)
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
    queued = [item for item in _all_items(bank_dir) if item.status == "queued"]
    if not queued:
        return None
    return min(queued, key=lambda item: (item.decided_at or item.banked_at, item.id))
