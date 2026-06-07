"""Reconcile a MusicDrop playlist into a Plex account.

Finds the Plex playlist by title and rebuilds it from the resolved, ordered
tracks: an existing playlist is **deleted and recreated** (rather than emptied
in place) so we never depend on Plex's behaviour for a playlist whose items were
all removed — some servers auto-delete an emptied playlist, which would make a
subsequent ``addItems`` fail. Resolution is one library scan
(``resolve_ordered_tracks``). The caller passes ``PlexTrackSpec``s whose paths
are already translated to Plex's view.

Exception contract — raises ONLY:
- ``PlexNotConfigured`` when no URL/token is set, or
- ``PlexConnectionError`` on ANY Plex API, network, or unexpected sync failure.

Time is stamped by the caller (this module has no clock): the returned
``PlexTargetState`` has ``synced_at = None`` for the caller to fill in.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.models.plex import PlexTargetState
from app.plex import client as client  # explicit re-export: the patchable seam (sync.client)
from app.plex.config import PlexConfig
from app.plex.errors import PlexConnectionError, PlexNotConfigured
from app.plex.mapping import PlexTrackSpec, resolve_ordered_tracks


def _find_existing(server: Any, title: str) -> Any | None:
    for playlist in server.playlists():
        if playlist.title == title:
            return playlist
    return None


def _reconcile_on(server: Any, title: str, tracks: list[Any], missing: int) -> PlexTargetState:
    """Rebuild the playlist ``title`` on ``server``: delete any existing one of
    that title, then recreate it from ``tracks`` (or leave it absent if empty).

    Delete-then-recreate (rather than emptying in place) keeps the result a clean
    rebuild in either branch — no reliance on Plex's emptied-playlist behaviour.
    """
    existing = _find_existing(server, title)
    if existing is not None:
        existing.delete()
    if not tracks:
        return PlexTargetState(rating_key=None, status="empty", missing=missing)
    playlist = server.createPlaylist(title, items=tracks)
    status = "ok" if missing == 0 else "partial"
    return PlexTargetState(rating_key=str(playlist.ratingKey), status=status, missing=missing)


def sync_playlist(config: PlexConfig, title: str, specs: list[PlexTrackSpec]) -> PlexTargetState:
    """Create/reconcile the playlist on the admin account only."""
    if not (config.base_url and config.token):
        raise PlexNotConfigured("Plex is not configured.")
    try:
        server = client.connect(config.base_url, config.token)
        section = client.music_section(server)
        if section is None:
            raise PlexConnectionError("No music library found in Plex.")
        tracks, missing = resolve_ordered_tracks(section, specs)
        return _reconcile_on(server, title, tracks, missing)
    except PlexConnectionError:
        raise  # already our type (e.g. no music section) — don't re-wrap
    except Exception as exc:
        # Translate EVERYTHING else (plexapi errors, requests network errors, or
        # any unexpected library failure) into our connection error, so the
        # caller only ever sees PlexNotConfigured / PlexConnectionError.
        raise PlexConnectionError("Plex sync failed.") from exc


def sync_playlist_to_targets(
    config: PlexConfig, title: str, specs: list[PlexTrackSpec], target_user_ids: list[str]
) -> dict[str, PlexTargetState]:
    """Reconcile the playlist on the admin account AND each target user.

    The owner ("admin") always gets their copy. Each target user is synced via
    ``switchUser`` and ISOLATED — one user's failure marks only that user
    ``failed`` and never aborts the others. Tracks are resolved once (the admin
    library scan) and reused for every target (ratingKeys are library-global).
    """
    if not (config.base_url and config.token):
        raise PlexNotConfigured("Plex is not configured.")
    try:
        admin = client.connect(config.base_url, config.token)
        section = client.music_section(admin)
        if section is None:
            raise PlexConnectionError("No music library found in Plex.")
        tracks, missing = resolve_ordered_tracks(section, specs)
    except PlexConnectionError:
        raise
    except Exception as exc:
        raise PlexConnectionError("Plex sync failed.") from exc

    # Bind each target id through a factory call so the closure captures the
    # current uid per iteration — sidesteps the late-binding-loop-variable trap.
    def _run_for(uid: str) -> Callable[[], PlexTargetState]:
        return lambda: _reconcile_on(admin.switchUser(uid), title, tracks, missing)

    results: dict[str, PlexTargetState] = {}
    results["admin"] = _safe_reconcile(lambda: _reconcile_on(admin, title, tracks, missing))
    for uid in target_user_ids:
        results[uid] = _safe_reconcile(_run_for(uid))
    return results


def _safe_reconcile(run: Callable[[], PlexTargetState]) -> PlexTargetState:
    try:
        return run()
    except Exception:
        return PlexTargetState(status="failed", error="Couldn't sync to this Plex account.")
