"""Reconcile a MusicDrop playlist into a Plex account.

Identifies the target Plex playlist by IDENTITY — never by bare title — and
rebuilds it from the resolved, ordered tracks. MusicDrop playlist names are not
unique, so title matching would let two same-named playlists clobber each
other. The existing Plex playlist to replace is found by (in order) the recorded
``rating_key``, else a ``MusicDrop-id:{playlist_id}`` marker stamped into the
Plex playlist's ``summary``; if neither matches we create a fresh one. The found
playlist is **deleted and recreated** (rather than emptied in place) so we never
depend on Plex's behaviour for a playlist whose items were all removed — some
servers auto-delete an emptied playlist, which would make a subsequent
``addItems`` fail; the recreated playlist is re-stamped with the id marker.
Resolution is one library scan (``resolve_ordered_tracks``). The caller passes
``PlexTrackSpec``s whose paths are already translated to Plex's view.

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


def _no_section_error(title: str) -> PlexConnectionError:
    if title.strip():
        return PlexConnectionError(f"Plex music section '{title.strip()}' not found.")
    return PlexConnectionError("No music library found in Plex.")


def _summary_marker(playlist_id: str) -> str:
    """The MusicDrop identity stamp embedded in a synced Plex playlist's summary."""
    return f"MusicDrop-id:{playlist_id}"


def _find_by_summary_marker(server: Any, playlist_id: str) -> Any | None:
    marker = _summary_marker(playlist_id)
    for playlist in server.playlists():
        if marker in (getattr(playlist, "summary", "") or ""):
            return playlist
    return None


def _reconcile_on(
    server: Any,
    title: str,
    tracks: list[Any],
    missing: int,
    *,
    playlist_id: str,
    rating_key: str | None,
) -> PlexTargetState:
    """Rebuild this MusicDrop playlist on ``server``: find its existing Plex copy
    by IDENTITY (recorded ``rating_key`` first, else the ``playlist_id`` summary
    marker), delete it, then recreate from ``tracks`` (or leave it absent if
    empty) and stamp the id marker onto the new playlist.

    Never matches by title — same-named playlists must not clobber each other.
    Delete-then-recreate (rather than emptying in place) keeps the result a clean
    rebuild in either branch — no reliance on Plex's emptied-playlist behaviour.
    """
    existing = _find_by_rating_key(server, rating_key) if rating_key is not None else None
    if existing is None:
        existing = _find_by_summary_marker(server, playlist_id)
    if existing is not None:
        existing.delete()
    if not tracks:
        return PlexTargetState(rating_key=None, status="empty", missing=missing)
    playlist = server.createPlaylist(title, items=tracks)
    try:
        playlist.editSummary(_summary_marker(playlist_id))
    except Exception:
        # Best-effort stamp: editSummary is a SEPARATE Plex PUT that can fail
        # transiently. We still return the real ratingKey, so the next sync
        # re-finds this playlist by ratingKey even if the marker never landed.
        # Failing the whole reconcile here would record no ratingKey and orphan
        # the just-created playlist, duplicating it on the next sync.
        pass
    status = "ok" if missing == 0 else "partial"
    return PlexTargetState(rating_key=str(playlist.ratingKey), status=status, missing=missing)


def sync_playlist(
    config: PlexConfig,
    title: str,
    specs: list[PlexTrackSpec],
    *,
    playlist_id: str,
    rating_key: str | None = None,
) -> PlexTargetState:
    """Create/reconcile the playlist on the admin account only, by identity."""
    if not (config.base_url and config.token):
        raise PlexNotConfigured("Plex is not configured.")
    try:
        server = client.connect(config.base_url, config.token)
        section = client.music_section(server, config.library_section)
        if section is None:
            raise _no_section_error(config.library_section)
        tracks, missing = resolve_ordered_tracks(section, specs)
        return _reconcile_on(
            server, title, tracks, missing, playlist_id=playlist_id, rating_key=rating_key
        )
    except PlexConnectionError:
        raise  # already our type (e.g. no music section) — don't re-wrap
    except Exception as exc:
        # Translate EVERYTHING else (plexapi errors, requests network errors, or
        # any unexpected library failure) into our connection error, so the
        # caller only ever sees PlexNotConfigured / PlexConnectionError.
        raise PlexConnectionError("Plex sync failed.") from exc


def sync_playlist_to_targets(
    config: PlexConfig,
    title: str,
    specs: list[PlexTrackSpec],
    target_user_ids: list[str],
    *,
    playlist_id: str,
    rating_keys: dict[str, str | None],
) -> dict[str, PlexTargetState]:
    """Reconcile the playlist on the admin account AND each target user.

    The owner ("admin") always gets their copy. Each target user is synced via
    ``switchUser`` and ISOLATED — one user's failure marks only that user
    ``failed`` and never aborts the others. Tracks are resolved once (the admin
    library scan) and reused for every target (ratingKeys are library-global).

    ``rating_keys`` maps a state-map key ("admin" or a uid) -> the playlist's
    recorded Plex ratingKey on that account, so each target reconciles against
    its OWN copy by identity; a missing/``None`` key means create fresh (the
    ``playlist_id`` summary marker still guards against title collisions).
    """
    if not (config.base_url and config.token):
        raise PlexNotConfigured("Plex is not configured.")
    try:
        admin = client.connect(config.base_url, config.token)
        section = client.music_section(admin, config.library_section)
        if section is None:
            raise _no_section_error(config.library_section)
        tracks, missing = resolve_ordered_tracks(section, specs)
    except PlexConnectionError:
        raise
    except Exception as exc:
        raise PlexConnectionError("Plex sync failed.") from exc

    def _reconcile_for(server: Any, key: str) -> PlexTargetState:
        return _reconcile_on(
            server, title, tracks, missing, playlist_id=playlist_id, rating_key=rating_keys.get(key)
        )

    # Bind each target id through a factory call so the closure captures the
    # current uid per iteration — sidesteps the late-binding-loop-variable trap.
    def _run_for(uid: str) -> Callable[[], PlexTargetState]:
        return lambda: _reconcile_for(admin.switchUser(uid), uid)

    results: dict[str, PlexTargetState] = {}
    results["admin"] = _safe_reconcile(lambda: _reconcile_for(admin, "admin"))
    for uid in target_user_ids:
        results[uid] = _safe_reconcile(_run_for(uid))
    return results


def _safe_reconcile(run: Callable[[], PlexTargetState]) -> PlexTargetState:
    try:
        return run()
    except Exception:
        return PlexTargetState(status="failed", error="Couldn't sync to this Plex account.")


def delete_playlist_on_targets(
    config: PlexConfig, rating_keys: dict[str, str | None]
) -> dict[str, str]:
    """Best-effort delete of a playlist from each target account, by ratingKey.

    ``rating_keys`` maps a state-map key -> the playlist's Plex ratingKey on that
    account (``"admin"`` is the owner's server; a uid is reached via
    ``admin.switchUser(uid)``). Deleting by the *recorded ratingKey* (not by
    title) keeps it precise — it can never remove a same-titled playlist that
    belongs to a different MusicDrop playlist or was made by hand in Plex, and it
    survives renames. A ``None`` ratingKey means nothing was ever synced there
    (-> "absent"). Each target is ISOLATED — one failure never aborts the others.
    Returns ``{target: "deleted" | "absent" | "failed"}``. Raises only
    ``PlexNotConfigured`` (no URL/token); a connect failure raises
    ``PlexConnectionError`` (callers wrap this best-effort)."""
    if not (config.base_url and config.token):
        raise PlexNotConfigured("Plex is not configured.")
    try:
        admin = client.connect(config.base_url, config.token)
    except Exception as exc:
        raise PlexConnectionError("Plex sync failed.") from exc
    return {target: _safe_delete(admin, target, rk) for target, rk in rating_keys.items()}


def _safe_delete(admin: Any, target: str, rating_key: str | None) -> str:
    if rating_key is None:  # never synced to this account — nothing to remove
        return "absent"
    try:
        server = admin if target == "admin" else admin.switchUser(target)
        existing = _find_by_rating_key(server, rating_key)
        if existing is None:
            return "absent"
        existing.delete()
        return "deleted"
    except Exception:  # one account failing must never abort the others
        return "failed"


def _find_by_rating_key(server: Any, rating_key: str) -> Any | None:
    for playlist in server.playlists():
        if str(playlist.ratingKey) == str(rating_key):
            return playlist
    return None
