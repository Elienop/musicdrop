"""Models for Sync-with-disk (the ``beet update`` equivalent).

Wire surface: a dry-run PLAN + a background-job STATUS, mirroring the
reorganize feature. ``DiskSyncOutcome`` is internal (runner -> registry).
One-way disk -> DB; the job never writes, moves, or deletes files.
"""

from typing import Literal

from pydantic import BaseModel, Field


class DiskSyncRemoval(BaseModel):
    label: str  # "Artist - Title"
    path: str  # relative to the music dir where possible (display only)


class DiskSyncEmptiedAlbum(BaseModel):
    """An album row that loses its LAST item — beets prunes it.

    Two album rows can carry the same label (a real album plus a phantom row
    holding a stray duplicate), so the label alone cannot say which row is
    meant. ``track_count`` + ``path`` are read off THAT row's own items.
    """

    label: str  # "Artist - Album" — not unique across album rows
    track_count: int  # items this row holds; all of them missing, by definition
    path: str  # the row's folder, relative to the music dir where possible


class DiskSyncChange(BaseModel):
    label: str  # "Artist - Title"
    fields: list[str]  # sorted media-field names whose value differs on disk


class DiskSyncReadError(BaseModel):
    label: str  # "Artist - Title"
    error: str  # read failure, verbatim


class DiskSyncPlan(BaseModel):
    total_items: int  # items examined
    will_remove: int  # exact count of missing-file rows
    will_update: int  # exact count of tag-refresh rows
    emptied_albums: list[DiskSyncEmptiedAlbum]  # capped rows
    emptied_total: int  # exact
    removals: list[DiskSyncRemoval]  # capped at PREVIEW_ROW_CAP
    changes: list[DiskSyncChange]  # capped
    read_errors: list[DiskSyncReadError]  # capped
    truncated: bool  # True when any capped list is shorter than its total


#: idle = never run / reset; running = walking; done/stopped/failed = terminal.
DiskSyncPhase = Literal["idle", "running", "done", "stopped", "failed"]


class DiskSyncStatus(BaseModel):
    phase: DiskSyncPhase
    job_id: str | None
    total: int
    processed: int
    removed: int
    updated: int
    unchanged: int  # mtime-skipped + read-with-no-field-change
    read_errors: int
    emptied_albums: int
    current: str | None  # label of the in-flight item
    error: str | None  # job-level failure
    # Playlists whose `.m3u8` was rewritten because this sync DROPPED one of their
    # tracks (its file is gone from disk, so the export must stop listing it).
    # Written once, just before the job reaches its terminal phase; a status read
    # while `running` always shows 0. Required (no default) like every field here.
    playlists_reexported: int
    failures: list[DiskSyncReadError]  # first FAILURE_ROW_CAP read errors


#: Per-item outcome (internal, runner -> registry).
DiskSyncOutcomeStatus = Literal["removed", "updated", "unchanged", "read_error"]


class DiskSyncOutcome(BaseModel):
    status: DiskSyncOutcomeStatus
    label: str
    fields: list[str] = Field(default_factory=list)  # changed fields (updated)
    error: str | None = None  # read_error detail
    # The beets item id this outcome is about, captured BEFORE any removal. The
    # runner reads it off the ``removed`` outcomes to seed the `.m3u8` re-export;
    # the registry ignores it. Optional so a hand-built outcome (tests, fakes)
    # stays valid.
    item_id: int | None = None
