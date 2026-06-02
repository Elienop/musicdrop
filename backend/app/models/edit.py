"""Pydantic contract for the album/track tag-edit feature.

The editable surface deliberately excludes beets' computed/protected fields
(id, path, mtime, bitrate, format, length, ...) — they are simply absent here,
so they can never be set. Types are validated up front (e.g. ``year: int``)
because beets silently coerces bad values to null. Field names mirror the read
models (``Album``/``Track``); ``app/beets/edit.py`` maps them to beets' names.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.models.album import AlbumDetail


class AlbumFieldEdits(BaseModel):
    """Album-header fields to change; only set (non-None) keys are applied.

    These propagate to every track (beets ``inherit``). Clearing a field (set
    to null) is out of scope, so None means "leave unchanged".
    """

    album_artist: str | None = None
    title: str | None = None
    year: int | None = None
    genre: str | None = None


class TrackFieldEdits(BaseModel):
    """Per-track fields to change, keyed by the track's beets item id."""

    item_id: int
    title: str | None = None
    track: int | None = None
    artist: str | None = None


class AlbumEditRequest(BaseModel):
    """One edit request: optional album-header changes + per-track changes."""

    album: AlbumFieldEdits | None = None
    tracks: list[TrackFieldEdits] = []


class AlbumDiffSide(BaseModel):
    """One side (before/after) of the album-header diff."""

    album_artist: str | None = None
    title: str | None = None
    year: int | None = None
    genre: str | None = None


class EditTrackChange(BaseModel):
    """One track row's before/after, for the preview diff (changed rows only)."""

    item_id: int
    title_before: str | None = None
    title_after: str | None = None
    track_before: int | None = None
    track_after: int | None = None
    artist_before: str | None = None
    artist_after: str | None = None


class TrackPathChange(BaseModel):
    """A track whose file would relocate when move is enabled."""

    item_id: int
    track: int | None = None
    old_path: str
    new_path: str


class AlbumEditPreview(BaseModel):
    """The preview of a pending edit: field diff + move plan. Persists nothing."""

    changed_fields: list[str]
    album_before: AlbumDiffSide
    album_after: AlbumDiffSide
    tracks: list[EditTrackChange]
    move_enabled: bool
    move_plan: list[TrackPathChange]


class ItemWriteResult(BaseModel):
    """The per-track outcome of an apply (replaces beets' silent all-or-nothing)."""

    item_id: int
    track: int | None = None
    title: str | None = None
    written: bool
    moved: bool
    error: str | None = None


class AlbumEditResult(BaseModel):
    """The result of an apply: the refreshed album + per-track outcomes."""

    album: AlbumDetail
    items: list[ItemWriteResult]
    write_failures: int
    move_failures: int
