"""Playlist API contract (the owned-playlist read/write models).

Playlists store ordered, uid-keyed entries (a resolved library track or a
pending one); the read models expose them as ``tracks``. ``track_ids`` survives
only as the add-tracks *request* field (:class:`PlaylistAddTracksRequest`) and
as the legacy on-disk record shape, which the store migrates to entries on read.
``target_plex_users`` lives on the read models from the start so the contract
does not churn between chunks.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.models.plex import MISSING_TRACKS_CAP, PlexTargetState


class PendingTrack(BaseModel):
    """The remembered identity of a playlist entry that has no library track
    yet (an import that didn't match). ``source`` is the original text the
    entry came from (m3u line / file path / "plex:<playlist>")."""

    artist: str | None = None
    title: str | None = None
    album: str | None = None
    duration_seconds: float | None = None
    source: str = ""


class Playlist(BaseModel):
    """Summary view of a playlist (list rows + create/patch responses)."""

    id: str
    name: str
    description: str
    track_count: int
    pending_count: int
    target_plex_users: list[str]
    plex: dict[str, PlexTargetState] = Field(
        description=(
            "Per-target Plex sync state, keyed by target ('admin' or a Plex user id). "
            "On this summary view every target's `missing_tracks` is ALWAYS empty — an "
            "empty list here means 'not carried', never 'nothing missed'; `missing` is "
            "the true count either way. One target can hold up to "
            f"{MISSING_TRACKS_CAP} miss identities and a listing multiplies that by every "
            "playlist, so the identities are carried only on the playlist detail response "
            "(GET /api/playlists/{playlist_id})."
        )
    )
    created_at: str
    updated_at: str
    # Cover art: the stored artwork's content hash (None when the playlist has
    # no cover), and up to a few album ids from the playlist's resolved tracks —
    # the FE renders a collage from those covers when there is no uploaded art.
    artwork_hash: str | None = None
    cover_album_ids: list[int] = []


class PlaylistTrack(BaseModel):
    """A track row in a playlist, ordered by its stable per-slot ``uid``.

    A RESOLVED row carries the library ``id`` (``available`` is False when that
    id no longer resolves — deleted from the library — but the slot still holds
    its position). A PENDING row (``pending`` True) has ``id`` None: its identity
    is remembered text (artist/title/album) with no library track yet. Either
    way the slot keeps its position and can be removed or resolved."""

    uid: str
    id: int | None
    title: str
    artist: str
    album: str
    duration_seconds: float | None
    available: bool
    pending: bool = False
    # The pending entry's original source text (the raw m3u line / file path /
    # "plex:<playlist>") — the tooltip identity for a bare-path import. None on
    # resolved and unavailable rows (there's a real library item behind those).
    source: str | None = None


class PlaylistDetail(Playlist):
    """Single-playlist view with the ordered, resolved tracklist."""

    # Redeclared only to carry its own description: this is the ONE view that
    # ships the miss identities, and the summary's field says the opposite.
    plex: dict[str, PlexTargetState] = Field(
        description=(
            "Per-target Plex sync state, keyed by target ('admin' or a Plex user id). "
            "Unlike the summary views, this one CARRIES `missing_tracks`: the identity of "
            "each playlist track the last sync could not find in Plex, in playlist order, "
            f"capped at {MISSING_TRACKS_CAP} per target (`missing` stays the true total, so "
            "`missing > len(missing_tracks)` means the rest were not listed)."
        )
    )
    tracks: list[PlaylistTrack]


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
    target_plex_users: list[str] | None = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped

    @field_validator("target_plex_users")
    @classmethod
    def _clean_targets(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        # "admin" is the owner's own sync key (synced unconditionally) — it is
        # never a *target* user; drop it (and any duplicates) so the fan-out
        # can't sync the admin account twice.
        seen: set[str] = set()
        cleaned: list[str] = []
        for uid in value:
            if uid == "admin" or uid in seen:
                continue
            seen.add(uid)
            cleaned.append(uid)
        return cleaned


# A generous ceiling on the ids accepted in one request — far above any real
# playlist, but it stops a pathological/buggy client from forcing an unbounded
# number of per-id library lookups in one call.
_MAX_TRACK_IDS = 10_000


class PlaylistAddTracksRequest(BaseModel):
    track_ids: list[int]
    position: int | None = None

    @field_validator("track_ids")
    @classmethod
    def _non_empty_and_bounded(cls, value: list[int]) -> list[int]:
        if not value:
            raise ValueError("track_ids must not be empty")
        if len(value) > _MAX_TRACK_IDS:
            raise ValueError(f"track_ids must contain at most {_MAX_TRACK_IDS} items")
        return value


class PlaylistReorderRequest(BaseModel):
    """Full replacement: the entries are now exactly this ordered uid list.

    A duplicate-free SUBSET of the playlist's current uids — unlisted entries
    are removed, empty clears. Unknown uids are rejected by the endpoint."""

    entry_uids: list[str]

    @field_validator("entry_uids")
    @classmethod
    def _bounded_and_unique(cls, value: list[str]) -> list[str]:
        if len(value) > _MAX_TRACK_IDS:
            raise ValueError(f"entry_uids must contain at most {_MAX_TRACK_IDS} items")
        if len(set(value)) != len(value):
            raise ValueError("entry_uids must not contain duplicates")
        return value


class PlaylistResolveEntryRequest(BaseModel):
    item_id: int


class PlaylistMergeRequest(BaseModel):
    """Fold ``source_id`` into the playlist named by the path (the TARGET).

    ``delete_source`` is an explicit, separate choice - the source survives by
    default. Ticking it removes the source record, its ``.m3u8``, and its Plex
    copies from admin and from every account it was synced to.
    """

    source_id: str
    delete_source: bool = False


class PlaylistMergeResponse(BaseModel):
    """The merged target, plus what the merge actually did.

    ``added`` and ``skipped_duplicates`` come back from the server rather than
    being inferred client-side, so the confirmation can state the outcome
    without recounting a list it would have to fetch twice to get right.
    """

    playlist: PlaylistDetail
    added: int
    skipped_duplicates: int
    source_deleted: bool
