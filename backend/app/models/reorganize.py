"""Models for the Reorganize Library feature (re-apply beets paths to existing files).

Wire surface: a dry-run PLAN (what would move) + a background-job STATUS (marching
progress). ``ReorganizeOutcome`` is internal (runner -> registry), not on the wire.
Scope is carried by route + ``?artist=`` query, so there is no request model.
"""

from typing import Literal

from pydantic import BaseModel

#: One row in the preview. ``kind`` distinguishes an album folder from a loose
#: singleton track. ``from_path``/``to_path`` are the unit's album-root dirs
#: (commonpath of its items' dirs) — equal when only filenames change.
ReorganizeMoveKind = Literal["album", "singleton"]


class ReorganizeMove(BaseModel):
    kind: ReorganizeMoveKind
    label: str  # "Artist — Album" / "Artist — Title"
    from_path: str
    to_path: str
    track_count: int  # items in this unit whose path changes


class ReorganizePlan(BaseModel):
    scope_label: str  # "library" / artist name / "Artist — Album"
    total: int  # units in scope (albums [+ singletons at library scope])
    will_move: int  # exact
    already_in_place: int  # exact
    moves: list[ReorganizeMove]  # capped at PREVIEW_ROW_CAP
    truncated: bool  # True when moves[] is shorter than will_move


#: Per-unit outcome (internal). moved = relocated; skipped = already organized /
#: empty; failed = ValueError/OSError raised by the move.
ReorganizeItemStatus = Literal["moved", "skipped", "failed"]


class ReorganizeOutcome(BaseModel):
    status: ReorganizeItemStatus
    label: str
    error: str | None = None


#: idle = never run / reset; running = sweeping; done/stopped/failed = terminal.
ReorganizePhase = Literal["idle", "running", "done", "stopped", "failed"]


class ReorganizeBackfillStatus(BaseModel):
    phase: ReorganizePhase
    job_id: str | None
    total: int
    processed: int
    moved: int
    skipped: int
    failed: int
    current: str | None  # "Artist — Album" of the in-flight unit
    error: str | None
    artist: str | None  # set for artist scope (None otherwise)
    album_id: int | None  # set for album scope (None otherwise)
    scope_label: str  # "library" / artist name / "Artist — Album"
