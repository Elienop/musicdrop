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
matches; ``items()`` is cached until ``reload()``; no mutator reloads. A
playlist that lists one track TWICE has a second row no first-match call can
name, so that case addresses rows by ``playlistItemID`` instead. See
``_reconcile_items``.

What no client can know about a Plex server is whether it will hold one track
on two rows at all: ``addItems`` is answered 200 whether the row appeared or
was collapsed into the one already there. So the duplicate path never assumes —
it asks for one repeat, reloads, and reads the answer off the row count. When
the server refuses, the sync still lands the distinct playlist and reports each
occurrence Plex would not hold as a ``duplicate_collapsed`` miss (status
``partial``): the copy is right about everything it says it is, and says what
it is short.

Resolution is one library scan (``resolve_ordered_tracks``) reused for every
target (ratingKeys are server-global). The caller passes ``PlexTrackSpec``s
whose paths are already translated to Plex's view. The whole ``PlexResolution``
travels down, not just its tracks: each target's recorded state carries the
per-rung tally (``matched_by``) beside the miss count, because "28 ok" and "28
matched, none of them by path" are the same status and very different news.

Exception contract — raises ONLY:
- ``PlexNotConfigured`` when no URL/token is set, or
- ``PlexConnectionError`` on ANY Plex API, network, or unexpected sync failure.

Time is stamped by the caller (this module has no clock): the returned
``PlexTargetState`` has ``synced_at = None`` for the caller to fill in.
"""

from __future__ import annotations

import bisect
from collections import Counter, defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.models.plex import MISSING_TRACKS_CAP, PlexMissingTrack, PlexTargetState
from app.plex import client as client  # explicit re-export: the patchable seam (sync.client)
from app.plex.config import PlexConfig
from app.plex.errors import PlexConnectionError, PlexNotConfigured
from app.plex.mapping import (
    PlexResolution,
    PlexTrackSpec,
    missing_from_spec,
    resolve_ordered_tracks,
)

_SMART_ERROR = "This Plex playlist is a smart playlist; MusicDrop can't update it in place."
_NOT_APPLIED_ERROR = "Plex did not apply the playlist changes."
_ACCOUNT_ERROR = "Couldn't sync to this Plex account."
_NOT_CONFIGURED_ERROR = "Plex is not configured."
_SYNC_FAILED_ERROR = "Plex sync failed."
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


def _repeats_a_key(keys: list[Any]) -> bool:
    """True when ``keys`` holds a ratingKey twice.

    ``_reconcile_items`` takes the duplicate strategy when EITHER the current
    rows or the desired rows repeat a key — plexapi resolves a key to its first
    row, so a repeated key on either side rules out the diff-and-move path.
    """
    return len(set(keys)) != len(keys)


@dataclass(frozen=True)
class _PlaylistRow:
    """One row of a Plex playlist: WHICH track (``key``, its ratingKey) and
    WHICH ROW (``row_id``, its ``playlistItemID``).

    That distinction is the whole duplicate story. plexapi names a row by its
    track's ratingKey and acts on the first match, so on a playlist holding one
    track twice the second row cannot be removed, moved, or moved after — while
    the row id names exactly one row and always has.
    """

    key: Any
    row_id: int


def _rows_of(items: list[Any]) -> list[_PlaylistRow]:
    """plexapi's playlist rows as ours.

    ``playlistItemID`` is a plain attribute plexapi sets on every object built
    from a playlist's ``/items`` response (``base.py:864``), so reading it is
    free here — unlike the partial-object reads ``_summary_of`` goes out of its
    way to avoid, a row fetched FROM a playlist always carries one.
    """
    return [_PlaylistRow(key=row.ratingKey, row_id=row.playlistItemID) for row in items]


def _reloaded_rows(playlist: Any) -> list[_PlaylistRow]:
    """The rows Plex holds NOW — the only thing worth verifying against."""
    playlist.reload()
    return _rows_of(list(playlist.items()))


def _row_request(playlist: Any, path: str, verb: str) -> None:
    """Issue one ROW-PRECISE request, exactly the way plexapi issues its own.

    ``removeItems`` and ``moveItem`` build ``{playlist.key}/items/{id}`` and
    ``{playlist.key}/items/{id}/move?after={id}`` and hand them to
    ``server.query(key, method=server._session.<verb>)`` (``playlist.py:285-317``).
    The one thing that changes here is WHICH row the id names: plexapi resolves
    it through ``_getPlaylistItemID``, a first-match on ratingKey, and no public
    call can be aimed at the second row of a duplicated track. Same endpoints,
    same verbs, same session — the playlistItemID is simply ours to choose.
    """
    server = playlist._server
    server.query(f"{playlist.key}{path}", method=getattr(server._session, verb))


def _remove_row(playlist: Any, row: _PlaylistRow) -> None:
    _row_request(playlist, f"/items/{row.row_id}", "delete")


def _move_row(playlist: Any, row: _PlaylistRow, after: _PlaylistRow | None) -> None:
    """Place ``row`` directly after ``after`` — at the front when it is None."""
    tail = "" if after is None else f"?after={after.row_id}"
    _row_request(playlist, f"/items/{row.row_id}/move{tail}", "put")


def _surplus_rows(rows: list[_PlaylistRow], want: Counter[Any]) -> list[_PlaylistRow]:
    """Every row beyond what the desired list asks for — a key it does not want
    at all, and the later rows of one it wants fewer times than the playlist
    holds it.

    WHICH rows go is free, since two rows of a track are interchangeable, so the
    EARLIEST are kept: that leaves the longest stretch of the existing order in
    place for the ordering pass to build on.
    """
    seen: Counter[Any] = Counter()
    surplus: list[_PlaylistRow] = []
    for row in rows:
        seen[row.key] += 1
        if seen[row.key] > want[row.key]:
            surplus.append(row)
    return surplus


def _fill_and_trim(
    playlist: Any, rows: list[_PlaylistRow], tracks: list[Any]
) -> list[_PlaylistRow]:
    """Get the rows to "every desired track present, nothing surplus" — the
    layer that asks Plex no question it cannot answer.

    Every desired key the playlist does not hold at all goes in ONE ``addItems``
    whose uri names each key once and none of them already present: the very
    call the unique-key path makes, the one proven against a real server. Then
    surplus rows are dropped BY ROW ID, so a key held three times loses exactly
    two rows without the reload-between-removals dance a first-match removal
    needs — and a copy left holding junk by an earlier failed attempt reconciles
    down in one pass.

    Additions land before removals AND are confirmed present first, per the
    module's never-destroy rule: a PUT Plex answered but ignored must never be
    followed by a removal. Returns the rows Plex holds afterwards — reloaded, so
    every later step reasons about the server rather than about what we asked
    for. A removal Plex ignored is not checked here: it leaves a row the desired
    list does not want, which is exactly what ``_order_rows`` refuses.
    """
    want = Counter(track.ratingKey for track in tracks)
    have = Counter(row.key for row in rows)
    absent = [track for track in _first_occurrences(tracks) if not have[track.ratingKey]]
    if absent:
        playlist.addItems(absent)
        rows = _reloaded_rows(playlist)
        if Counter(row.key for row in rows) != have + Counter(t.ratingKey for t in absent):
            raise PlexConnectionError(_NOT_APPLIED_ERROR)
    surplus = _surplus_rows(rows, want)
    if not surplus:
        return rows
    for row in surplus:
        _remove_row(playlist, row)
    return _reloaded_rows(playlist)


def _top_up_repeats(
    playlist: Any, rows: list[_PlaylistRow], tracks: list[Any]
) -> tuple[list[_PlaylistRow], list[int]]:
    """Ask Plex to hold a track a SECOND time, one occurrence at a time, and
    learn from what it does.

    Whether a server will is not knowable from the client — plexapi sends one
    comma-joined uri and gets a 200 whether the row appeared or was collapsed
    into the one already there — so this never assumes. It appends one
    occurrence, reloads, and reads the answer off the count: grown by that key,
    the server honours duplicates and we ask for the next one; unchanged, it
    collapses them, so we stop asking rather than send calls whose answer we now
    know. Anything else means Plex applied something we did not ask for, which
    is not a state to keep building on.

    Returns the rows Plex holds now and the DESIRED-LIST INDICES of the
    occurrences that did not land. The LATER occurrence is the one reported: the
    earlier ones keep the rows, so a playlist listing a track twice still holds
    it once, in its first position.
    """
    available = Counter(row.key for row in rows)
    collapsed: list[int] = []
    refused = False
    for index, track in enumerate(tracks):
        key = track.ratingKey
        if available[key]:
            available[key] -= 1
            continue
        if refused:
            collapsed.append(index)
            continue
        before = Counter(row.key for row in rows)
        playlist.addItems([track])
        rows = _reloaded_rows(playlist)
        after = Counter(row.key for row in rows)
        if after == before + Counter([key]):
            continue
        if after != before:
            raise PlexConnectionError(_NOT_APPLIED_ERROR)
        refused = True
        collapsed.append(index)
    return rows, collapsed


def _rows_for(rows: list[_PlaylistRow], target: list[Any]) -> list[_PlaylistRow]:
    """One row per target position — the earliest still-unused row of that key.

    Precondition: the rows hold exactly ``target`` as a multiset (the caller's
    ``Counter`` check). Rows of one key are interchangeable, so any assignment
    lands the right sequence; taking them in current order keeps those rows in
    their existing relative order, which is the assignment that leaves the most
    of the list already in place.
    """
    pool: dict[Any, deque[_PlaylistRow]] = defaultdict(deque)
    for row in rows:
        pool[row.key].append(row)
    return [pool[key].popleft() for key in target]


def _order_rows(playlist: Any, rows: list[_PlaylistRow], target: list[Any]) -> None:
    """Move rows BY ROW ID until the playlist reads ``target``, then prove it did.

    ``moveItem`` cannot do this once a key sits on two rows — it resolves the row
    AND the anchor by first ratingKey match — so every move names its
    playlistItemID. Only the rows outside one longest increasing subsequence
    move, which is the minimum, and each lands directly after its predecessor:
    after step i the first i+1 rows are ``target[:i+1]`` (induction on i).

    ``rows`` is what Plex last told us it holds, so the count check is what
    catches a removal Plex answered and ignored — and it comes BEFORE the moves:
    the closing check would refuse that playlist too, but only after reordering
    a copy we already knew was wrong.

    That closing reload is what this path promises: the rows Plex REALLY holds,
    compared as a LIST, or the sync says Plex did not apply the change.
    """
    if Counter(row.key for row in rows) != Counter(target):
        raise PlexConnectionError(_NOT_APPLIED_ERROR)
    if [row.key for row in rows] == target:
        return  # `rows` came from Plex, so equality here IS the verification
    placed = _rows_for(rows, target)
    at = {row.row_id: index for index, row in enumerate(rows)}
    keep = _stable_rows([at[row.row_id] for row in placed])
    for index, row in enumerate(placed):
        if index in keep:
            continue
        _move_row(playlist, row, placed[index - 1] if index else None)
    if [row.key for row in _reloaded_rows(playlist)] != target:
        raise PlexConnectionError(_NOT_APPLIED_ERROR)


def _reconcile_by_row(playlist: Any, rows: list[_PlaylistRow], tracks: list[Any]) -> list[int]:
    """The duplicate strategy: reconcile the MULTISET in layers, naming rows.

    A key on two rows defeats every first-match call plexapi offers, and the
    question "will this server hold it twice?" has no answer until it is asked.
    So the layers are ordered by how much they have to assume:

    1. the DISTINCT layer — add the keys the playlist is missing, drop the
       surplus rows (``_fill_and_trim``). Nothing here is a repeat, so it is the
       reconcile the unique path already proves against the owner's server.
    2. the REPEATS — one occurrence at a time, each one confirmed or refused by
       the row count (``_top_up_repeats``). Refused occurrences come back as
       indices and are reported, never silently dropped.
    3. the ORDER — one move per row that is genuinely out of place, by row id,
       verified against a reload (``_order_rows``).

    Returns the desired-list indices Plex would not hold a second time.
    """
    rows = _fill_and_trim(playlist, rows, tracks)
    rows, collapsed = _top_up_repeats(playlist, rows, tracks)
    dropped = set(collapsed)
    target = [track.ratingKey for index, track in enumerate(tracks) if index not in dropped]
    _order_rows(playlist, rows, target)
    return collapsed


def _move_into_order(playlist: Any, tracks: list[Any], order: list[Any]) -> None:
    """Issue one ``moveItem`` per row that is genuinely out of place — the minimum.

    ``order`` is the playlist's current key order; the rows of one longest
    increasing subsequence of the desired rows' positions in it are already
    correct relative to each other and stay put.

    Precondition: ``order`` already holds exactly the desired keys — the
    caller's ``Counter`` check is what guarantees ``at[key]`` resolves.
    """
    by_key = {track.ratingKey: track for track in tracks}
    desired = [track.ratingKey for track in tracks]
    at = {key: i for i, key in enumerate(order)}
    keep = _stable_rows([at[key] for key in desired])
    for i, key in enumerate(desired):
        # After step i, desired[:i+1] are in the right relative order and still
        # precede every yet-unplaced kept row, so placing each mover directly
        # after its predecessor lands the whole list in order (induction on i).
        if i in keep:
            continue
        playlist.moveItem(by_key[key], after=by_key[desired[i - 1]] if i > 0 else None)


def _diff_then_reorder(playlist: Any, current_rows: list[Any], tracks: list[Any]) -> None:
    """Add the new rows, remove the stale ones, then move what is out of place.

    The unique-key strategy: every key names exactly one row, so plexapi's
    first-match removal is unambiguous and the whole diff fits in one
    ``addItems`` plus one ``removeItems``.
    """
    current_set = {row.ratingKey for row in current_rows}
    desired = [track.ratingKey for track in tracks]
    desired_set = set(desired)
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
    _move_into_order(playlist, tracks, order)


def _reconcile_items(playlist: Any, tracks: list[Any]) -> list[int]:
    """Make ``playlist``'s rows equal ``tracks`` (multiset AND order), in place.

    Two strategies, chosen by whether any ratingKey repeats:

    * Unique keys (the common case): a diff — ``_diff_then_reorder``.
      ``addItems`` the new ones (one call), ``removeItems`` the stale ones (one
      call — with unique keys the first-match row IS the only row, so the stale
      cache is harmless), then ``reload()`` ONCE so ``moveItem`` can see the
      added rows, and move only the rows that actually moved (``_move_into_order``):
      the longest increasing subsequence of the current positions is already in
      the right relative order and stays put, and each remaining row costs one
      ``moveItem`` after its predecessor (``after=None`` = front). Dragging one
      track of a 500-row playlist is 1 PUT, not 499.

    * Any duplicate key: reconcile the multiset by ROW — ``_reconcile_by_row``.
      Distinct keys first, then the repeats one at a time (whether Plex holds a
      track twice is the server's answer to give, not ours to guess), then the
      order, every row named by its playlistItemID because a repeated key
      defeats plexapi's first-match addressing.

    Either way additions land before removals, so the playlist never empties.

    Returns the INDICES into ``tracks`` of occurrences Plex refused to hold a
    second time — empty for every unique playlist. Indices, because ``tracks``
    is the caller's resolved matches in order, so ``resolution.matches[i]`` is
    the row that collapsed and knows which library item it came from.
    """
    current_rows = list(playlist.items())
    current = [row.ratingKey for row in current_rows]
    desired = [track.ratingKey for track in tracks]
    if current == desired:
        return []
    if _repeats_a_key(current) or _repeats_a_key(desired):
        return _reconcile_by_row(playlist, _rows_of(current_rows), tracks)
    _diff_then_reorder(playlist, current_rows, tracks)
    return []


def _misses(resolution: PlexResolution, collapsed: list[int]) -> list[PlexMissingTrack]:
    """Every playlist row Plex does not fully hold: the ones that resolved to
    nothing, then the occurrences it would not hold a second time.

    Both are the same news to a user looking at a row — "this is not on Plex" —
    and both belong in one count, so a playlist listing a track twice on a
    server that keeps it once reads ``partial`` and names the row, instead of
    ``ok`` about a copy that is a row short.
    """
    return resolution.missing + [
        missing_from_spec(resolution.matches[index].spec, "duplicate_collapsed")
        for index in collapsed
    ]


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


def _poster_hash_after_update(
    playlist: Any, artwork: PlexArtwork | None, prior_hash: str | None
) -> str | None:
    """The artwork hash to RECORD for an existing copy: the new one only once
    Plex has actually taken a changed poster, else the one already recorded.

    Keeping the prior hash on a failed upload is what makes the next sync retry
    instead of skipping the upload forever on an unchanged hash.
    """
    if artwork is None or artwork.hash == prior_hash:
        return prior_hash
    return artwork.hash if _best_effort_poster(playlist, artwork) else prior_hash


def _create_our_playlist(
    server: Any,
    title: str,
    resolution: PlexResolution,
    *,
    marker: str,
    artwork: PlexArtwork | None,
) -> PlexTargetState:
    """The state after creating a FRESH Plex copy — identity matched nothing.

    Plex refuses an empty create, so an empty resolve yields ``empty`` and no
    key rather than a playlist.
    """
    tracks, missing = resolution.tracks, resolution.missing
    shown = missing[:MISSING_TRACKS_CAP]
    if not tracks:
        return PlexTargetState(
            rating_key=None,
            status="empty",
            missing=len(missing),
            missing_tracks=shown,
            matched_by=resolution.matched_by,
        )
    # Create from the distinct keys, then let the reconcile append the
    # repeats — one create uri must not carry a key twice either. The marker
    # is stamped BEFORE that second step, so a reconcile failure leaves a
    # findable playlist rather than an orphan we would duplicate next sync.
    distinct = _first_occurrences(tracks)
    playlist = server.createPlaylist(title, items=distinct)
    _best_effort_stamp(playlist, marker)
    collapsed = _reconcile_items(playlist, tracks) if len(distinct) != len(tracks) else []
    pushed_hash: str | None = None
    if artwork is not None and _best_effort_poster(playlist, artwork):
        pushed_hash = artwork.hash
    # A fresh copy on a server that will not hold a track twice is short those
    # rows from its first minute, and says so exactly as an updated one does.
    misses = _misses(resolution, collapsed)
    return PlexTargetState(
        rating_key=str(playlist.ratingKey),
        status="ok" if not misses else "partial",
        missing=len(misses),
        missing_tracks=misses[:MISSING_TRACKS_CAP],
        matched_by=resolution.matched_by,
        artwork_hash=pushed_hash,
    )


def _update_our_playlist(
    existing: Any,
    title: str,
    resolution: PlexResolution,
    *,
    marker: str,
    prior_hash: str | None,
    artwork: PlexArtwork | None,
) -> PlexTargetState:
    """The state after updating the copy we already own, IN PLACE.

    The ratingKey survives every outcome, including the two that deliberately
    touch nothing: a smart playlist (``failed`` — Plex won't let us edit its
    rows) and an empty resolve (``empty``).
    """
    tracks, missing = resolution.tracks, resolution.missing
    shown = missing[:MISSING_TRACKS_CAP]
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
            matched_by=resolution.matched_by,
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
            matched_by=resolution.matched_by,
            artwork_hash=prior_hash,
        )
    collapsed = _reconcile_items(existing, tracks)
    if getattr(existing, "title", None) != title:
        existing.editTitle(title)
    if marker not in _summary_of(existing):
        _best_effort_stamp(existing, marker)
    # Pushes the poster to Plex when the art changed; a statement, not an
    # argument, so the outbound call is visible in the body.
    artwork_hash = _poster_hash_after_update(existing, artwork, prior_hash)
    misses = _misses(resolution, collapsed)
    return PlexTargetState(
        rating_key=key,
        status="ok" if not misses else "partial",
        missing=len(misses),
        missing_tracks=misses[:MISSING_TRACKS_CAP],
        matched_by=resolution.matched_by,
        artwork_hash=artwork_hash,
    )


def _reconcile_on(
    server: Any,
    title: str,
    resolution: PlexResolution,
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

    existing = _find_our_playlist(server, playlist_id, prior_key)
    if existing is None:
        return _create_our_playlist(server, title, resolution, marker=marker, artwork=artwork)
    return _update_our_playlist(
        existing,
        title,
        resolution,
        marker=marker,
        prior_hash=prior_hash,
        artwork=artwork,
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
        raise PlexNotConfigured(_NOT_CONFIGURED_ERROR)
    try:
        server = client.connect(config.base_url, config.token)
        section = client.music_section(server, config.library_section)
        if section is None:
            raise _no_section_error(config.library_section)
        resolution = resolve_ordered_tracks(section, specs)
        return _reconcile_on(
            server,
            title,
            resolution,
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
        raise PlexConnectionError(_SYNC_FAILED_ERROR) from exc


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
        raise PlexNotConfigured(_NOT_CONFIGURED_ERROR)
    try:
        admin = client.connect(config.base_url, config.token)
        section = client.music_section(admin, config.library_section)
        if section is None:
            raise _no_section_error(config.library_section)
        resolution = resolve_ordered_tracks(section, specs)
    except PlexConnectionError:
        raise
    except Exception as exc:
        raise PlexConnectionError(_SYNC_FAILED_ERROR) from exc

    def _reconcile_for(server: Any, key: str) -> PlexTargetState:
        return _reconcile_on(
            server,
            title,
            resolution,
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
        # whose message is not ours to show. INVARIANT this relies on: every
        # PlexConnectionError raised on the reconcile path carries a STATIC
        # message — never interpolate an exception, URL, or token into one, or
        # it ships verbatim to the UI through this line.
        error = str(exc) if isinstance(exc, PlexConnectionError) else _ACCOUNT_ERROR
        # Carry the prior ratingKey (and poster hash) into the failed state: the
        # caller replaces the WHOLE state map with what we return, so recording
        # None here would ERASE a known key and orphan the still-existing Plex
        # copy (a later delete/de-target short-circuits on a None key).
        # `matched_by` deliberately stays at zero: the resolution may well have
        # succeeded, but nothing of it reached this target, and a tally here
        # would read as a description of the copy Plex actually holds.
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
        raise PlexNotConfigured(_NOT_CONFIGURED_ERROR)
    try:
        admin = client.connect(config.base_url, config.token)
    except Exception as exc:
        raise PlexConnectionError(_SYNC_FAILED_ERROR) from exc
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
