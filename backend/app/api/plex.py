"""Plex settings, connection test, and user discovery endpoints.

The Plex config store is resolved from ``settings`` per request (like the
playlists dir) so it works under the lifespan-less test client. The admin token
is write-only over the API — ``GET`` returns ``has_token``, never the value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool

from app.config import settings
from app.models.plex import (
    PlexConnection,
    PlexPlaylistList,
    PlexSectionList,
    PlexSettings,
    PlexSettingsUpdate,
    PlexUserList,
)
from app.plex import playlists_pull, service
from app.plex.config import PlexConfig, PlexConfigStore
from app.plex.errors import PlexConnectionError, PlexNotConfigured

router = APIRouter(tags=["plex"])


def get_plex_store() -> PlexConfigStore:
    base = settings.plex_settings_dir.strip()
    directory = Path(base) if base else Path(settings.beets_dir) / "plex"
    env = PlexConfig(
        base_url=settings.plex_url,
        token=settings.plex_token,
        library_path=settings.plex_library_path,
        library_section=settings.plex_library_section,
    )
    return PlexConfigStore(directory / "plex.json", env_defaults=env)


def _to_settings(config: PlexConfig) -> PlexSettings:
    return PlexSettings(
        base_url=config.base_url,
        library_path=config.library_path,
        library_section=config.library_section,
        has_token=bool(config.token),
    )


@router.get("/plex/settings")
async def get_plex_settings(
    store: Annotated[PlexConfigStore, Depends(get_plex_store)],
) -> PlexSettings:
    return _to_settings(store.get())


@router.put("/plex/settings")
async def put_plex_settings(
    body: PlexSettingsUpdate,
    store: Annotated[PlexConfigStore, Depends(get_plex_store)],
) -> PlexSettings:
    config = await run_in_threadpool(
        store.update,
        base_url=body.base_url,
        token=body.token,
        library_path=body.library_path,
        library_section=body.library_section,
    )
    return _to_settings(config)


@router.post("/plex/test")
async def test_plex(
    store: Annotated[PlexConfigStore, Depends(get_plex_store)],
) -> PlexConnection:
    return await run_in_threadpool(service.test_connection, store.get())


@router.get("/plex/users")
async def list_plex_users(
    store: Annotated[PlexConfigStore, Depends(get_plex_store)],
) -> PlexUserList:
    try:
        users = await run_in_threadpool(service.discover_users, store.get())
    except PlexNotConfigured as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PlexConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return PlexUserList(users=users)


@router.get("/plex/sections")
async def list_plex_sections(
    store: Annotated[PlexConfigStore, Depends(get_plex_store)],
) -> PlexSectionList:
    try:
        sections = await run_in_threadpool(service.list_music_sections, store.get())
    except PlexNotConfigured as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PlexConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return PlexSectionList(sections=sections)


@router.get("/plex/playlists")
async def list_plex_playlists(
    store: Annotated[PlexConfigStore, Depends(get_plex_store)],
) -> PlexPlaylistList:
    try:
        playlists = await run_in_threadpool(playlists_pull.list_audio_playlists, store.get())
    except PlexNotConfigured as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PlexConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return PlexPlaylistList(playlists=playlists)
