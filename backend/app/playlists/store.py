"""Owned playlist store — one JSON file per playlist under the playlists dir.

MusicDrop owns playlists (beets has no concept of a hand-curated, ordered
playlist). This module is pure filesystem I/O + the stored record shape; it
imports neither beets nor Plex. The ``.m3u8`` export (Chunk 3) and Plex sync
(Chunks 5-7) build on top of this store.

Single-user app: a bool/JSON flip is GIL-atomic and writes go through a
tmp-then-replace (with fsync) recipe, so no lock is needed.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel


class StoredPlaylist(BaseModel):
    """On-disk playlist record (``<playlists_dir>/<id>.json``)."""

    id: str
    name: str
    description: str = ""
    track_ids: list[int] = []
    target_plex_users: list[str] = []
    created_at: str
    updated_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _record_path(playlists_dir: Path, playlist_id: str) -> Path:
    return playlists_dir / f"{playlist_id}.json"


def _write_atomic(path: Path, record: StoredPlaylist) -> None:
    """Crash-safe write: tmp in same dir -> fsync -> os.replace -> dir fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(record.model_dump_json(indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
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


def create_playlist(playlists_dir: Path, *, name: str, description: str = "") -> StoredPlaylist:
    now = _now()
    record = StoredPlaylist(
        id=uuid.uuid4().hex,
        name=name,
        description=description,
        track_ids=[],
        target_plex_users=[],
        created_at=now,
        updated_at=now,
    )
    _write_atomic(_record_path(playlists_dir, record.id), record)
    return record


def get_playlist(playlists_dir: Path, playlist_id: str) -> StoredPlaylist | None:
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
) -> StoredPlaylist | None:
    record = get_playlist(playlists_dir, playlist_id)
    if record is None:
        return None
    if name is not None:
        record.name = name
    if description is not None:
        record.description = description
    record.updated_at = _now()
    _write_atomic(_record_path(playlists_dir, playlist_id), record)
    return record


def delete_playlist(playlists_dir: Path, playlist_id: str) -> bool:
    try:
        _record_path(playlists_dir, playlist_id).unlink()
        return True
    except FileNotFoundError:
        return False
