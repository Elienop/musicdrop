"""Models for the lyrics completeness view (presence + fetch-into-files).

MusicDrop never renders lyrics text — these carry the per-track fetch OUTCOME
and album/library coverage, not the lyrics themselves.
"""

from typing import Literal

from pydantic import BaseModel

#: Per-track fetch outcome. found -> written (if writes on); the rest leave the
#: file untouched. skipped_existing = already had lyrics (skip-existing default);
#: skipped_no_metadata = no usable artist/title to search.
ItemLyricsStatus = Literal[
    "found",
    "not_found",
    "fetch_failed",
    "skipped_existing",
    "skipped_checked",
    "skipped_no_metadata",
]


class ItemLyricsOutcome(BaseModel):
    item_id: int
    status: ItemLyricsStatus
    source: str | None  # backend name (e.g. "lrclib") on found; else None
    written: bool  # try_write() ran (writes enabled AND found)


class LyricsCoverage(BaseModel):
    total: int
    with_lyrics: int
    checked_no_lyrics: int  # no lyrics, but already searched (lyrics_checked set)
    percent: float  # 0.0-100.0, rounded to 1 dp


#: idle = never run / reset; running = sweeping; done/stopped/failed = terminal.
LyricsBackfillPhase = Literal["idle", "running", "done", "stopped", "failed"]


class LyricsBackfillStatus(BaseModel):
    phase: LyricsBackfillPhase
    job_id: str | None
    total: int
    processed: int
    found: int
    not_found: int
    failed: int
    skipped: int
    current: str | None  # "artist - album - title" of the in-flight track
    writes_enabled: bool
    error: str | None
    album_id: int | None  # None = library-wide; else the scoped album
    scope_label: str  # "library" or "artist - album" (banner/label text)
