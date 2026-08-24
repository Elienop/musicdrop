"""Pydantic contract for the artist-rename feature (fan-out of the album edit).

The rename edits ONE field — the album-level ``albumartist`` — across every
album of an artist. Per-track ``artist`` tags are deliberately never touched
(owner decision 2026-08-24: they must keep matching source metadata for
lyrics/LRC lookups). ``name`` is the exact roster identity: raw, case-sensitive
``albumartist`` equality, the same comparison the artist page's album filter
applies, so the fan-out covers precisely the albums the user is looking at.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models.edit import TrackMoveRefusal

# Same cap as AlbumFieldEdits: no multi-megabyte tag values.
_MAX_TEXT = 1000


class ArtistRenameRequest(BaseModel):
    """Rename ``name`` -> ``new_name`` across every album of the artist.

    ``new_name`` is stripped; equal to ``name`` after the strip is refused up
    front (a rename to itself is a no-op the UI should never submit). A case-
    or accent-only change is NOT equal and passes — fixing casing is a
    legitimate rename. ``name`` is never stripped or normalized: it is the
    identity, not an input.
    """

    name: str = Field(min_length=1, max_length=_MAX_TEXT)
    new_name: str = Field(min_length=1, max_length=_MAX_TEXT)

    @model_validator(mode="after")
    def _clean_new_name(self) -> ArtistRenameRequest:
        stripped = self.new_name.strip()
        if not stripped:
            raise ValueError("new_name must not be blank")
        if stripped == self.name:
            raise ValueError("new_name is the same as the current name")
        self.new_name = stripped
        return self


class ArtistRenameAlbumPreview(BaseModel):
    """One album's slice of the rename preview."""

    album_id: int
    title: str
    move_count: int
    refusals: list[TrackMoveRefusal]


class ArtistRenameMergeInfo(BaseModel):
    """Present when ``new_name`` already has albums: the rename merges into it."""

    existing_album_count: int


class ArtistRenamePreview(BaseModel):
    """The aggregate preview: per-album move picture + the merge note."""

    name: str
    new_name: str
    move_enabled: bool
    albums: list[ArtistRenameAlbumPreview]
    merge: ArtistRenameMergeInfo | None = None


class ArtistRenameAlbumResult(BaseModel):
    """One album's apply outcome. ``skipped_drifted`` = its albumartist changed
    between preview and apply, so it was left alone rather than silently renamed."""

    album_id: int
    title: str
    outcome: Literal["renamed", "skipped_drifted", "failed"]
    write_failures: int = 0
    move_failures: int = 0
    error: str | None = None


class ArtistRenameResult(BaseModel):
    """The full apply result, collateral included. Partial failure is a 200
    that says so per album — never a 4xx/5xx pretending the batch is atomic."""

    name: str
    new_name: str
    albums: list[ArtistRenameAlbumResult]
    old_name_remaining_albums: int
    # not_rekeyed = the old name still has albums (partial failure), so neither
    # side's portrait may be touched.
    portrait: Literal["moved", "kept_target", "none", "not_rekeyed"]
    playlists_reexported: int
    # not_needed = no files moved, or writing artist art to the library is off.
    artist_art_job: Literal["started", "skipped_busy", "not_needed"]
