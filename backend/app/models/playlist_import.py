"""Playlist import contract: parsed sources, match previews, and the commit.

``SourceEntry``/``ParsedPlaylist`` are internal plumbing (parser/puller ->
matcher) — they never appear in an endpoint signature, so they stay out of
the OpenAPI schema. The rest is the preview/commit API contract.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, model_validator

from app.models.playlist import PendingTrack, Playlist


class SourceEntry(BaseModel):
    """One entry as read from a source playlist, before matching."""

    position: int
    path: str | None = None
    artist: str | None = None
    title: str | None = None
    album: str | None = None
    duration_seconds: float | None = None
    source: str  # the raw line / "plex:<playlist>" - the user-visible origin


class ParsedPlaylist(BaseModel):
    name: str
    entries: list[SourceEntry]


class TrackSummary(BaseModel):
    """A library track offered as a match or suggestion."""

    item_id: int
    title: str
    artist: str
    album: str
    duration_seconds: float | None


class ImportEntryPreview(BaseModel):
    """One source entry with its match verdict."""

    position: int
    source: str
    artist: str | None
    title: str | None
    album: str | None
    duration_seconds: float | None
    status: Literal["matched", "ambiguous", "unmatched"]
    item_id: int | None = None
    match: TrackSummary | None = None
    suggestions: list[TrackSummary] = []


class PlaylistImportPreview(BaseModel):
    name: str
    entries: list[ImportEntryPreview]
    matched_count: int
    ambiguous_count: int
    unmatched_count: int


class PlaylistImportFile(BaseModel):
    name: str
    content: str


class PlaylistImportPreviewRequest(BaseModel):
    """Exactly one source: uploaded m3u files OR named Plex playlists."""

    files: list[PlaylistImportFile] | None = None
    plex_playlists: list[str] | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> Self:
        if (self.files is None) == (self.plex_playlists is None):
            raise ValueError("provide exactly one of files or plex_playlists")
        if self.files is not None and not self.files:
            raise ValueError("files must not be empty")
        if self.plex_playlists is not None and not self.plex_playlists:
            raise ValueError("plex_playlists must not be empty")
        return self


class PlaylistImportPreviewResponse(BaseModel):
    playlists: list[PlaylistImportPreview]


class ImportEntry(BaseModel):
    """One committed entry: a resolved library track OR a pending record."""

    item_id: int | None = None
    pending: PendingTrack | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.item_id is None) == (self.pending is None):
            raise ValueError("provide exactly one of item_id or pending")
        return self


class PlaylistImportPlaylist(BaseModel):
    name: str
    description: str = ""
    entries: list[ImportEntry]


class PlaylistImportRequest(BaseModel):
    playlists: list[PlaylistImportPlaylist]

    @model_validator(mode="after")
    def _non_empty(self) -> Self:
        if not self.playlists:
            raise ValueError("playlists must not be empty")
        return self


class PlaylistImportResponse(BaseModel):
    created: list[Playlist]
