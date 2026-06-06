"""Reconcile a MusicDrop playlist into a Plex account.

Finds the Plex playlist by title and replaces its contents with the resolved,
ordered tracks (clear + repopulate, preserving the playlist's ratingKey), or
creates it. Resolution is one library scan (``resolve_ordered_tracks``). The
caller passes paths already translated to Plex's view. Time is stamped by the
caller (this module has no clock) — it returns a ``PlexTargetState`` minus
``synced_at``, which the caller fills in.
"""

from __future__ import annotations

from typing import Any

from plexapi.exceptions import PlexApiException
from requests.exceptions import RequestException

from app.models.plex import PlexTargetState
from app.plex import client
from app.plex.config import PlexConfig
from app.plex.errors import PlexConnectionError, PlexNotConfigured
from app.plex.mapping import resolve_ordered_tracks


def _find_existing(server: Any, title: str) -> Any | None:
    for playlist in server.playlists():
        if playlist.title == title:
            return playlist
    return None


def sync_playlist(config: PlexConfig, title: str, plex_paths: list[str]) -> PlexTargetState:
    """Create/reconcile the Plex playlist ``title`` with ``plex_paths`` (ordered).

    Raises ``PlexNotConfigured`` when no URL/token, ``PlexConnectionError`` on a
    Plex/network failure. ``synced_at`` is left None for the caller to stamp.
    """
    if not (config.base_url and config.token):
        raise PlexNotConfigured("Plex is not configured.")
    try:
        server = client.connect(config.base_url, config.token)
        section = client.music_section(server)
        if section is None:
            raise PlexConnectionError("No music library found in Plex.")
        tracks, missing = resolve_ordered_tracks(section, plex_paths)
        existing = _find_existing(server, title)

        if not tracks:
            # Nothing to sync; remove any stale Plex playlist so it reflects empty.
            if existing is not None:
                existing.delete()
            return PlexTargetState(rating_key=None, status="empty", missing=missing)

        if existing is not None:
            current = existing.items()
            if current:
                existing.removeItems(current)
            existing.addItems(tracks)
            playlist = existing
        else:
            playlist = server.createPlaylist(title, items=tracks)

        status = "ok" if missing == 0 else "partial"
        return PlexTargetState(
            rating_key=str(playlist.ratingKey), status=status, missing=missing
        )
    except (PlexApiException, RequestException) as exc:
        raise PlexConnectionError("Plex sync failed.") from exc
