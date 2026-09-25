"""The bank store — every set-aside album as one row of ``<bank_dir>/bank.db``.

ONE SQLite file (stdlib ``sqlite3``, the module beets' own ``dbcore`` imports).
Each row keeps today's full row JSON beside the few columns the questions need
— status, reason, folder, and two times as integers — so every question the app
asks ("which rows are applying?", "the next queued row", "one page of the
list", "the row for this folder") reads only the rows it answers with. The boot
reads the applying rows and the first queued one, not the whole bank. Pure
database I/O — no beets imports.

CONNECTIONS: one connection per bank file, shared by every thread, and every
use of it — reads included — under the module lock ``_LOCK``. That is beets'
own model with the per-thread connections folded away: beets opens one
connection per thread (``dbcore/db.py:1177-1191``) but then holds ONE lock for
every transaction (``_db_lock``, ``db.py:1129-1138``), so its accesses are
serialized too. One lock around one connection gives the same ordering, a
connection the tests can close, and the read-modify-write transitions below
(decide, the apply runner's compare-and-set claim, re-bank) their atomicity.
``check_same_thread=False`` is what lets the boot's one call on the event loop,
the API's anyio worker threads (``run_in_threadpool``), the apply-runner thread
and the sweep's import worker thread (``upsert_by_folder``) share it — safe
because ``_LOCK`` never lets two threads into it at once.

JOURNAL: SQLite's default (rollback journal, ``synchronous=FULL``), as beets
leaves its ``library.db``. Local disk only — SQLite does not support network
filesystems (https://www.sqlite.org/useovernet.html).

LEGACY ROWS: earlier versions kept one ``<id>.json`` file per row in the same
folder. The first open of a bank whose database is not marked imported
(``PRAGMA user_version`` 0) imports every such file in ONE transaction, one row
at a time, skipping out loud a row it cannot read, validate or store (the rest
still import); a crash mid-import leaves
nothing and the next boot starts over. The files are left untouched — a frozen
backup — and never read again.

SERIALIZATION (lossless, deliberately not the wire path): a stored folder can
carry a LONE SURROGATE — ``folder`` comes from ``os.fsdecode``, so a folder
name with undecodable UTF-8 bytes (e.g. ``b"Bj\\xf6rk"``) is a perfectly legal
Python str (``"Bj\\udcf6rk"``) that the folder on disk actually needs. The store
must round-trip it losslessly (invariant: ``os.fsencode`` on the reloaded str
yields the original on-disk bytes), because the apply step resolves the folder
against the real filesystem. A U+FFFD display-scrub (``app/wire.py``) is the
opposite guarantee — a scrubbed path would point at a non-existent folder and
apply would break — so it belongs ONLY on responses, never in this sink.

Pydantic's Rust serializers reject lone surrogates (``model_dump_json`` raises
``PydanticSerializationError``), so the row JSON is written with Python's stdlib
``json`` instead: ``json.dumps(model_dump(mode="json"), ensure_ascii=True)``
escapes each surrogate as ``\\uXXXX`` (CPython explicitly supports encoding lone
surrogates in text output), the text stays pure ASCII — and ``sqlite3`` binds a
``str`` as strict UTF-8, which a raw surrogate would fail — and reading with
``json.loads`` restores the identical str, which ``model_validate`` accepts.
``ensure_ascii=True`` is REQUIRED (the SINGLE SINK RULE). The folder COLUMN is
``os.fsencode``'s bytes, a BLOB, as beets stores its paths
(``library/fields.py:82``, ``dbcore/types.py:363-372``): as text the same str
raises ``UnicodeEncodeError`` at the bind."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.models.bank import (
    BankDecision,
    BankFailureRecovery,
    BankItem,
    BankItemSummary,
    BankReason,
    BankSource,
    BankStatus,
)
from app.models.import_models import DuplicatePrompt, ParkedAlbum

_VALID_ID = re.compile(r"\A[0-9a-f]{32}\Z")

logger = logging.getLogger(__name__)
# The import's one INFO line must reach ``docker logs`` (tests/test_operator_logging.py).
operator_logger = logging.getLogger("uvicorn.error")

#: The database's file name inside the bank folder.
DB_NAME = "bank.db"

# The Review page's default "needs attention" view: in-flight rows stay
# visible, resolved rows (done/ignored) don't.
ACTIVE_STATUSES: frozenset[str] = frozenset(
    {"needs_review", "queued", "applying", "failed", "stale"}
)

# One lock for every use of every connection (see the module docstring).
_LOCK = threading.Lock()

# Open connections, by resolved database path. Only touched under ``_LOCK``.
_CONNECTIONS: dict[str, sqlite3.Connection] = {}

# ``banked_at`` / ``apply_at`` are microseconds since the epoch, UTC: an ISO
# string mis-orders a whole second (``...:00Z``) after a fractional one
# (``...:00.500000Z``), which pydantic writes for the same field.
# ``apply_at`` is the apply runner's FIFO key: ``decided_at``, else
# ``banked_at`` for a row that never carried one.
_SCHEMA = (
    """CREATE TABLE bank (
        id TEXT PRIMARY KEY,
        folder BLOB NOT NULL,
        status TEXT NOT NULL,
        reason TEXT NOT NULL,
        banked_at INTEGER NOT NULL,
        apply_at INTEGER NOT NULL,
        summary TEXT NOT NULL,
        row TEXT NOT NULL
    )""",
    "CREATE INDEX bank_folder ON bank (folder)",
    "CREATE INDEX bank_status ON bank (status)",
    "CREATE INDEX bank_banked ON bank (banked_at, id)",
)

# One WHERE for every list question: ``:statuses`` is a JSON array of the
# statuses to keep (NULL = any), ``:reason`` the reason to keep (NULL = any).
# Bound, never formatted into the text.
_LIST_WHERE = (
    " WHERE (:statuses IS NULL OR status IN (SELECT value FROM json_each(:statuses)))"
    " AND (:reason IS NULL OR reason = :reason)"
)

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

_SQLITE_MAX_INT = 2**63 - 1

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


def _now() -> datetime:
    return datetime.now(UTC)


def _micros(moment: datetime) -> int:
    """``moment`` as an integer the database orders by real time.

    A NAIVE time (no offset) counts as UTC: every time this app writes carries
    one (``_now``), so a naive one comes only from a hand-made row, and UTC is
    the zone every other row is in. Its stored JSON keeps the value as it was.
    """
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return (aware - _EPOCH) // _MICROSECOND


def _summary_of(item: BankItem) -> BankItemSummary:
    return BankItemSummary(**item.model_dump(exclude=_SUMMARY_EXCLUDE))


def _json_text(payload: object) -> str:
    """Lossless JSON for the database. See the module docstring (SINGLE SINK RULE).

    ``allow_nan=False`` makes the sink FAIL LOUD rather than store a bare
    NaN/Infinity token that no strict JSON parser reads back. The producer
    clamps (``_confidence``) and the legacy import heals the tokens
    (``_parse_legacy_row``); a legacy ``"NaN"`` STRING still reaches here, and
    the import skips that row (``_import_legacy_file``).
    """
    return json.dumps(payload, ensure_ascii=True, allow_nan=False)


def _row_text(item: BankItem) -> str:
    return _json_text(item.model_dump(mode="json"))


def _finite_payload(value: object) -> object:
    """Heal non-finite floats in a parsed legacy row to 0.0, recursively.

    ONE policy for every float in a row — the Optional top-level ``confidence``
    included: a non-finite VALUE becomes 0.0 ("no confidence"), the same clamp
    the producer applies. ``None`` stays ``None`` — healing never invents a
    value. Deliberately not None: a ``null`` in a REQUIRED nested candidate
    float would be a ValidationError, and the import would then skip the row.
    """
    if isinstance(value, dict):
        return {key: _finite_payload(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_finite_payload(child) for child in value]
    if isinstance(value, float) and not math.isfinite(value):
        return 0.0
    return value


def _parse_legacy_row(raw: str) -> BankItem:
    """One legacy ``<id>.json`` file's row.

    A row poisoned with non-finite float TOKENS (written by a pre-clamp
    producer) still imports: ``json.loads`` admits the NaN/Infinity/-Infinity
    tokens and :func:`_finite_payload` clamps them to 0.0 before validation.
    """
    return BankItem.model_validate(_finite_payload(json.loads(raw)))


def _parse_row(raw: str) -> BankItem:
    """A stored row: stdlib ``json.loads`` restores the escaped lone surrogates."""
    return BankItem.model_validate(json.loads(raw))


def _put(conn: sqlite3.Connection, item: BankItem) -> None:
    """Insert or replace ``item``'s row. Callers hold ``_LOCK``."""
    conn.execute(
        "INSERT OR REPLACE INTO bank"
        " (id, folder, status, reason, banked_at, apply_at, summary, row)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item.id,
            os.fsencode(item.folder),
            item.status,
            item.reason,
            _micros(item.banked_at),
            _micros(item.decided_at or item.banked_at),
            _json_text(_summary_of(item).model_dump(mode="json")),
            _row_text(item),
        ),
    )


def _get(conn: sqlite3.Connection, item_id: str) -> BankItem | None:
    """The row ``item_id`` names, or None. Callers hold ``_LOCK``.

    ``_VALID_ID`` first: an id the store never mints is absent without a query,
    and a lone surrogate in one never reaches the UTF-8 bind.
    """
    if not _VALID_ID.match(item_id):
        return None
    found = conn.execute("SELECT row FROM bank WHERE id = ?", (item_id,)).fetchone()
    return None if found is None else _parse_row(found[0])


def _import_legacy_file(conn: sqlite3.Connection, child: Path) -> bool:
    """Import one legacy row file; False (logged) when it cannot be imported.

    ``RecursionError`` is a deeply nested file, which ``json.loads`` refuses
    that way rather than with a ``ValueError``. An id this store never mints is
    refused too: ``_get`` answers None for it, so the row could be listed but
    never opened or deleted. So is a row that validates but that ``_put``
    cannot store: a folder str ``os.fsdecode`` could not have produced, or a
    ``"NaN"`` STRING that pydantic's lax float parse turns into nan after the
    healing ran. A ``sqlite3.Error`` is not a row's fault and still aborts the
    import. The parsed row dies when this returns, so the caller's loop never
    holds two.
    """
    try:
        item = _parse_legacy_row(child.read_text(encoding="utf-8"))
        if not _VALID_ID.match(item.id):
            raise ValueError(f"{item.id!r} is not a bank row id")
        _put(conn, item)
    except (OSError, ValueError, RecursionError) as exc:
        # Loud skip: a silently vanished row is indistinguishable from a
        # deleted one in the UI — name the file and the reason. ``%r`` keeps
        # a newline in the file name or pydantic's multi-line text on ONE line.
        logger.warning("Skipping unreadable bank row %r: %r", child.name, str(exc))
        return False
    return True


def _import_legacy_rows(conn: sqlite3.Connection, bank_dir: Path) -> None:
    """Create the table and import every ``*.json`` row, all in ONE transaction.

    One full row alive at a time: resolved rows stay as history, and Python
    keeps the memory a peak allocated, so a list of every parsed row would stay
    pinned for the life of the process.
    """
    started = time.monotonic()
    imported = skipped = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in _SCHEMA:
            conn.execute(statement)
        # ``iterdir``, not ``glob``: pathlib's glob swallows a failed listing
        # (``_WildcardSelector``, ``except OSError: pass``), which would mark an
        # import of zero rows done and never read the files again.
        for child in (c for c in bank_dir.iterdir() if c.suffix == ".json"):
            if _import_legacy_file(conn, child):
                imported += 1
            else:
                skipped += 1
        # The mark, in the same transaction: imported, and never read again.
        conn.execute("PRAGMA user_version = 1")
        conn.execute("COMMIT")
    except BaseException:
        # A failure SQLite already rolled back leaves no transaction to end.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    operator_logger.info(
        "bank: imported %d rows (%d skipped) in %.1f s",
        imported,
        skipped,
        time.monotonic() - started,
    )


def _open(bank_dir: Path) -> sqlite3.Connection:
    bank_dir.mkdir(parents=True, exist_ok=True)
    # ``isolation_level=None``: no implicit transactions — each statement
    # commits by itself, and the import opens its one transaction explicitly.
    conn = sqlite3.connect(bank_dir / DB_NAME, isolation_level=None, check_same_thread=False)
    try:
        (version,) = conn.execute("PRAGMA user_version").fetchone()
        if version == 0:
            _import_legacy_rows(conn, bank_dir)
    except BaseException:
        conn.close()
        raise
    return conn


def _conn(bank_dir: Path) -> sqlite3.Connection:
    """The open connection for ``bank_dir``, opening (and importing) on first use.

    Callers hold ``_LOCK``.
    """
    key = str(Path(bank_dir).resolve() / DB_NAME)
    conn = _CONNECTIONS.get(key)
    if conn is None:
        conn = _open(Path(bank_dir))
        _CONNECTIONS[key] = conn
    return conn


def close_connections() -> None:
    """Close every open bank database; the next call reopens on demand.

    For tests (each uses its own bank folder) and to stand in for a restart.
    """
    with _LOCK:
        while _CONNECTIONS:
            _key, conn = _CONNECTIONS.popitem()
            conn.close()


def _new_item(
    *,
    folder: str,
    source: BankSource,
    reason: BankReason,
    fingerprint: str,
    artist: str | None,
    album: str | None,
    recommendation: str | None,
    confidence: float | None,
    parked: ParkedAlbum | None,
    duplicate: DuplicatePrompt | None,
) -> BankItem:
    return BankItem(
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
    item = _new_item(
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
    with _LOCK:
        _put(_conn(bank_dir), item)
    return item


def get_item(bank_dir: Path, item_id: str) -> BankItem | None:
    with _LOCK:
        return _get(_conn(bank_dir), item_id)


def list_page(
    bank_dir: Path,
    *,
    status: BankStatus | None = None,
    active_only: bool = False,
    reason: BankReason | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[BankItemSummary], int, int]:
    """-> (page, total_filtered, total_all).

    ``total_all`` counts rows of ANY status (drives the Review page's section
    visibility so resolved history stays reachable when the active view empties
    out). A specific ``status`` wins over ``active_only`` (the router never
    sends both); ``reason`` ANDs with whichever status narrowing is in effect.
    """
    if status is not None:
        statuses: str | None = json.dumps([status])
    elif active_only:
        statuses = json.dumps(sorted(ACTIVE_STATUSES))
    else:
        statuses = None
    where = {"statuses": statuses, "reason": reason}
    # SQLite binds 64-bit integers only (a larger one is ``OverflowError``);
    # an offset past that skips every row anyway, so answer the empty page.
    offset = min(offset, _SQLITE_MAX_INT)
    with _LOCK:
        conn = _conn(bank_dir)
        (total_all,) = conn.execute("SELECT count(*) FROM bank").fetchone()
        (total,) = conn.execute("SELECT count(*) FROM bank" + _LIST_WHERE, where).fetchone()
        # Display order: newest banked first (the Review page reads top-down);
        # id breaks timestamp ties. The APPLY order is next_queued's
        # oldest-decided FIFO — a queue, not this display sort.
        rows = conn.execute(
            "SELECT summary FROM bank" + _LIST_WHERE + " ORDER BY banked_at DESC, id DESC"
            " LIMIT :limit OFFSET :offset",
            {**where, "limit": limit, "offset": offset},
        ).fetchall()
    page = [BankItemSummary.model_validate(json.loads(summary)) for (summary,) in rows]
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
        conn = _conn(bank_dir)
        item = _get(conn, item_id)
        if item is None:
            return None
        if item.status not in _DECIDABLE:
            raise InvalidTransitionError(
                f"row is {item.status}; decisions need one of {sorted(_DECIDABLE)}"
            )
        item.decided = decision
        item.decided_at = _now()
        item.error = None
        # Cleared WITH the error it belongs to, here and in every other reset
        # (``upsert_by_folder``, ``rescan_item``, ``reconcile_interrupted``):
        # ``set_status`` rewriting it on each of its own transitions makes it
        # true only while the runner owns the row, which is not the same as the
        # promise ``set_status``'s docstring makes.
        item.error_recovery = "decide_again"
        if decision.action == "ignore":
            item.status = "ignored"
            item.resolved_at = _now()
        else:
            item.status = "queued"
        _put(conn, item)
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
        conn = _conn(bank_dir)
        item = _get(conn, item_id)
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
        _put(conn, item)
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
    (the up-front collision check re-flags it if still real).

    Two rows RESET to ``needs_review`` with ``decided``/``error``/
    ``error_recovery`` cleared (the re-bank precedent), because reaching here
    disproves what their banner says: a ``stale`` row (the folder changed —
    this rescan just re-read it and blessed a fresh fingerprint) and a
    ``fix_folder`` failure (the folder would not answer — the route
    fingerprinted it and read its audio files before calling this, or it 409ed
    instead). A ``fix_folder`` row refused for WHERE its folder is resets too,
    though nothing was disproved; its next decision fails the same way, with
    the same sentence (harmless, BACKLOG). Every other row KEEPS its status: a
    rescan disproves nothing else. It emphatically does not un-import the second
    copy a ``remove_duplicate`` row is waiting on, and an ordinary
    ``decide_again`` failure's banner stays true.
    """
    with _LOCK:
        conn = _conn(bank_dir)
        item = _get(conn, item_id)
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
        folder_answered = item.status == "failed" and item.error_recovery == "fix_folder"
        if item.status == "stale" or folder_answered:
            item.status = "needs_review"
            item.decided = None
            item.error = None
            item.error_recovery = "decide_again"
        _put(conn, item)
        return item


def set_status(
    bank_dir: Path,
    item_id: str,
    status: BankStatus,
    *,
    error: str | None = None,
    album_id: int | None = None,
    error_recovery: BankFailureRecovery = "decide_again",
    expected: BankStatus | None = None,
) -> BankItem | None:
    """Bookkeeping transition (chunk 4's apply runner + reconciliation use it).

    ``album_id`` is only ever supplied with ``done`` (the apply landed an
    album); None leaves the field untouched so failure paths never erase a
    previously recorded id. ``error_recovery`` is the opposite: it is written on
    EVERY transition here and defaults to ``decide_again``. That alone does not
    stop a row carrying a recovery into its next decision — the writers that
    reset a row without going through here (``decide_item``,
    ``upsert_by_folder``, ``rescan_item``, ``reconcile_interrupted``) clear it
    beside the error, which is what makes it true.
    ``expected`` makes the write a compare-and-set:
    when given and the row's current status differs, return None WITHOUT
    writing — the apply runner's queued->applying claim uses it so a row
    re-banked under a stale reference (reset to needs_review, decided=None)
    is never blind-overwritten into a validator-rejected state (row loss).
    """
    with _LOCK:
        conn = _conn(bank_dir)
        item = _get(conn, item_id)
        if item is None:
            return None
        if expected is not None and item.status != expected:
            return None
        item.status = status
        item.error = error
        item.error_recovery = error_recovery
        if album_id is not None:
            item.album_id = album_id
        if status in ("done", "failed", "ignored"):
            item.resolved_at = _now()
        _put(conn, item)
        return item


def refresh_duplicate(bank_dir: Path, item_id: str, prompt: DuplicatePrompt) -> BankItem | None:
    """Replace the row's stored collision with the one an apply just saw.

    The one writer that REPLACES a prompt rather than clearing it
    (``rescan_item`` sets it to None). For the stale-consent refusal, whose cause
    is the stored prompt no longer matching the library: re-deciding on the same
    payload refuses again, forever (measured). Written inside the ``applying``
    window, so the row the user re-opens shows what is in the library NOW.

    Status is not touched: the caller flips it (``failed``) immediately after,
    and an ``applying`` row is neither decidable nor rescannable, so no second
    writer sees the half-updated shape.
    """
    with _LOCK:
        conn = _conn(bank_dir)
        item = _get(conn, item_id)
        if item is None:
            return None
        item.duplicate = prompt
        _put(conn, item)
        return item


def _delete_unless_applying(conn: sqlite3.Connection, item_id: str) -> bool:
    """Remove ``item_id``'s row; False when there is none. Callers hold ``_LOCK``."""
    item = _get(conn, item_id)
    if item is None:
        return False
    if item.status == "applying":
        raise InvalidTransitionError("row is applying; wait for the apply to finish")
    conn.execute("DELETE FROM bank WHERE id = ?", (item_id,))
    return True


def delete_item(bank_dir: Path, item_id: str) -> bool:
    """Delete the row. True iff a row was removed; a missing id reports
    ``False`` (API -> 404), an ``applying`` row raises (API -> 409)."""
    with _LOCK:
        return _delete_unless_applying(_conn(bank_dir), item_id)


def bulk_ignore(bank_dir: Path, ids: list[str]) -> int:
    """Ignore every listed row still in ``needs_review``; skip the rest."""
    flipped = 0
    for item_id in ids:
        with _LOCK:
            conn = _conn(bank_dir)
            item = _get(conn, item_id)
            if item is None or item.status != "needs_review":
                continue
            item.status = "ignored"
            item.resolved_at = _now()
            _put(conn, item)
            flipped += 1
    return flipped


def bulk_delete(bank_dir: Path, ids: list[str]) -> int:
    """Delete every listed deletable row; return how many were actually removed.

    Skip-and-report posture: a missing id and an ``applying`` row are skipped,
    so no input can abort the batch mid-way; the count reports what landed.
    """
    deleted = 0
    for item_id in ids:
        with _LOCK:
            try:
                if _delete_unless_applying(_conn(bank_dir), item_id):
                    deleted += 1
            except InvalidTransitionError:
                continue  # an applying row can't be deleted
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
    fresh decision).

    Rows from before the dedupe can share a folder; the latest banked one owns
    it (``banked_at``, then id), the order the old store's boot used."""
    with _LOCK:
        conn = _conn(bank_dir)
        found = conn.execute(
            "SELECT row FROM bank WHERE folder = ? ORDER BY banked_at DESC, id DESC LIMIT 1",
            (os.fsencode(folder),),
        ).fetchone()
        existing = None if found is None else _parse_row(found[0])
        if existing is None:
            item = _new_item(
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
        elif existing.fingerprint == fingerprint:
            existing.banked_at = _now()
            item = existing
        else:
            item = existing.model_copy(
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
                    "error_recovery": "decide_again",
                    "banked_at": _now(),
                    "decided_at": None,
                    "resolved_at": None,
                }
            )
        _put(conn, item)
        return item


def reconcile_interrupted(bank_dir: Path) -> int:
    """Startup pass: rows stuck in ``applying`` (process died mid-apply) revert
    to ``needs_review`` with a note. Never blind-requeues (spec §5/§8).

    Reads the applying rows alone — at most one in practice (one drain claims
    one row at a time) — whatever the size of the bank."""
    with _LOCK:
        conn = _conn(bank_dir)
        applying = conn.execute("SELECT row FROM bank WHERE status = 'applying'").fetchall()
        for (raw,) in applying:
            item = _parse_row(raw)
            item.status = "needs_review"
            item.error = "apply interrupted by a restart - decide again"
            item.error_recovery = "decide_again"
            _put(conn, item)
    return len(applying)


def next_queued(bank_dir: Path) -> BankItem | None:
    """The apply runner's FIFO head: the oldest-decided ``queued`` row.

    Ordered by ``decided_at`` (the spec's apply order — decision time, not
    banking time), id as the tie-break. ``decided_at`` is always set on a
    queued row (decide_item stamps it); ``banked_at`` is a defensive fallback
    for a hand-made row (``apply_at`` in the schema).
    """
    with _LOCK:
        found = (
            _conn(bank_dir)
            .execute("SELECT row FROM bank WHERE status = 'queued' ORDER BY apply_at, id LIMIT 1")
            .fetchone()
        )
    return None if found is None else _parse_row(found[0])
