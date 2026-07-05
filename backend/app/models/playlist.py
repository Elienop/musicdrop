"""Playlist API contract (the owned-playlist read/write models).

Chunk 1 covers identity + naming; ``track_ids`` is always empty until Chunk 2
adds track operations, and ``target_plex_users`` until Chunk 7. They live on the
read models from the start so the contract does not churn between chunks.
"""

from __future__ import annotations

from pydantic import BaseModel, field_validator

from app.models.plex import PlexTargetState


class PendingTrack(BaseModel):
    """The remembered identity of a playlist entry that has no library track
    yet (an import that didn't match). ``source`` is the original text the
    entry came from (m3u line / file path / "plex:<playlist>")."""

    artist: str | None = None
    title: str | None = None
    album: str | None = None
    duration_seconds: float | None = None
    source: str = ""


class Playlist(BaseModel):
    """Summary view of a playlist (list rows + create/patch responses)."""

    id: str
    name: str
    description: str
    track_count: int
    target_plex_users: list[str]
    plex: dict[str, PlexTargetState]
    created_at: str
    updated_at: str


class PlaylistTrack(BaseModel):
    """A track as shown in a playlist. ``available`` is False when the beets
    ``item.id`` no longer resolves (deleted from the library); such entries
    still occupy their position and can be removed."""

    id: int
    title: str
    artist: str
    album: str
    duration_seconds: float | None
    available: bool


class PlaylistDetail(Playlist):
    """Single-playlist view with the ordered, resolved tracklist."""

    tracks: list[PlaylistTrack]


class PlaylistCreateRequest(BaseModel):
    name: str
    description: str = ""

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped


class PlaylistUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    target_plex_users: list[str] | None = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped

    @field_validator("target_plex_users")
    @classmethod
    def _clean_targets(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        # "admin" is the owner's own sync key (synced unconditionally) — it is
        # never a *target* user; drop it (and any duplicates) so the fan-out
        # can't sync the admin account twice.
        seen: set[str] = set()
        cleaned: list[str] = []
        for uid in value:
            if uid == "admin" or uid in seen:
                continue
            seen.add(uid)
            cleaned.append(uid)
        return cleaned


# A generous ceiling on the ids accepted in one request — far above any real
# playlist, but it stops a pathological/buggy client from forcing an unbounded
# number of per-id library lookups in one call.
_MAX_TRACK_IDS = 10_000


class PlaylistAddTracksRequest(BaseModel):
    track_ids: list[int]
    position: int | None = None

    @field_validator("track_ids")
    @classmethod
    def _non_empty_and_bounded(cls, value: list[int]) -> list[int]:
        if not value:
            raise ValueError("track_ids must not be empty")
        if len(value) > _MAX_TRACK_IDS:
            raise ValueError(f"track_ids must contain at most {_MAX_TRACK_IDS} items")
        return value


class PlaylistReorderRequest(BaseModel):
    # An empty list is allowed and clears the playlist — reorder is a full
    # replacement ("the tracks are now exactly this ordered list"), and there is
    # no separate clear endpoint.
    track_ids: list[int]

    @field_validator("track_ids")
    @classmethod
    def _bounded(cls, value: list[int]) -> list[int]:
        if len(value) > _MAX_TRACK_IDS:
            raise ValueError(f"track_ids must contain at most {_MAX_TRACK_IDS} items")
        return value
