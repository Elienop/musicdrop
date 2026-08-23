"""Owned playlist store — one JSON file per playlist under the playlists dir.

MusicDrop owns playlists (beets has no concept of a hand-curated, ordered
playlist). This module is pure filesystem I/O + the stored record shape; it
imports neither beets nor Plex. The ``.m3u8`` export (Chunk 3) and Plex sync
(Chunks 5-7) build on top of this store.

The tmp-then-replace (with fsync) write recipe gives crash-safety — a reader
never sees a torn file. Concurrency is a separate concern: the API runs mutators
on parallel worker threads, and every mutator is a read-modify-write
(``get_playlist`` -> mutate -> ``_write_atomic``). Without serialization two
overlapping mutations both read version V and both write, so the later write
silently drops the earlier one's change. A process-wide ``_LOCK`` therefore
serializes the read-modify-write body of every mutator; pure reads
(``get_playlist``/``list_playlists``) stay lock-free (the atomic replace means a
read never sees a half-written file).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator

from app.models.playlist import PendingTrack
from app.models.plex import PlexTargetState
from app.playlists.atomic import write_atomic_text

# Playlist ids are ``uuid.uuid4().hex`` — exactly 32 lowercase hex chars. Any
# other value (``..``, an absolute path, a stray slash) is rejected before it
# touches the filesystem, so a hostile ``{playlist_id}`` URL parameter cannot
# escape the playlists dir via path traversal.
_VALID_ID = re.compile(r"\A[0-9a-f]{32}\Z")

# Serializes every mutator's read-modify-write so concurrent API worker threads
# can't drop a mutation (last-writer-wins). Pure reads don't take it — the
# atomic replace means a read never observes a half-written file.
_LOCK = threading.Lock()


def _is_valid_id(playlist_id: str) -> bool:
    return bool(_VALID_ID.match(playlist_id))


class StoredEntry(BaseModel):
    """One ordered playlist slot: a resolved library track OR a pending one."""

    uid: str
    item_id: int | None = None
    pending: PendingTrack | None = None


class ArtworkInfo(BaseModel):
    """Marker for a playlist's cover art file (the bytes live on disk, not here).

    ``hash`` is the leading 16 hex chars of the sha256 of the stored bytes — a
    cheap change-detection tag (e.g. for ETags / "did the art change since last
    sync") without re-reading the file.
    """

    format: Literal["jpg", "png"]
    hash: str


class StoredPlaylist(BaseModel):
    """On-disk playlist record (``<playlists_dir>/<id>.json``)."""

    id: str
    name: str
    description: str = ""
    track_ids: list[int] = []  # legacy (pre-entries) — migrated on read, always []
    entries: list[StoredEntry] = []
    target_plex_users: list[str] = []
    plex: dict[str, PlexTargetState] = {}
    artwork: ArtworkInfo | None = None
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def _migrate_legacy_track_ids(self) -> StoredPlaylist:
        # Migrate the pre-entries ``track_ids`` shape to uid-keyed entries.
        #
        # The uids MUST be STABLE across reads: ``get_playlist`` never writes
        # back, so a legacy record is re-migrated on every read. Random uuids
        # would mint DIFFERENT uids each time, and a uid captured from one read
        # (GET detail) would 404 on the next (DELETE/reorder/resolve re-read the
        # file). Deterministic ``legacy-<index>-<item_id>`` uids fix that: the
        # enumerate index guarantees uniqueness within the record (duplicate
        # item ids included), and the ``legacy-`` prefix can't collide with the
        # uuid4-hex uids new entries carry. These uids persist naturally on the
        # first real mutation — harmless, since uids are opaque handles.
        if self.track_ids and not self.entries:
            self.entries = [
                StoredEntry(uid=f"legacy-{index}-{item_id}", item_id=item_id)
                for index, item_id in enumerate(self.track_ids)
            ]
        self.track_ids = []
        return self

    @property
    def resolved_item_ids(self) -> list[int]:
        """Ordered item ids of the RESOLVED entries — what export/sync consume."""
        return [e.item_id for e in self.entries if e.item_id is not None]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _record_path(playlists_dir: Path, playlist_id: str) -> Path:
    return playlists_dir / f"{playlist_id}.json"


def _write_atomic(path: Path, record: StoredPlaylist) -> None:
    """Crash-safe write of the JSON record (shared atomic-text recipe).

    Serialized with stdlib ``json`` (not pydantic's Rust ``model_dump_json``): a
    name or pending-track field can carry a LONE SURROGATE — a client delivers one
    with a ``"\\udce9"`` JSON escape without a single non-UTF-8 byte — and the
    Rust serializer rejects those with ``PydanticSerializationError`` (an unhandled
    500 on every mutation). ``json.dumps`` escapes each surrogate as ``\\uXXXX``
    (lossless) and, with ``ensure_ascii=True``, keeps the output pure ASCII so the
    strict-UTF-8 ``write_atomic_text`` sink never sees an unencodable byte. The
    store stays LOSSLESS (exact code points on disk); the U+FFFD degradation
    belongs on the WIRE only (``app/wire.py``), never here. Mirrors
    ``app/bank/store.py``'s SINGLE SINK RULE.
    """
    text = json.dumps(record.model_dump(mode="json"), ensure_ascii=True, indent=2)
    write_atomic_text(path, text)


def artwork_path(playlists_dir: Path, playlist_id: str, format: str) -> Path:
    """On-disk path of a playlist's cover art (``artwork/<id>.<format>``)."""
    return playlists_dir / "artwork" / f"{playlist_id}.{format}"


def _write_artwork_atomic(path: Path, data: bytes) -> None:
    """Crash-safe write of the raw art bytes — the binary sibling of the shared
    ``write_atomic_text`` recipe (tmp -> fsync -> chmod -> replace -> parent
    fsync), owner-only ``0o600`` to match the store's atomic-write posture."""
    mode = 0o600
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    try:
        # Create the tempfile with the final mode up front (os.open honours
        # umask, so the following chmod pins the exact bits).
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def create_playlist(
    playlists_dir: Path,
    *,
    name: str,
    description: str = "",
    entries: list[StoredEntry] | None = None,
) -> StoredPlaylist:
    now = _now()
    record = StoredPlaylist(
        id=uuid.uuid4().hex,
        name=name,
        description=description,
        entries=list(entries or []),
        target_plex_users=[],
        created_at=now,
        updated_at=now,
    )
    with _LOCK:
        _write_atomic(_record_path(playlists_dir, record.id), record)
    return record


def get_playlist(playlists_dir: Path, playlist_id: str) -> StoredPlaylist | None:
    if not _is_valid_id(playlist_id):
        return None
    path = _record_path(playlists_dir, playlist_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        # ``json.loads`` -> ``model_validate`` (not the Rust ``model_validate_json``):
        # the former restores the identical str INCLUDING lone surrogates the new
        # sink writes as ``\\uXXXX``, and ``model_validate`` accepts it where the
        # Rust JSON parser does not. ``json.JSONDecodeError`` and pydantic's
        # ``ValidationError`` are both ``ValueError`` subclasses, so the corrupt-file
        # posture (return None) is unchanged.
        return StoredPlaylist.model_validate(json.loads(raw))
    except ValueError:
        return None


def list_playlists(playlists_dir: Path) -> list[StoredPlaylist]:
    """All playlists, sorted by ``created_at`` ascending. Skips unreadable files."""
    if not playlists_dir.exists():
        return []
    records: list[StoredPlaylist] = []
    for child in playlists_dir.glob("*.json"):
        # ``json.loads`` -> ``model_validate`` (see ``get_playlist``); a corrupt
        # file (``json.JSONDecodeError`` / pydantic ``ValidationError``, both
        # ``ValueError`` subclasses) or unreadable one (``OSError``) is skipped.
        try:
            records.append(
                StoredPlaylist.model_validate(json.loads(child.read_text(encoding="utf-8")))
            )
        except (OSError, ValueError):
            continue
    records.sort(key=lambda record: record.created_at)
    return records


def update_playlist(
    playlists_dir: Path,
    playlist_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    target_plex_users: list[str] | None = None,
) -> StoredPlaylist | None:
    with _LOCK:
        record = get_playlist(playlists_dir, playlist_id)
        if record is None:
            return None
        if name is not None:
            record.name = name
        if description is not None:
            record.description = description
        if target_plex_users is not None:
            record.target_plex_users = target_plex_users
        record.updated_at = _now()
        _write_atomic(_record_path(playlists_dir, playlist_id), record)
        return record


def set_artwork(
    playlists_dir: Path,
    playlist_id: str,
    data: bytes,
    format: Literal["jpg", "png"],
) -> StoredPlaylist | None:
    """Store cover art bytes for a playlist and record the marker on the record.

    Adding/replacing art is a real content edit, so it bumps ``updated_at`` (the
    editor's Plex-staleness signal).
    """
    with _LOCK:
        record = get_playlist(playlists_dir, playlist_id)
        if record is None:
            return None
        _write_artwork_atomic(artwork_path(playlists_dir, playlist_id, format), data)
        # Drop the other-format leftover: a stale ``.jpg`` sitting next to a live
        # ``.png`` would be re-served if the format ever flipped back to jpg.
        for other in ("jpg", "png"):
            if other != format:
                artwork_path(playlists_dir, playlist_id, other).unlink(missing_ok=True)
        record.artwork = ArtworkInfo(format=format, hash=hashlib.sha256(data).hexdigest()[:16])
        record.updated_at = _now()
        _write_atomic(_record_path(playlists_dir, playlist_id), record)
        return record


def delete_artwork(playlists_dir: Path, playlist_id: str) -> StoredPlaylist | None:
    """Remove a playlist's cover art (both formats) and clear the marker.

    Idempotent: returns the record even when there was no artwork (a missing
    file is fine), and still bumps ``updated_at`` — clearing art is an edit.
    """
    with _LOCK:
        record = get_playlist(playlists_dir, playlist_id)
        if record is None:
            return None
        for fmt in ("jpg", "png"):
            artwork_path(playlists_dir, playlist_id, fmt).unlink(missing_ok=True)
        record.artwork = None
        record.updated_at = _now()
        _write_atomic(_record_path(playlists_dir, playlist_id), record)
        return record


def add_tracks(
    playlists_dir: Path,
    playlist_id: str,
    *,
    track_ids: list[int],
    position: int | None = None,
) -> StoredPlaylist | None:
    with _LOCK:
        record = get_playlist(playlists_dir, playlist_id)
        if record is None:
            return None
        new_entries = [StoredEntry(uid=uuid.uuid4().hex, item_id=item_id) for item_id in track_ids]
        if position is None:
            record.entries = [*record.entries, *new_entries]
        else:
            index = max(0, min(position, len(record.entries)))
            record.entries = [*record.entries[:index], *new_entries, *record.entries[index:]]
        record.updated_at = _now()
        _write_atomic(_record_path(playlists_dir, playlist_id), record)
        return record


def remove_entry(playlists_dir: Path, playlist_id: str, uid: str) -> StoredPlaylist | None:
    with _LOCK:
        record = get_playlist(playlists_dir, playlist_id)
        if record is None:
            return None
        record.entries = [e for e in record.entries if e.uid != uid]
        record.updated_at = _now()
        _write_atomic(_record_path(playlists_dir, playlist_id), record)
        return record


def set_entry_order(
    playlists_dir: Path, playlist_id: str, *, uids: list[str]
) -> StoredPlaylist | None:
    """Full replacement: keep exactly ``uids`` in this order (a subset drops
    the rest; empty clears). The API validates the uids BEFORE calling."""
    with _LOCK:
        record = get_playlist(playlists_dir, playlist_id)
        if record is None:
            return None
        by_uid = {e.uid: e for e in record.entries}
        record.entries = [by_uid[uid] for uid in uids if uid in by_uid]
        record.updated_at = _now()
        _write_atomic(_record_path(playlists_dir, playlist_id), record)
        return record


def resolve_entry(
    playlists_dir: Path, playlist_id: str, uid: str, *, item_id: int
) -> StoredPlaylist | None:
    """Point the entry at a library track (clears pending; also re-points an
    already-resolved entry in place — 'replace track, keep position')."""
    with _LOCK:
        record = get_playlist(playlists_dir, playlist_id)
        if record is None:
            return None
        for entry in record.entries:
            if entry.uid == uid:
                entry.item_id = item_id
                entry.pending = None
                break
        else:
            return None
        record.updated_at = _now()
        _write_atomic(_record_path(playlists_dir, playlist_id), record)
        return record


def set_plex_state(
    playlists_dir: Path, playlist_id: str, target: str, state: PlexTargetState
) -> StoredPlaylist | None:
    with _LOCK:
        record = get_playlist(playlists_dir, playlist_id)
        if record is None:
            return None
        # Recording a Plex sync result is bookkeeping, NOT a content edit, so it
        # must NOT bump ``updated_at`` — otherwise a freshly-synced playlist would
        # have ``updated_at > synced_at`` and the editor would wrongly read
        # "out of date" the instant after a successful sync.
        record.plex[target] = state
        _write_atomic(_record_path(playlists_dir, playlist_id), record)
        return record


def replace_plex_states(
    playlists_dir: Path, playlist_id: str, states: dict[str, PlexTargetState]
) -> StoredPlaylist | None:
    with _LOCK:
        record = get_playlist(playlists_dir, playlist_id)
        if record is None:
            return None
        # Whole-map replace (a sync recomputes every target's state). Bookkeeping,
        # NOT a content edit — must not bump updated_at (see set_plex_state).
        record.plex = states
        _write_atomic(_record_path(playlists_dir, playlist_id), record)
        return record


def _delete_record_files(playlists_dir: Path, playlist_id: str) -> bool:
    """Unlink a playlist's record (and any art files). True iff it existed.

    PRECONDITION: the caller already holds ``_LOCK`` and has established that
    ``playlist_id`` is a valid id. ``_LOCK`` is a plain, NON-REENTRANT
    ``threading.Lock``, so a lock-holding caller (``merge_playlists``) must come
    here rather than to ``delete_playlist``, which takes the lock itself and
    would deadlock.
    """
    try:
        _record_path(playlists_dir, playlist_id).unlink()
    except FileNotFoundError:
        return False
    # Sweep any art files too - no record survives to point at them.
    for fmt in ("jpg", "png"):
        artwork_path(playlists_dir, playlist_id, fmt).unlink(missing_ok=True)
    return True


def delete_playlist(playlists_dir: Path, playlist_id: str) -> bool:
    if not _is_valid_id(playlist_id):
        return False
    with _LOCK:
        return _delete_record_files(playlists_dir, playlist_id)


@dataclass(frozen=True)
class MergeOutcome:
    """What one merge did. A dataclass, not a Pydantic model, because it never
    crosses the wire - the router builds the response model from it."""

    playlist: StoredPlaylist
    added: int
    skipped_duplicates: int
    source_deleted: bool


def merge_playlists(
    playlists_dir: Path,
    target_id: str,
    source_id: str,
    *,
    delete_source: bool,
) -> MergeOutcome | None:
    """Append ``source_id``'s rows to ``target_id``; optionally delete the source.

    The FIRST two-record operation in this module - every other mutator touches
    one record. Both reads, the target write and the optional source removal
    happen inside ONE ``_LOCK`` acquisition, so nothing can interleave against
    either record mid-merge. ``_LOCK`` is NON-REENTRANT: ``get_playlist`` is
    lock-free so it is safe to call here, and the source removal goes through
    ``_delete_record_files`` rather than ``delete_playlist``, which would
    deadlock.

    Rules (see docs/superpowers/specs/2026-08-16-playlist-merge-design.md):

    - Duplicates are decided on ``item_id`` ONLY, against a SNAPSHOT of the
      target's ids taken before anything is copied. The snapshot does not grow
      while copying, so a source listing one track twice brings BOTH rows
      across. Merge must never cost a row.
    - This store has no beets access, so it cannot tell a resolved entry from
      one whose library track has vanished; both are entries carrying an
      ``item_id`` and both are subject to that id check.
    - A PENDING row is copied VERBATIM and is NEVER text-matched against the
      target. It has no library track behind it, so any dedupe would be guessing
      on artist/title text - "Last Christmas" by two artists is two recordings -
      and dropping a row on a fuzzy match is the one way merge could quietly
      lose a song.
    - Every copied row gets a FRESH ``uuid4().hex`` uid. Uids are per-playlist
      handles: reusing the source's would break "duplicate tracks are
      individually addressable" and could collide with ``legacy-<i>-<id>``.
    - The target keeps its OWN ``artwork`` and ``target_plex_users``. Both are
      properties of the playlist you are keeping, not of its contents.
    - ``plex`` state is untouched; only ``updated_at`` bumps, so the editor's
      existing "updated_at > synced_at => out of date" rule asks for a re-sync
      instead of merge fanning out to every target account on its own.

    Returns ``None`` when either record is missing or its id is invalid, so the
    caller owns the 404.

    PRECONDITION: ``target_id != source_id``. The caller refuses a self-merge
    (409) before getting here; passing one id twice would append a record to
    itself.
    """
    with _LOCK:
        target = get_playlist(playlists_dir, target_id)
        source = get_playlist(playlists_dir, source_id)
        if target is None or source is None:
            return None
        already = set(target.resolved_item_ids)
        copied: list[StoredEntry] = []
        skipped = 0
        for entry in source.entries:
            if entry.item_id is not None and entry.item_id in already:
                skipped += 1
                continue
            copied.append(
                StoredEntry(
                    uid=uuid.uuid4().hex,
                    item_id=entry.item_id,
                    pending=entry.pending.model_copy() if entry.pending is not None else None,
                )
            )
        target.entries = [*target.entries, *copied]
        target.updated_at = _now()
        _write_atomic(_record_path(playlists_dir, target_id), target)
        source_deleted = delete_source and _delete_record_files(playlists_dir, source_id)
        return MergeOutcome(
            playlist=target,
            added=len(copied),
            skipped_duplicates=skipped,
            source_deleted=source_deleted,
        )
