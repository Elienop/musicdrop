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
    # A per-response client key (minted uuid): preview playlists have no stored
    # identity and names can collide, so list rendering must not key on index.
    preview_id: str
    name: str
    entries: list[ImportEntryPreview]
    matched_count: int
    ambiguous_count: int
    unmatched_count: int


class PlaylistImportFile(BaseModel):
    name: str
    content: str


class PlaylistImportPreviewRequest(BaseModel):
    """Exactly one source: uploaded m3u files OR Plex playlists by ratingKey.

    Plex selections travel by ``ratingKey``, never by title — Plex allows
    duplicate titles, and keying by title makes one of a same-titled pair
    permanently unreachable.
    """

    files: list[PlaylistImportFile] | None = None
    plex_rating_keys: list[str] | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> Self:
        if (self.files is None) == (self.plex_rating_keys is None):
            raise ValueError("provide exactly one of files or plex_rating_keys")
        if self.files is not None and not self.files:
            raise ValueError("files must not be empty")
        if self.plex_rating_keys is not None and not self.plex_rating_keys:
            raise ValueError("plex_rating_keys must not be empty")
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
    # The source Plex playlist's TITLE, if any — the user-facing origin, kept
    # verbatim (never the edited name, never a decorated "plex:<name>").
    plex_source: str | None = None
    # The source Plex playlist's IDENTITY (``ratingKey``). Titles aren't unique
    # on Plex, so the poster pull resolves by this; ``plex_source`` remains the
    # display/back-compat value and is only the fallback when no key is sent.
    plex_rating_key: str | None = None


class PlaylistImportRequest(BaseModel):
    playlists: list[PlaylistImportPlaylist]

    @model_validator(mode="after")
    def _non_empty(self) -> Self:
        if not self.playlists:
            raise ValueError("playlists must not be empty")
        return self


class PlaylistImportFailure(BaseModel):
    """One playlist that couldn't be created during a multi-playlist import."""

    name: str
    error: str


class PlaylistImportResponse(BaseModel):
    created: list[Playlist]
    failed: list[PlaylistImportFailure] = []
