"""Reconcile a MusicDrop playlist into a Plex account — IN PLACE.

Identifies the target Plex playlist by IDENTITY — never by bare title — and
updates it to the resolved, ordered tracks. MusicDrop playlist names are not
unique, so title matching would let two same-named playlists clobber each
other. The existing Plex playlist is found by (in order) the
``MusicDrop-id:{playlist_id}`` marker stamped into the Plex playlist's
``summary``, else the recorded ``rating_key`` — but never a keyed playlist
wearing SOMEONE ELSE'S marker, which is what a rebuilt Plex DB's reassigned
ratingKeys produce. If neither matches we create a fresh one.

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

import bisect
from collections import Counter
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
_NOT_APPLIED_ERROR = "Plex did not apply the playlist changes."
_ACCOUNT_ERROR = "Couldn't sync to this Plex account."
_MARKER_PREFIX = "MusicDrop-id:"


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
    return f"{_MARKER_PREFIX}{playlist_id}"


def _summary_of(playlist: Any) -> str:
    """``playlist``'s summary, read WITHOUT a hidden Plex round trip.

    ``Playlist._loadData`` stores ``data.attrib.get('summary')`` — ``None`` when
    the listing carries no summary (``playlist.py:71``) — and a playlist built
    from ``/playlists`` is PARTIAL (its ``key`` is ``/playlists/<id>``, its
    ``_initpath`` ``/playlists``, so ``isFullObject`` is False:
    ``base.py:692-702``). ``PlexPartialObject.__getattribute__`` turns a ``None``
    read on a partial object into ``self._reload()`` — a full HTTP GET —
    whenever ``_autoReload`` is on (default True, ``base.py:115``) and the name
    is not one of ``_DONT_RELOAD_FOR_KEYS`` = {centroid, key, sourceURI}, which
    ``summary`` is not (``base.py:650-668``). Identity lookup reads EVERY
    playlist's summary on EVERY sync for EVERY target, so a plain attribute read
    costs one GET per summary-less playlist each time. ``__dict__`` starts with
    an underscore and so returns at ``base.py:656`` before any of that.
    """
    return str(playlist.__dict__.get("summary") or "")


def _find_our_playlist(server: Any, playlist_id: str, rating_key: str | None) -> Any | None:
    """Our copy on ``server``, by the most durable identity FIRST.

    The ONE identity rule, shared by the reconcile and the delete: whatever a
    sync would UPDATE in place is exactly what a delete may REMOVE. Two copies of
    this rule would drift, and the failure mode of a drifted delete is a
    destroyed playlist.

    The ``MusicDrop-id`` marker outranks the recorded ``rating_key``: a Plex DB
    rebuild reassigns ratingKeys, so a recorded key can come to resolve to a
    playlist that is not ours. Adopting it would rewrite that playlist in place
    and then RE-RECORD its key, so every later sync keeps rewriting a stranger's
    playlist while our own copy is orphaned — a permanent mis-binding, not a
    one-off. Marker-first also costs nothing: both lookups read the same
    ``server.playlists()`` listing.

    A key match with NO marker at all is still ours to adopt — that is the copy
    whose stamp PUT failed transiently, and re-stamping it is the recovery path.
    Only a marker naming a DIFFERENT playlist is disqualifying.
    """
    marker = _summary_marker(playlist_id)
    playlists = list(server.playlists())
    marked = [playlist for playlist in playlists if marker in _summary_of(playlist)]
    if marked:
        # Two copies can both wear our marker — a listing call that failed
        # mid-sync leaves a stamped playlist behind and the next sync creates
        # another. The recorded key then decides, so the copy a sync UPDATES is
        # the copy a delete REMOVES; without it the two can pick different ones
        # and the delete destroys the copy Plex clients are not holding.
        for playlist in marked:
            if rating_key is not None and str(playlist.ratingKey) == str(rating_key):
                return playlist
        return marked[0]
    if rating_key is None:
        return None
    for playlist in playlists:
        if str(playlist.ratingKey) != str(rating_key):
            continue
        # Our own marker was ruled out above, so any marker here is someone else's.
        return None if _MARKER_PREFIX in _summary_of(playlist) else playlist
    return None


def _unique_key_chunks(tracks: list[Any]) -> list[list[Any]]:
    """Split ``tracks`` into the fewest ORDER-PRESERVING runs that each carry a
    ratingKey at most once.

    plexapi comma-joins one ``addItems`` into a single ``/library/metadata/2,1,2``
    uri (``playlist.py:255-262``), and whether PMS honours a repeated id inside
    one uri cannot be known from the client — if it de-dups we would silently
    build the wrong playlist and still report ``ok``. So we never ask. Splitting
    at each repeat keeps every call unique while the concatenation stays in
    desired order, and costs one call per repeat DEPTH (2 for a playlist holding
    a track twice) rather than one per track.
    """
    chunks: list[list[Any]] = []
    current: list[Any] = []
    seen: set[Any] = set()
    for track in tracks:
        if track.ratingKey in seen:
            chunks.append(current)
            current, seen = [], set()
        current.append(track)
        seen.add(track.ratingKey)
    if current:
        chunks.append(current)
    return chunks


def _first_occurrences(tracks: list[Any]) -> list[Any]:
    """``tracks`` with repeats dropped, first occurrence kept, order preserved."""
    seen: set[Any] = set()
    unique: list[Any] = []
    for track in tracks:
        if track.ratingKey in seen:
            continue
        seen.add(track.ratingKey)
        unique.append(track)
    return unique


def _stable_rows(positions: list[int]) -> set[int]:
    """Indices of one longest strictly increasing subsequence of ``positions``.

    Those rows are already in the right order relative to each other, so they
    never have to move — every other row costs exactly one ``moveItem``, which
    is the minimum.
    """
    tail_values: list[int] = []
    tail_index: list[int] = []
    parent: list[int] = [-1] * len(positions)
    for i, position in enumerate(positions):
        slot = bisect.bisect_left(tail_values, position)
        if slot > 0:
            parent[i] = tail_index[slot - 1]
        if slot == len(tail_values):
            tail_values.append(position)
            tail_index.append(i)
        else:
            tail_values[slot] = position
            tail_index[slot] = i
    keep: set[int] = set()
    i = tail_index[-1] if tail_index else -1
    while i >= 0:
        keep.add(i)
        i = parent[i]
    return keep


def _reconcile_items(playlist: Any, tracks: list[Any]) -> None:
    """Make ``playlist``'s rows equal ``tracks`` (multiset AND order), in place.

    Two strategies, chosen by whether any ratingKey repeats:

    * Unique keys (the common case): a diff. ``addItems`` the new ones (one
      call), ``removeItems`` the stale ones (one call — with unique keys the
      first-match row IS the only row, so the stale cache is harmless), then
      ``reload()`` ONCE so ``moveItem`` can see the added rows, and move only the
      rows that actually moved: the longest increasing subsequence of the current
      positions is already in the right relative order and stays put, and each
      remaining row costs one ``moveItem`` after its predecessor (``after=None``
      = front). Dragging one track of a 500-row playlist is 1 PUT, not 499.

    * Any duplicate key: append-then-remove-old. The desired rows are appended in
      order AFTER the old ones (in unique-key chunks, see ``_unique_key_chunks``);
      one ``reload()`` then confirms Plex really holds old + desired before a
      single row is removed; then each OLD row is removed with a ``reload()``
      before every further ``removeItems`` — the first-match row for a key is
      then always the earliest surviving OLD row, never one of the freshly
      appended ones. Costs 2 calls per old row; only paid when a playlist
      actually holds a track twice.

    Either way additions land before removals, so the playlist never empties.
    """
    current_rows = list(playlist.items())
    current = [row.ratingKey for row in current_rows]
    desired = [track.ratingKey for track in tracks]
    if current == desired:
        return
    if len(set(current)) != len(current) or len(set(desired)) != len(desired):
        for chunk in _unique_key_chunks(tracks):
            playlist.addItems(chunk)
        # This path removes EVERY old row, so the appends have to be confirmed
        # before the first removal: a PUT Plex answered but ignored would
        # otherwise leave the playlist EMPTY and the sync would report "ok". The
        # reload is the one the removal loop needed anyway (moved out of it), so
        # the check is free.
        # Compared as a LIST, not a multiset: the removals below take the FIRST
        # cached row per key, which is only the old row if Plex appended the new
        # rows AFTER the old ones in the order we sent — placement matters here
        # as much as completeness (a complete-but-reordered result would strip
        # the wrong rows).
        playlist.reload()
        if [row.ratingKey for row in playlist.items()] != current + desired:
            raise PlexConnectionError(_NOT_APPLIED_ERROR)
        for position, row in enumerate(current_rows):
            if position:  # the verification reload above already refreshed the cache
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
    if Counter(order) != Counter(desired):
        # Plex took the PUTs and the rows still are not what we asked for. Walking
        # the desired order against this list would index past its end and surface
        # as the generic "Plex sync failed."; say what actually happened instead.
        raise PlexConnectionError(_NOT_APPLIED_ERROR)
    at = {key: i for i, key in enumerate(order)}
    keep = _stable_rows([at[key] for key in desired])
    for i, key in enumerate(desired):
        # After step i, desired[:i+1] are in the right relative order and still
        # precede every yet-unplaced kept row, so placing each mover directly
        # after its predecessor lands the whole list in order (induction on i).
        if i in keep:
            continue
        playlist.moveItem(by_key[key], after=by_key[desired[i - 1]] if i > 0 else None)


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

    Find the copy by IDENTITY (the ``playlist_id`` summary marker first, else the
    recorded ``rating_key`` — see ``_find_our_playlist``). Found -> update its
    rows, title, marker, and (only if the art changed) poster; the ratingKey is
    preserved. Not found -> create it (Plex refuses an empty create, so an empty
    resolve with no copy yields ``empty`` and no key). Found but nothing resolved
    -> touch NOTHING and keep the key (``empty``). Found but smart -> ``failed``
    (Plex won't let us edit its rows), key preserved.

    Never matches by title — same-named playlists must not clobber each other.
    """
    prior_key = prior.rating_key if prior is not None else None
    prior_hash = prior.artwork_hash if prior is not None else None
    marker = _summary_marker(playlist_id)
    shown = missing[:MISSING_TRACKS_CAP]

    existing = _find_our_playlist(server, playlist_id, prior_key)

    if existing is None:
        if not tracks:
            return PlexTargetState(
                rating_key=None, status="empty", missing=len(missing), missing_tracks=shown
            )
        # Create from the distinct keys, then let the reconcile append the
        # repeats — one create uri must not carry a key twice either. The marker
        # is stamped BEFORE that second step, so a reconcile failure leaves a
        # findable playlist rather than an orphan we would duplicate next sync.
        distinct = _first_occurrences(tracks)
        playlist = server.createPlaylist(title, items=distinct)
        _best_effort_stamp(playlist, marker)
        if len(distinct) != len(tracks):
            _reconcile_items(playlist, tracks)
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
    # Deliberately a PLAIN attribute read: on a partial listing object plexapi
    # auto-reloads when it sees None here (the same trap _summary_of avoids) —
    # but for `smart` that refetch makes the value CORRECT, and it is one GET on
    # one playlist per sync. Reading it via __dict__ would treat a smart
    # playlist as normal, and the precise error below would degrade to the
    # generic one when addItems raised.
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
    if marker not in _summary_of(existing):
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
    except Exception as exc:
        # PlexConnectionError is OUR type, raised with text already written for
        # the user (``_NOT_APPLIED_ERROR``), so it rides through: this catch is
        # every target's only exit, and flattening it would tell the user their
        # account was unreachable when the connection was fine and Plex just
        # refused the change. Anything else is a raw plexapi/requests failure
        # whose message is not ours to show.
        error = str(exc) if isinstance(exc, PlexConnectionError) else _ACCOUNT_ERROR
        # Carry the prior ratingKey (and poster hash) into the failed state: the
        # caller replaces the WHOLE state map with what we return, so recording
        # None here would ERASE a known key and orphan the still-existing Plex
        # copy (a later delete/de-target short-circuits on a None key).
        return PlexTargetState(
            rating_key=prior.rating_key if prior is not None else None,
            status="failed",
            artwork_hash=prior.artwork_hash if prior is not None else None,
            error=error,
        )


def delete_playlist_on_targets(
    config: PlexConfig, rating_keys: dict[str, str | None], *, playlist_id: str
) -> dict[str, str]:
    """Best-effort delete of a playlist from each target account, by IDENTITY.

    ``rating_keys`` maps a state-map key -> the playlist's Plex ratingKey on that
    account (``"admin"`` is the owner's server; a uid is reached via
    ``admin.switchUser(uid)``). The copy to remove is found by the SAME rule the
    reconcile uses — ``_find_our_playlist``: the ``playlist_id`` marker first, the
    recorded ratingKey only as a fallback, and never a keyed playlist wearing
    another MusicDrop playlist's marker. Never by title. That is what keeps a
    delete precise across the two ways identity goes wrong: a rename (the key and
    marker both survive it) and a Plex DB rebuild (which reassigns ratingKeys, so
    the recorded key can come to point at somebody else's playlist — deleting
    that would destroy a stranger's copy and orphan ours). A ``None`` key, or one
    that no longer resolves, still finds our copy by marker; only when neither
    identity matches is the answer "absent". Each target is ISOLATED — one
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
        # One rule, one implementation: whatever the reconcile would UPDATE is
        # exactly what a delete may REMOVE. A key with no marker is still ours
        # (a stamp PUT that failed); a key wearing someone else's marker is not.
        existing = _find_our_playlist(server, playlist_id, rating_key)
        if existing is None:
            return "absent"
        existing.delete()
        return "deleted"
    except Exception:  # one account failing must never abort the others
        return "failed"
