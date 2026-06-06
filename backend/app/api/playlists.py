"""CRUD endpoints for MusicDrop-owned playlists.

Thin router over ``app.playlists.store``. The store directory is resolved from
``settings`` (not ``app.state``) so it works under the lifespan-less ``client``
test fixture; tests drive it via the monkeypatched ``settings.beets_dir``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.concurrency import run_in_threadpool

from app.config import settings
from app.models.playlist import (
    Playlist,
    PlaylistCreateRequest,
    PlaylistDetail,
    PlaylistUpdateRequest,
)
from app.playlists import store
from app.playlists.store import StoredPlaylist

router = APIRouter(tags=["playlists"])


def get_playlists_dir() -> Path:
    """Resolve the owned-playlist store dir from settings.

    Empty ``MUSICDROP_PLAYLISTS_DIR`` -> ``<beets_dir>/playlists``.
    """
    configured = settings.playlists_dir.strip()
    if configured:
        return Path(configured)
    return Path(settings.beets_dir) / "playlists"


def _to_playlist(record: StoredPlaylist) -> Playlist:
    return Playlist(
        id=record.id,
        name=record.name,
        description=record.description,
        track_count=len(record.track_ids),
        target_plex_users=record.target_plex_users,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _to_detail(record: StoredPlaylist) -> PlaylistDetail:
    return PlaylistDetail(**_to_playlist(record).model_dump(), track_ids=record.track_ids)


@router.get("/playlists", response_model=list[Playlist])
async def list_playlists_endpoint(
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
) -> list[Playlist]:
    records = await run_in_threadpool(store.list_playlists, playlists_dir)
    return [_to_playlist(record) for record in records]


@router.post("/playlists", response_model=Playlist)
async def create_playlist_endpoint(
    body: PlaylistCreateRequest,
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
) -> Playlist:
    record = await run_in_threadpool(
        store.create_playlist,
        playlists_dir,
        name=body.name,
        description=body.description,
    )
    return _to_playlist(record)


@router.get("/playlists/{playlist_id}", response_model=PlaylistDetail)
async def get_playlist_endpoint(
    playlist_id: str,
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
) -> PlaylistDetail:
    record = await run_in_threadpool(store.get_playlist, playlists_dir, playlist_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return _to_detail(record)


@router.patch("/playlists/{playlist_id}", response_model=Playlist)
async def update_playlist_endpoint(
    playlist_id: str,
    body: PlaylistUpdateRequest,
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
) -> Playlist:
    record = await run_in_threadpool(
        store.update_playlist,
        playlists_dir,
        playlist_id,
        name=body.name,
        description=body.description,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return _to_playlist(record)


@router.delete("/playlists/{playlist_id}", status_code=204)
async def delete_playlist_endpoint(
    playlist_id: str,
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
) -> Response:
    deleted = await run_in_threadpool(store.delete_playlist, playlists_dir, playlist_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return Response(status_code=204)
