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
