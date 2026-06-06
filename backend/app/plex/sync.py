"""Reconcile a MusicDrop playlist into a Plex account.

Finds the Plex playlist by title and rebuilds it from the resolved, ordered
tracks: an existing playlist is **deleted and recreated** (rather than emptied
in place) so we never depend on Plex's behaviour for a playlist whose items were
all removed — some servers auto-delete an emptied playlist, which would make a
subsequent ``addItems`` fail. Resolution is one library scan
(``resolve_ordered_tracks``). The caller passes paths already translated to
Plex's view.

Exception contract — raises ONLY:
- ``PlexNotConfigured`` when no URL/token is set, or
- ``PlexConnectionError`` on ANY Plex API, network, or unexpected sync failure.

Time is stamped by the caller (this module has no clock): the returned
``PlexTargetState`` has ``synced_at = None`` for the caller to fill in.
"""

from __future__ import annotations

from typing import Any

from app.models.plex import PlexTargetState
from app.plex import client as client  # explicit re-export: the patchable seam (sync.client)
from app.plex.config import PlexConfig
from app.plex.errors import PlexConnectionError, PlexNotConfigured
from app.plex.mapping import resolve_ordered_tracks


def _find_existing(server: Any, title: str) -> Any | None:
    for playlist in server.playlists():
        if playlist.title == title:
            return playlist
    return None


def sync_playlist(config: PlexConfig, title: str, plex_paths: list[str]) -> PlexTargetState:
    """Create/reconcile the Plex playlist ``title`` with ``plex_paths`` (ordered)."""
    if not (config.base_url and config.token):
        raise PlexNotConfigured("Plex is not configured.")
    try:
        server = client.connect(config.base_url, config.token)
        section = client.music_section(server)
        if section is None:
            raise PlexConnectionError("No music library found in Plex.")
        tracks, missing = resolve_ordered_tracks(section, plex_paths)
        existing = _find_existing(server, title)

        # Delete any existing playlist of this title first, so the result is a
        # clean rebuild in either branch (empty or repopulated) — no reliance on
        # Plex's emptied-playlist behaviour.
        if existing is not None:
            existing.delete()

        if not tracks:
            return PlexTargetState(rating_key=None, status="empty", missing=missing)

        playlist = server.createPlaylist(title, items=tracks)
        status = "ok" if missing == 0 else "partial"
        return PlexTargetState(rating_key=str(playlist.ratingKey), status=status, missing=missing)
    except PlexConnectionError:
        raise  # already our type (e.g. no music section) — don't re-wrap
    except Exception as exc:
        # Translate EVERYTHING else (plexapi errors, requests network errors, or
        # any unexpected library failure) into our connection error, so the
        # caller only ever sees PlexNotConfigured / PlexConnectionError.
        raise PlexConnectionError("Plex sync failed.") from exc
