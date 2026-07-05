"""Plex settings + connection + user contract."""

from __future__ import annotations

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
    """One audio playlist on the Plex server (import source listing)."""

    name: str
    track_count: int


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


class PlexTargetState(BaseModel):
    """Per-target Plex sync bookkeeping recorded on a playlist.

    Keyed by target in ``StoredPlaylist.plex`` — ``"admin"`` for the owner's
    account (Chunk 6); per-user account ids in Chunk 7.
    """

    rating_key: str | None = None
    status: str = "pending"  # ok | partial | empty | failed | pending
    missing: int = 0  # tracks not found in Plex
    synced_at: str | None = None
    error: str | None = None
