"""Models for the Reorganize Library feature (re-apply beets paths to existing files).

Wire surface: a dry-run PLAN (what would move) + a background-job STATUS (marching
progress). ``ReorganizeOutcome`` is internal (runner -> registry), not on the wire.
Scope is carried by route + ``?artist=`` query, so there is no request model.
"""

from typing import Literal

from pydantic import BaseModel

#: Which set of files a reorganize targets. Carried by route + ``?artist=`` query
#: at the API layer; echoed on the wire (plan + status) so the UI can tell which
#: scope a plan/job belongs to without inferring from ``artist``/``album_id``.
ReorganizeScope = Literal["library", "artist", "album"]

#: One row in the preview. ``kind`` distinguishes an album folder from a loose
#: singleton track. ``from_path``/``to_path`` are the unit's album-root dirs
#: (commonpath of its items' dirs) — equal when only filenames change.
ReorganizeMoveKind = Literal["album", "singleton"]


class ReorganizeMove(BaseModel):
    kind: ReorganizeMoveKind
    label: str  # "Artist - Album" / "Artist - Title"
    from_path: str
    to_path: str
    track_count: int  # items in this unit whose path changes


class OrphanFolder(BaseModel):
    name: str  # basename shown in the preview, e.g. "Old Artist feat. X"
    path: str  # path relative to the music root (display only)
    file_count: int  # leftover files (art/sidecars) in the husk


class ReorganizePlan(BaseModel):
    scope: ReorganizeScope  # which set of files this plan describes
    scope_label: str  # "library" / artist name / "Artist - Album"
    total: int  # units in scope (albums [+ singletons at library scope])
    will_move: int  # exact
    already_in_place: int  # exact
    moves: list[ReorganizeMove]  # capped at PREVIEW_ROW_CAP
    truncated: bool  # True when moves[] is shorter than will_move
    orphans: list[OrphanFolder]  # audio-empty husks that would be moved to Trash (capped)
    orphans_total: int  # exact husk count (orphans[] may be truncated)


#: Per-unit outcome (internal). moved = relocated; skipped = already organized /
#: empty; failed = ValueError/OSError raised by the move.
ReorganizeItemStatus = Literal["moved", "skipped", "failed"]


class ReorganizeOutcome(BaseModel):
    status: ReorganizeItemStatus
    label: str
    error: str | None = None
    # the unit's pre-move root dir (set on `moved`); seeds the orphan sweep
    source_dir: str | None = None


class ReorganizeUnitFailure(BaseModel):
    label: str  # "Artist - Album" / "Artist - Title"
    error: str  # human-readable reason from the move/verification


#: idle = never run / reset; running = sweeping; done/stopped/failed = terminal.
ReorganizePhase = Literal["idle", "running", "done", "stopped", "failed"]


class ReorganizeBackfillStatus(BaseModel):
    phase: ReorganizePhase
    job_id: str | None
    scope: ReorganizeScope | None  # None when idle; else the running job's scope
    total: int
    processed: int
    moved: int
    skipped: int
    failed: int
    current: str | None  # "Artist - Album" of the in-flight unit
    error: str | None
    artist: str | None  # set for artist scope (None otherwise)
    album_id: int | None  # set for album scope (None otherwise)
    scope_label: str  # "library" / artist name / "Artist - Album"
    orphans_trashed: int  # husks moved to Trash this run (0 until the post-move pass)
    failures: list[ReorganizeUnitFailure]  # first FAILURE_ROW_CAP failed units (label + reason)
