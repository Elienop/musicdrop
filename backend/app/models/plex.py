"""Plex settings + connection + user contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


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


class PlexSectionList(BaseModel):
    """GET /plex/sections — the server's music (artist-type) section titles."""

    sections: list[str]


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


class PlexMissingTrack(BaseModel):
    """One playlist track that did not resolve to a Plex track on the last sync.

    ``reason``: ``not_found`` — no Plex track at that path and no metadata
    candidate; ``ambiguous`` — several Plex tracks matched the metadata and
    album/track-number could not single one out (never guessed).
    """

    item_id: int
    title: str
    albumartist: str
    album: str
    reason: Literal["not_found", "ambiguous"]


class PlexTargetState(BaseModel):
    """Per-target Plex sync bookkeeping recorded on a playlist.

    Keyed by target in ``StoredPlaylist.plex`` — ``"admin"`` for the owner's
    account, per-user account ids for fan-out targets. ``rating_key`` is the
    Plex playlist this target's copy IS — a sync updates that playlist in place
    and never mints a new key while it exists. ``artwork_hash`` is the poster
    last pushed to that copy (so a sync re-uploads only when the art changed).
    """

    rating_key: str | None = None
    status: SyncStatus = "pending"
    missing: int = 0  # TRUE count of tracks not found in Plex
    missing_tracks: list[PlexMissingTrack] = []  # first MISSING_TRACKS_CAP of them
    artwork_hash: str | None = None
    synced_at: str | None = None
    error: str | None = None
