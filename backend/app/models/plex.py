"""Plex settings + connection + user contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PlexSettings(BaseModel):
    """GET /plex/settings — the token is never returned, only whether one is set."""

    base_url: str
    library_path: str
    library_section: str
    has_token: bool


class PlexSettingsUpdate(BaseModel):
    """PUT body — any omitted field is left unchanged; token is write-only."""

    base_url: str | None = None
    library_path: str | None = None
    library_section: str | None = None
    token: str | None = None


class PlexSectionInfo(BaseModel):
    """One music (artist-type) library section on the Plex server.

    ``locations`` are the folder paths PLEX reports for that library — the truth
    the ``library_path`` setting has to agree with, since ``translate_path``
    rebases every beets path onto it. A library can span several folders, so
    this is a list; it is empty only when the server listed none.
    """

    title: str
    locations: list[str]


class PlexSectionList(BaseModel):
    """GET /plex/sections — the server's music (artist-type) sections."""

    sections: list[PlexSectionInfo]


class PlexPlaylistInfo(BaseModel):
    """One audio playlist on the Plex server (import source listing).

    ``rating_key`` is the playlist's Plex identity (``ratingKey``, stringified).
    Titles are NOT unique on Plex — two playlists may share one — so every
    selection travels by key; ``name`` is display only.
    """

    name: str
    track_count: int
    rating_key: str


class PlexPlaylistList(BaseModel):
    playlists: list[PlexPlaylistInfo]


class PlexConnection(BaseModel):
    ok: bool
    server_name: str | None = None
    error: str | None = None


class PlexUserInfo(BaseModel):
    id: str
    name: str
    home: bool


class PlexUserList(BaseModel):
    users: list[PlexUserInfo]


SyncStatus = Literal["ok", "partial", "empty", "failed", "pending"]

# How many missing-track identities a target state carries. ``missing`` is always
# the TRUE total; this only bounds the per-record JSON for a huge all-missing sync.
MISSING_TRACKS_CAP = 200


# Why one playlist row is not on Plex after a sync. The first two are the
# MATCHER's verdicts about a track it could not find at all (``app/plex/mapping.py``);
# the third is the SYNC's, and is a different kind of news — the track resolved
# and IS in the Plex playlist, once, but the playlist lists it twice and this
# server would not hold a second row of it (``app/plex/sync.py``).
PlexMissReason = Literal["not_found", "ambiguous", "duplicate_collapsed"]


class PlexMissingTrack(BaseModel):
    """One playlist track the last sync could not fully place on Plex.

    ``reason``: ``not_found`` — no Plex track at that path and no metadata
    candidate; ``ambiguous`` — several Plex tracks matched the metadata and
    album/track-number could not single one out (never guessed);
    ``duplicate_collapsed`` — the track is on Plex and in the playlist, but this
    playlist lists it more than once and Plex kept a single row.

    ``item_id`` is the beets library item, so two rows for one item report under
    one id — which is exactly right for the first two reasons (both rows are
    missing) and is what makes the third readable: the item is there, the
    second listing of it is not.
    """

    item_id: int
    title: str
    albumartist: str
    album: str
    reason: PlexMissReason


# Which rung of the matcher resolved a track — see ``app/plex/mapping.py``, whose
# docstring explains why there are three and why their ORDER is contract.
PlexMatchMethod = Literal["path", "artist_title", "album_length"]


class PlexMatchCounts(BaseModel):
    """How many of a sync's tracks each matching rung accounted for.

    A flat "ok" hides which rung did the work, and that blind spot has already
    cost this app once: path matching was broken for the app's whole life while
    the metadata fallback silently carried 100% of every sync, and every sync
    still reported "ok". These counts are what makes that visible — a library
    whose ``path`` count is suddenly zero is misconfigured, not fine.

    Every field DEFAULTS to zero, which is also how a new rung is added without
    breaking the wire: a reader that predates the new field ignores it, and a
    reader that postdates a record written without it sees zero. A rung missing
    from here would be counted nowhere, so ``PlexMatchMethod`` and these field
    names are pinned equal by a test.
    """

    path: int = 0  # exact file path — the strongest evidence
    artist_title: int = 0  # (album-artist, title) — separate copies of one library
    album_length: int = 0  # (album, title) + duration — a Plex-rewritten artist


class PlexTargetState(BaseModel):
    """Per-target Plex sync bookkeeping recorded on a playlist.

    Keyed by target in ``StoredPlaylist.plex`` — ``"admin"`` for the owner's
    account, per-user account ids for fan-out targets. ``rating_key`` is the
    Plex playlist this target's copy IS — a sync updates that playlist in place
    and never mints a new key while it exists. ``artwork_hash`` is the poster
    last pushed to that copy (so a sync re-uploads only when the art changed).
    ``matched_by`` breaks the resolved tracks down by which rung found them.
    """

    rating_key: str | None = None
    status: SyncStatus = "pending"
    missing: int = 0  # TRUE count of tracks not found in Plex
    missing_tracks: list[PlexMissingTrack] = []  # first MISSING_TRACKS_CAP of them
    # All-zero on a target that never got as far as applying a resolution (a
    # per-target failure), which is why the counts sit beside `status` rather
    # than standing in for it.
    matched_by: PlexMatchCounts = Field(default_factory=PlexMatchCounts)
    artwork_hash: str | None = None
    synced_at: str | None = None
    error: str | None = None
