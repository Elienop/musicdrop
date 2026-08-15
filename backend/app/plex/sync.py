"""Reconcile a MusicDrop playlist into a Plex account — IN PLACE.

Identifies the target Plex playlist by IDENTITY — never by bare title — and
updates it to the resolved, ordered tracks. MusicDrop playlist names are not
unique, so title matching would let two same-named playlists clobber each
other. The existing Plex playlist is found by (in order) the recorded
``rating_key``, else a ``MusicDrop-id:{playlist_id}`` marker stamped into the
Plex playlist's ``summary``; if neither matches we create a fresh one.

An existing playlist is UPDATED, never deleted-and-recreated: its ratingKey,
poster, and anything Plex hangs off the playlist object survive every sync,
and Plex clients holding the key keep working. Additions go in BEFORE removals
so the playlist never passes through empty; when the desired list resolves to
nothing at all we leave the Plex copy untouched (an empty resolve almost always
means resolution failed, not that the user emptied the playlist).

plexapi traps this module is written around (installed 4.18.2, playlist.py):
``removeItems``/``moveItem`` act on the FIRST cached row whose ratingKey
matches; ``items()`` is cached until ``reload()``; no mutator reloads. See
``_reconcile_items``.

Resolution is one library scan (``resolve_ordered_tracks``) reused for every
target (ratingKeys are server-global). The caller passes ``PlexTrackSpec``s
whose paths are already translated to Plex's view.

Exception contract — raises ONLY:
- ``PlexNotConfigured`` when no URL/token is set, or
- ``PlexConnectionError`` on ANY Plex API, network, or unexpected sync failure.

Time is stamped by the caller (this module has no clock): the returned
``PlexTargetState`` has ``synced_at = None`` for the caller to fill in.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.models.plex import MISSING_TRACKS_CAP, PlexMissingTrack, PlexTargetState
from app.plex import client as client  # explicit re-export: the patchable seam (sync.client)
from app.plex.config import PlexConfig
from app.plex.errors import PlexConnectionError, PlexNotConfigured
from app.plex.mapping import PlexTrackSpec, resolve_ordered_tracks

_SMART_ERROR = "This Plex playlist is a smart playlist; MusicDrop can't update it in place."


@dataclass(frozen=True)
class PlexArtwork:
    """The playlist's cover file to push as the Plex poster, plus its content
    hash — the reconcile re-uploads only when the hash differs from the one
    recorded on the target's prior state."""

    file: Path
    hash: str


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


def _find_by_rating_key(server: Any, rating_key: str) -> Any | None:
    for playlist in server.playlists():
        if str(playlist.ratingKey) == str(rating_key):
            return playlist
    return None


def _reconcile_items(playlist: Any, tracks: list[Any]) -> None:
    """Make ``playlist``'s rows equal ``tracks`` (multiset AND order), in place.

    Two strategies, chosen by whether any ratingKey repeats:

    * Unique keys (the common case): a diff. ``addItems`` the new ones (one
      call), ``removeItems`` the stale ones (one call — with unique keys the
      first-match row IS the only row, so the stale cache is harmless), then
      ``reload()`` ONCE so ``moveItem`` can see the added rows, and walk the
      desired order fixing each out-of-place position with one ``moveItem``
      (``after=None`` = front). At most one move per row.

    * Any duplicate key: append-then-remove-old. ``addItems(tracks)`` appends
      the desired rows in the desired order AFTER the old rows; then remove each
      OLD row with a ``reload()`` before every single ``removeItems`` — the
      first-match row for a key is then always the earliest surviving OLD row,
      never one of the freshly appended ones. Costs 2 calls per old row; only
      paid when a playlist actually holds a track twice.

    Either way additions land before removals, so the playlist never empties.
    """
    current_rows = list(playlist.items())
    current = [row.ratingKey for row in current_rows]
    desired = [track.ratingKey for track in tracks]
    if current == desired:
        return
    if len(set(current)) != len(current) or len(set(desired)) != len(desired):
        playlist.addItems(tracks)
        for row in current_rows:
            playlist.reload()
            playlist.removeItems([row])
        return
    by_key = {track.ratingKey: track for track in tracks}
    current_set, desired_set = set(current), set(desired)
    to_add = [track for track in tracks if track.ratingKey not in current_set]
    to_remove = [row for row in current_rows if row.ratingKey not in desired_set]
    if to_add:
        playlist.addItems(to_add)
    if to_remove:
        playlist.removeItems(to_remove)
    playlist.reload()
    order = [row.ratingKey for row in playlist.items()]
    for i, key in enumerate(desired):
        if order[i] == key:
            continue
        playlist.moveItem(by_key[key], after=by_key[desired[i - 1]] if i > 0 else None)
        order.remove(key)
        order.insert(i, key)


def _best_effort_stamp(playlist: Any, marker: str) -> None:
    # editSummary is a SEPARATE Plex PUT that can fail transiently. We still
    # return the real ratingKey, so the next sync re-finds this playlist by key
    # even if the marker never landed; failing the reconcile here would orphan a
    # perfectly good playlist.
    try:
        playlist.editSummary(marker)
    except Exception:
        pass


def _best_effort_poster(playlist: Any, artwork: PlexArtwork) -> bool:
    """Push the poster onto ``playlist``; True when Plex took it.

    uploadPoster is a SEPARATE Plex PUT that can fail transiently. The tracks
    are already synced, so a poster hiccup must never fail the reconcile — but
    the caller records the hash only when we return True, so the next sync
    retries instead of skipping the upload forever on an unchanged hash.
    """
    try:
        playlist.uploadPoster(filepath=str(artwork.file))
    except Exception:
        return False
    return True


def _reconcile_on(
    server: Any,
    title: str,
    tracks: list[Any],
    missing: list[PlexMissingTrack],
    *,
    playlist_id: str,
    prior: PlexTargetState | None,
    artwork: PlexArtwork | None = None,
) -> PlexTargetState:
    """Bring this MusicDrop playlist's copy on ``server`` up to date IN PLACE.

    Find the copy by IDENTITY (recorded ``rating_key`` first, else the
    ``playlist_id`` summary marker). Found -> update its rows, title, marker,
    and (only if the art changed) poster; the ratingKey is preserved. Not found
    -> create it (Plex refuses an empty create, so an empty resolve with no
    copy yields ``empty`` and no key). Found but nothing resolved -> touch
    NOTHING and keep the key (``empty``). Found but smart -> ``failed`` (Plex
    won't let us edit its rows), key preserved.

    Never matches by title — same-named playlists must not clobber each other.
    """
    prior_key = prior.rating_key if prior is not None else None
    prior_hash = prior.artwork_hash if prior is not None else None
    marker = _summary_marker(playlist_id)
    shown = missing[:MISSING_TRACKS_CAP]

    existing = _find_by_rating_key(server, prior_key) if prior_key is not None else None
    if existing is None:
        existing = _find_by_summary_marker(server, playlist_id)

    if existing is None:
        if not tracks:
            return PlexTargetState(
                rating_key=None, status="empty", missing=len(missing), missing_tracks=shown
            )
        playlist = server.createPlaylist(title, items=tracks)
        _best_effort_stamp(playlist, marker)
        pushed_hash: str | None = None
        if artwork is not None and _best_effort_poster(playlist, artwork):
            pushed_hash = artwork.hash
        return PlexTargetState(
            rating_key=str(playlist.ratingKey),
            status="ok" if not missing else "partial",
            missing=len(missing),
            missing_tracks=shown,
            artwork_hash=pushed_hash,
        )

    key = str(existing.ratingKey)
    if getattr(existing, "smart", False):
        return PlexTargetState(
            rating_key=key,
            status="failed",
            missing=len(missing),
            missing_tracks=shown,
            artwork_hash=prior_hash,
            error=_SMART_ERROR,
        )
    if not tracks:
        # An empty resolve almost always means resolution failed (section
        # renamed, paths moved, Plex mid-scan) — never destroy the copy over it.
        return PlexTargetState(
            rating_key=key,
            status="empty",
            missing=len(missing),
            missing_tracks=shown,
            artwork_hash=prior_hash,
        )
    _reconcile_items(existing, tracks)
    if getattr(existing, "title", None) != title:
        existing.editTitle(title)
    if marker not in (getattr(existing, "summary", "") or ""):
        _best_effort_stamp(existing, marker)
    pushed = prior_hash
    if artwork is not None and artwork.hash != prior_hash:
        if _best_effort_poster(existing, artwork):
            pushed = artwork.hash
    return PlexTargetState(
        rating_key=key,
        status="ok" if not missing else "partial",
        missing=len(missing),
        missing_tracks=shown,
        artwork_hash=pushed,
    )


def sync_playlist(
    config: PlexConfig,
    title: str,
    specs: list[PlexTrackSpec],
    *,
    playlist_id: str,
    prior: PlexTargetState | None = None,
    artwork: PlexArtwork | None = None,
) -> PlexTargetState:
    """Create/reconcile the playlist on the admin account only, by identity."""
    if not (config.base_url and config.token):
        raise PlexNotConfigured("Plex is not configured.")
    try:
        server = client.connect(config.base_url, config.token)
        section = client.music_section(server, config.library_section)
        if section is None:
            raise _no_section_error(config.library_section)
        resolution = resolve_ordered_tracks(section, specs)
        return _reconcile_on(
            server,
            title,
            resolution.tracks,
            resolution.missing,
            playlist_id=playlist_id,
            prior=prior,
            artwork=artwork,
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
    priors: dict[str, PlexTargetState],
    artwork: PlexArtwork | None = None,
) -> dict[str, PlexTargetState]:
    """Reconcile the playlist on the admin account AND each target user.

    The owner ("admin") always gets their copy. Each target user is synced via
    ``switchUser`` and ISOLATED — one user's failure marks only that user
    ``failed`` and never aborts the others. Tracks are resolved once (the admin
    library scan) and reused for every target (ratingKeys are library-global).

    ``priors`` maps a state-map key ("admin" or a uid) -> that target's recorded
    state (its ``rating_key`` and ``artwork_hash``), so each target reconciles
    against its OWN copy by identity; a missing entry means create fresh (the
    ``playlist_id`` summary marker still guards against title collisions).
    """
    if not (config.base_url and config.token):
        raise PlexNotConfigured("Plex is not configured.")
    try:
        admin = client.connect(config.base_url, config.token)
        section = client.music_section(admin, config.library_section)
        if section is None:
            raise _no_section_error(config.library_section)
        resolution = resolve_ordered_tracks(section, specs)
    except PlexConnectionError:
        raise
    except Exception as exc:
        raise PlexConnectionError("Plex sync failed.") from exc

    def _reconcile_for(server: Any, key: str) -> PlexTargetState:
        return _reconcile_on(
            server,
            title,
            resolution.tracks,
            resolution.missing,
            playlist_id=playlist_id,
            prior=priors.get(key),
            artwork=artwork,
        )

    # Bind each target id through a factory call so the closure captures the
    # current uid per iteration — sidesteps the late-binding-loop-variable trap.
    def _run_for(uid: str) -> Callable[[], PlexTargetState]:
        return lambda: _reconcile_for(admin.switchUser(uid), uid)

    results: dict[str, PlexTargetState] = {}
    results["admin"] = _safe_reconcile(lambda: _reconcile_for(admin, "admin"), priors.get("admin"))
    for uid in target_user_ids:
        results[uid] = _safe_reconcile(_run_for(uid), priors.get(uid))
    return results


def _safe_reconcile(
    run: Callable[[], PlexTargetState], prior: PlexTargetState | None = None
) -> PlexTargetState:
    try:
        return run()
    except Exception:
        # Carry the prior ratingKey (and poster hash) into the failed state: the
        # caller replaces the WHOLE state map with what we return, so recording
        # None here would ERASE a known key and orphan the still-existing Plex
        # copy (a later delete/de-target short-circuits on a None key).
        return PlexTargetState(
            rating_key=prior.rating_key if prior is not None else None,
            status="failed",
            artwork_hash=prior.artwork_hash if prior is not None else None,
            error="Couldn't sync to this Plex account.",
        )


def delete_playlist_on_targets(
    config: PlexConfig, rating_keys: dict[str, str | None], *, playlist_id: str
) -> dict[str, str]:
    """Best-effort delete of a playlist from each target account, by ratingKey.

    ``rating_keys`` maps a state-map key -> the playlist's Plex ratingKey on that
    account (``"admin"`` is the owner's server; a uid is reached via
    ``admin.switchUser(uid)``). Deleting by the *recorded ratingKey* (not by
    title) keeps it precise — it can never remove a same-titled playlist that
    belongs to a different MusicDrop playlist or was made by hand in Plex, and it
    survives renames. If the recorded ratingKey no longer resolves (a Plex DB
    rebuild reassigns ratingKeys), OR is ``None`` (never recorded, or erased by a
    transient sync failure), we fall back to the ``playlist_id`` summary marker so
    a missing key doesn't orphan an existing copy; only a marker miss is "absent".
    Each target is ISOLATED — one
    failure never aborts the others. Returns
    ``{target: "deleted" | "absent" | "failed"}``. Raises only
    ``PlexNotConfigured`` (no URL/token); a connect failure raises
    ``PlexConnectionError`` (callers wrap this best-effort)."""
    if not (config.base_url and config.token):
        raise PlexNotConfigured("Plex is not configured.")
    try:
        admin = client.connect(config.base_url, config.token)
    except Exception as exc:
        raise PlexConnectionError("Plex sync failed.") from exc
    return {
        target: _safe_delete(admin, target, rk, playlist_id=playlist_id)
        for target, rk in rating_keys.items()
    }


def _safe_delete(admin: Any, target: str, rating_key: str | None, *, playlist_id: str) -> str:
    try:
        server = admin if target == "admin" else admin.switchUser(target)
        existing = _find_by_rating_key(server, rating_key) if rating_key is not None else None
        if existing is None:
            # No usable ratingKey — either it went stale (a Plex DB rebuild), or it
            # was erased by a transient reconcile failure before it could be carried
            # forward. Re-find our copy by the id marker so we still delete it rather
            # than short-circuiting to "absent" and orphaning it on Plex forever.
            existing = _find_by_summary_marker(server, playlist_id)
        if existing is None:
            return "absent"
        existing.delete()
        return "deleted"
    except Exception:  # one account failing must never abort the others
        return "failed"
