"""Plex settings + connection + user contract."""

from __future__ import annotations

from pydantic import BaseModel


class PlexSettings(BaseModel):
    """GET /plex/settings — the token is never returned, only whether one is set."""

    base_url: str
    library_path: str
    has_token: bool


class PlexSettingsUpdate(BaseModel):
    """PUT body — any omitted field is left unchanged; token is write-only."""

    base_url: str | None = None
    library_path: str | None = None
    token: str | None = None


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
