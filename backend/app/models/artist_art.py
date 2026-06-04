"""Models for writing artist art into the library (Phase 2)."""

from typing import Literal

from pydantic import BaseModel

#: written = >=1 file written; skipped = already had art (skip-existing);
#: no_art = no poster/background resolved; no_folder = VA/comp/flat-layout/empty;
#: failed = every write attempt errored (perms/IO).
ArtistArtStatus = Literal["written", "skipped", "no_art", "no_folder", "failed"]


class ArtistArtOutcome(BaseModel):
    artist: str
    status: ArtistArtStatus
    written: int  # files written for this artist
    dirs: int  # writable folders found


ArtistArtBackfillPhase = Literal["idle", "running", "done", "stopped", "failed"]


class ArtistArtBackfillStatus(BaseModel):
    phase: ArtistArtBackfillPhase
    job_id: str | None
    total: int
    processed: int
    written: int  # artists with >=1 file written
    skipped: int  # already-had / no-art / no-folder
    failed: int
    current: str | None  # artist name in flight
    error: str | None
    artist: str | None  # None = library-wide; else the scoped artist
    scope_label: str  # "library" or the artist name


class ArtistArtWriteSettings(BaseModel):
    enabled: bool
