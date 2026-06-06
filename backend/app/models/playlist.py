"""Playlist API contract (the owned-playlist read/write models).

Chunk 1 covers identity + naming; ``track_ids`` is always empty until Chunk 2
adds track operations, and ``target_plex_users`` until Chunk 7. They live on the
read models from the start so the contract does not churn between chunks.
"""

from __future__ import annotations

from pydantic import BaseModel, field_validator


class Playlist(BaseModel):
    """Summary view of a playlist (list rows + create/patch responses)."""

    id: str
    name: str
    description: str
    track_count: int
    target_plex_users: list[str]
    created_at: str
    updated_at: str


class PlaylistDetail(Playlist):
    """Single-playlist view. Chunk 2 adds resolved ``tracks`` alongside ids."""

    track_ids: list[int]


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

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped
