"""Owned playlist store — one JSON file per playlist under the playlists dir.

MusicDrop owns playlists (beets has no concept of a hand-curated, ordered
playlist). This module is pure filesystem I/O + the stored record shape; it
imports neither beets nor Plex. The ``.m3u8`` export (Chunk 3) and Plex sync
(Chunks 5-7) build on top of this store.

Single-user app: a bool/JSON flip is GIL-atomic and writes go through a
tmp-then-replace (with fsync) recipe, so no lock is needed.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, model_validator

from app.models.playlist import PendingTrack
from app.models.plex import PlexTargetState
from app.playlists.atomic import write_atomic_text

# Playlist ids are ``uuid.uuid4().hex`` — exactly 32 lowercase hex chars. Any
# other value (``..``, an absolute path, a stray slash) is rejected before it
# touches the filesystem, so a hostile ``{playlist_id}`` URL parameter cannot
# escape the playlists dir via path traversal.
_VALID_ID = re.compile(r"\A[0-9a-f]{32}\Z")


def _is_valid_id(playlist_id: str) -> bool:
    return bool(_VALID_ID.match(playlist_id))


class StoredEntry(BaseModel):
    """One ordered playlist slot: a resolved library track OR a pending one."""

    uid: str
    item_id: int | None = None
    pending: PendingTrack | None = None


class StoredPlaylist(BaseModel):
    """On-disk playlist record (``<playlists_dir>/<id>.json``)."""

    id: str
    name: str
    description: str = ""
    track_ids: list[int] = []  # legacy (pre-entries) — migrated on read, always []
    entries: list[StoredEntry] = []
    target_plex_users: list[str] = []
    plex: dict[str, PlexTargetState] = {}
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def _migrate_legacy_track_ids(self) -> StoredPlaylist:
        if self.track_ids and not self.entries:
            self.entries = [
                StoredEntry(uid=uuid.uuid4().hex, item_id=item_id) for item_id in self.track_ids
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
    """Crash-safe write of the JSON record (shared atomic-text recipe)."""
    write_atomic_text(path, record.model_dump_json(indent=2))


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
        return StoredPlaylist.model_validate_json(raw)
    except ValueError:
        return None


def list_playlists(playlists_dir: Path) -> list[StoredPlaylist]:
    """All playlists, sorted by ``created_at`` ascending. Skips unreadable files."""
    if not playlists_dir.exists():
        return []
    records: list[StoredPlaylist] = []
    for child in playlists_dir.glob("*.json"):
        try:
            records.append(StoredPlaylist.model_validate_json(child.read_text(encoding="utf-8")))
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


def add_tracks(
    playlists_dir: Path,
    playlist_id: str,
    *,
    track_ids: list[int],
    position: int | None = None,
) -> StoredPlaylist | None:
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
    record = get_playlist(playlists_dir, playlist_id)
    if record is None:
        return None
    # Whole-map replace (a sync recomputes every target's state). Bookkeeping,
    # NOT a content edit — must not bump updated_at (see set_plex_state).
    record.plex = states
    _write_atomic(_record_path(playlists_dir, playlist_id), record)
    return record


def delete_playlist(playlists_dir: Path, playlist_id: str) -> bool:
    if not _is_valid_id(playlist_id):
        return False
    try:
        _record_path(playlists_dir, playlist_id).unlink()
        return True
    except FileNotFoundError:
        return False
