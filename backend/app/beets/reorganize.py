# backend/app/beets/reorganize.py
"""Reorganize: re-apply the live beets paths/replace config to EXISTING files.

All beets access for the feature lives here (rule 3). A preview is read-only
(``item.destination`` compared to ``item.path``, exactly as ``beet move`` filters);
the move (Task 4) is ``Album.move``/``Item.move`` with ``MoveOperation.MOVE``, which
relocates files + art and prunes the vacated dirs. Every op binds
``lib.music_dir_context()`` because beets stores DB paths relative to the library
dir and converts them both ways via a ContextVar a worker thread does not inherit.

Three of the helpers here are the move-hygiene contract for the WHOLE app, not
just this feature: ``collisions_by_dest`` (would beets divert this move to a
``.N`` sibling?), ``art_preflight`` (would beets silently rename the album art?)
and ``carry_sidecars`` (lyrics follow their audio). ``app/beets/edit.py`` imports
all three, because a tag edit that renames a file performs the same move under a
different trigger, and a second copy of any of them would drift out of agreement.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Any, NamedTuple

import beets
from beets.util import FilesystemError, MoveOperation, prune_dirs, samefile, syspath

from app.beets.library import LibraryHandle, _abs_path
from app.beets.orphans import _under, find_orphan_folders
from app.beets.sidecars import move_sidecars
from app.models.reorganize import (
    OrphanFolder,
    ReorganizeCollision,
    ReorganizeConflict,
    ReorganizeMove,
    ReorganizeMoveKind,
    ReorganizeOutcome,
    ReorganizePlan,
    ReorganizeScope,
)

_log = logging.getLogger(__name__)

#: Detailed preview rows are capped here; counts stay exact, truncated=True past it.
PREVIEW_ROW_CAP = 1000

#: Collision details quoted in a refused unit's error string. A whole-folder
#: collision yields one per track and every failed unit's message is carried in the
#: job status, so the string stays bounded; the preview keeps them all.
COLLISION_ERROR_CAP = 3


def _albums_for_scope(
    lib: Any, *, scope: ReorganizeScope, artist: str | None, album_id: int | None
) -> list[Any]:
    from beets.dbcore.query import MatchQuery

    if scope == "album":
        album = lib.get_album(album_id) if album_id is not None else None
        return [album] if album is not None else []
    if scope == "artist":
        # Parameterized query (not f-string) so metacharacters in the name are safe.
        return list(lib.albums(MatchQuery("albumartist", artist)))
    return list(lib.albums())


def _singletons_for_scope(lib: Any, *, scope: ReorganizeScope) -> list[Any]:
    # Singletons (album_id IS NULL) only sweep at library scope.
    if scope == "library":
        return list(lib.items("singleton:true"))
    return []


def collect_units(
    lib: Any, *, scope: ReorganizeScope, artist: str | None, album_id: int | None
) -> tuple[list[Any], list[Any]]:
    """(albums, singletons) for the scope. The runner snapshots these."""
    return (
        _albums_for_scope(lib, scope=scope, artist=artist, album_id=album_id),
        _singletons_for_scope(lib, scope=scope),
    )


def _item_moves(lib: Any, item: Any) -> bool:
    """True iff this item's file would relocate — beets' own per-item filter."""
    return bool(item.path != item.destination(basedir=lib.directory))


def _unit_dests(lib: Any, items: list[Any]) -> list[tuple[Any, bytes]]:
    """(item, computed destination) for every item of a unit.

    ``destination()`` evaluates a path template per item, so it is computed ONCE
    here and shared by the move filter, the collision pre-flight and the preview
    row rather than recomputed at each site.
    """
    return [(i, bytes(i.destination(basedir=lib.directory))) for i in items]


def _rel_to_music(lib: Any, path: bytes) -> str:
    """``path`` shown relative to the music root (absolute if it lies outside it)."""
    p = os.fsdecode(path)
    prefix = os.fsdecode(lib.directory) + os.sep
    return p[len(prefix) :] if p.startswith(prefix) else p


def _track_desc(item: Any) -> str:
    """``track 1 'Song' (a.mp3)`` — the tags that built the name, and the file
    they named, so the user can act on either end."""
    name = os.path.basename(os.fsdecode(item.path))
    return f"track {item.track} {str(item.title or '')!r} ({name})"


def _occupant_desc(lib: Any, dest: bytes) -> str:
    """What is sitting at ``dest``: a library track (named with its owning unit) or
    a file the library knows nothing about. One query per collision, which is fine
    because a collision is rare — this never runs on the happy path."""
    from beets.dbcore.query import PathQuery

    # Exact-path lookup: PathQuery's other arm matches a DIRECTORY prefix, which a
    # file path can never satisfy. It also handles beets' relative DB path storage.
    occupant = lib.items(PathQuery("path", dest)).get()
    if occupant is None:
        return "is not a file in the library"
    if occupant.album_id:
        artist = str(occupant.albumartist or occupant.artist or "").strip() or "Unknown"
        # The album id disambiguates the real-world case: two album ROWS with
        # identical tags, whose labels alone read as one album.
        owner = f"{artist} - {occupant.album} (album {occupant.album_id})"
    else:
        owner = singleton_label(occupant)
    return f"holds track {occupant.track} {str(occupant.title or '')!r} of {owner}"


def collisions_by_dest(
    lib: Any, dests: list[tuple[Any, bytes]]
) -> dict[bytes, ReorganizeCollision]:
    """Every destination in this batch that beets would divert to a ``.N`` sibling,
    keyed by that (normalized) destination.

    Keyed rather than listed because the tag-edit move path reports per TRACK and
    has to attribute a collision back to the item that computed it; reorganize
    refuses whole units and takes the values (``_collisions``). The key is always
    ``os.path.normpath`` of a destination that was passed in, so a caller can look
    its own items up without re-deriving anything.

    ``Item.move_file`` calls ``util.unique_path`` when the destination exists and
    renames the loser silently, so detecting it afterwards is useless: the loser
    vacates the slot it held and ``unique_path`` restarts its scan at ``.1``, which
    is why an untreated collision renames a real file on EVERY sweep.

    Two independent classes, both evaluated over ALL of the unit's items — from the
    second run onward a moving-only view no longer sees the duplicate, because the
    twin that won the name now sits on it and no longer "moves":

    a. INTRA-UNIT: two items compute the same destination.
    b. CROSS-UNIT: the destination exists on disk and is not one of this unit's own
       current file paths. A path the unit itself holds is deliberately allowed —
       an item relocating this run vacates it, so beets diverts at most once and
       the next run settles it; refusing would freeze that state forever.

    Mirrors ``unique_path``'s own existence test and ``move_file``'s ``samefile``
    guard, so this predicts a divert instead of guessing at one.
    """
    own_paths = {os.path.normpath(bytes(i.path)) for i, _dest in dests}
    # Paths this run will vacate. Only these may absorb a samefile alias below: a
    # STAYING mate holds its name forever, so an alias of it diverts every sweep.
    moving_paths = {
        p for i, dest in dests if (p := os.path.normpath(bytes(i.path))) != os.path.normpath(dest)
    }
    groups: dict[bytes, list[Any]] = {}
    for item, dest in dests:
        groups.setdefault(os.path.normpath(dest), []).append(item)

    found: dict[bytes, ReorganizeCollision] = {}
    for dest, group in groups.items():
        if len(group) < 2:
            continue
        rel = _rel_to_music(lib, dest)
        found[dest] = ReorganizeCollision(
            kind="intra_unit",
            path=rel,
            detail=(
                f"{rel}: {len(group)} tracks resolve to this same name: "
                + ", ".join(_track_desc(i) for i in group)
            ),
        )

    for item, dest in dests:
        key = os.path.normpath(dest)
        # ``key in own_paths`` is a byte-equal fast path, not a separate rule: an
        # item already sitting at its destination or a mate's slot are settled
        # without a syscall, and the dangerous byte-equal case — a STAYING mate's
        # path — cannot reach here, because a settled mate's destination equals
        # that same path and the intra-unit arm above has already reported it.
        # Order matters — samefile costs two stats per non-matching pair, so it
        # must not run for the overwhelmingly common free destination.
        if key in found or key in own_paths:
            continue
        if not os.path.exists(syspath(key)):
            continue
        # Byte-unequal yet the same file. beets skips unique_path only when the
        # destination IS the moving item's own file (``move_file``'s guard) — the
        # case-only rename on a case-insensitive filesystem. Wider than that, an
        # alias is only safe when it names a mate that itself vacates this run
        # (one divert, settles next sweep); an alias of a STAYING mate's file
        # holds the name forever and beets would divert on every sweep.
        #
        # The self check is SUBSUMED by the moving_paths check today: reaching
        # here means key != this item's own path, which is exactly what puts that
        # path into moving_paths. It stays as the explicit beets-parity anchor —
        # if the wider exemption below is ever narrowed, this line is the part
        # that must survive to keep case-only renames from being refused.
        if samefile(key, os.path.normpath(bytes(item.path))):
            continue
        if any(samefile(key, p) for p in moving_paths):
            continue
        rel = _rel_to_music(lib, key)
        found[key] = ReorganizeCollision(
            kind="cross_unit",
            path=rel,
            detail=f"{rel}: already exists on disk and {_occupant_desc(lib, key)}",
        )
    return found


def _collisions(lib: Any, dests: list[tuple[Any, bytes]]) -> list[ReorganizeCollision]:
    """This unit's collisions as a flat list — reorganize refuses the whole unit,
    so which destination produced which row does not matter here."""
    return list(collisions_by_dest(lib, dests).values())


class ArtPreflight(NamedTuple):
    collision: ReorganizeCollision | None
    expected_dest: bytes | None  # normalized predicted art path when an art move is predicted


def art_preflight(lib: Any, album: Any, dests: list[tuple[Any, bytes]]) -> ArtPreflight:
    """Predict the album-art divert before ``Album.move`` happens.

    ``Album.move`` moves the items and then calls ``move_art``; ``move_art``
    silently renames the art to a ``.N`` sibling (``util.unique_path``) when its
    destination name is already held by a file that is not the art itself. The
    predicted destination is what ``Album.move`` will hand ``move_art``: the dir
    of the FIRST item that relocates (``move_art`` receives ``os.path.dirname``
    of the moved item's path, and beets moves items in order) — more precisely,
    the first item whose move CHANGED its path — and, since beets'
    ``Item.move`` silently SKIPS a mover whose source file is missing
    (``beets/library/models.py:1179``), really the first mover that exists on
    disk, which is exactly whom the prediction below starts from.
    Must be called
    under ``lib.music_dir_context()`` — the same requirement as the item
    pre-flight, since ``album.art_destination`` renders paths the same way.

    Returns ``(collision, expected_dest)``: ``collision`` is set only for a real
    art collision (the predicted name is already held by something else on disk);
    ``expected_dest`` is the normalized predicted art path whenever an art move
    is predicted at all — ``reorganize_album``'s post-move backstop compares the
    landed ``artpath`` against it.
    """
    old_art = album.artpath
    if not old_art:
        return ArtPreflight(None, None)
    old = os.path.normpath(bytes(old_art))
    if not os.path.exists(syspath(old)):
        # beets clears a missing-art ref itself; not a divert.
        return ArtPreflight(None, None)
    moving = [(i, d) for i, d in dests if bytes(i.path) != d]
    if not moving:
        return ArtPreflight(None, None)
    # Item.move silently SKIPS a mover whose source file is missing
    # (beets/library/models.py:1179), so Album.move's "first item whose path
    # changed" is really the first mover that exists on disk — predict from
    # exactly that one.
    present = next(
        (d for i, d in moving if os.path.exists(syspath(os.path.normpath(bytes(i.path))))),
        None,
    )
    if present is None:
        return ArtPreflight(None, None)
    item_dir = os.path.dirname(os.path.normpath(present))
    try:
        new_art = album.art_destination(old, item_dir=item_dir)
    except Exception:  # a template that cannot render must not fail the sweep
        # (unlike _disc_dir_levels, degrading here disarms BOTH the refusal and
        # the backstop — a DEBUG log would hide that entirely)
        _log.warning(
            "art-destination probe failed for album %s; art divert prediction"
            " disabled for this move",
            album.id,
            exc_info=True,
        )
        return ArtPreflight(None, None)
    key = os.path.normpath(bytes(new_art))
    if key == old:
        return ArtPreflight(None, None)  # already in place
    if not os.path.exists(syspath(key)):
        return ArtPreflight(None, key)  # free name; the move will land there
    # No samefile(key, old) exemption: beets' Album.move_art (beets/library/
    # models.py:407-463) has NO samefile guard — only the byte-equal
    # new_art == old_art short-circuit above runs before util.unique_path
    # diverts, so an occupant that is merely an ALIAS (e.g. symlink) of the
    # album's own art still gets diverted to a .N sibling. The normpath
    # byte-equality check (key == old) is the only own-art exemption we make.
    moving_paths = {os.path.normpath(bytes(i.path)) for i, _d in moving}
    if key in moving_paths or any(samefile(key, p) for p in moving_paths):
        # the occupant vacates before art moves — items move first
        return ArtPreflight(None, key)
    rel = _rel_to_music(lib, key)
    return ArtPreflight(
        ReorganizeCollision(
            kind="art",
            path=rel,
            detail=(
                f"{rel}: the album art's computed name already exists on disk"
                f" and {_occupant_desc(lib, key)}"
            ),
        ),
        key,
    )


def _collision_error(collisions: list[ReorganizeCollision]) -> str:
    """The refused unit's error string (see COLLISION_ERROR_CAP)."""
    shown = [c.detail for c in collisions[:COLLISION_ERROR_CAP]]
    hidden = len(collisions) - len(shown)
    if hidden:
        shown.append(f"and {hidden} more collision{'s' if hidden > 1 else ''}")
    return "; ".join(shown)


def _verify_moves(pending: list[tuple[int, bytes, bytes]], after: dict[int, bytes]) -> list[str]:
    """Post-move verification: beets silently skips a move whose source file is
    absent and silently diverts to a `.N`-suffixed name when the destination is
    occupied (unique_path) — both leave the unit eternally re-flagged by the
    preview. Returns one human-readable problem per affected file.

    The BACKSTOP, not the defence: ``_collisions`` refuses a divertable unit before
    anything moves, so a divert reaching here means the destination was taken
    between the pre-flight and the move. This code therefore knows only that the
    name was taken and where the file landed — never why — and says exactly that.
    """
    problems: list[str] = []
    for item_id, old_path, dest in pending:
        new_path = after.get(item_id, old_path)
        name = os.path.basename(os.fsdecode(old_path))
        if new_path == old_path:
            if not os.path.exists(old_path):
                problems.append(
                    f"{name}: file not found on disk at the library's recorded path"
                    "; fix the file name on disk or re-import the album"
                )
            else:
                problems.append(f"{name}: move did not take effect")
        elif new_path != dest:
            problems.append(
                f"{name}: the computed file name was already taken on disk, so the file"
                f" landed at {os.path.basename(os.fsdecode(new_path))!r}"
                "; check this album's folder for what is holding that name"
            )
    return problems


def carry_sidecars(lib: Any, old_path: bytes, new_path: bytes) -> None:
    """Move ONE relocated item's lyric sidecars to its new location, then re-prune.

    beets moves audio + album art and nothing else, so MusicDrop's own
    ``.lrc``/``.txt`` sidecars (the files Plex actually reads) stay in the vacated
    folder — which the post-run orphan sweep then classifies as an audio-empty
    husk and moves to Trash, losing the lyrics while the job reports success. The
    same stranding happens on an in-place rename, where there is no husk at all
    and the sidecar simply stops matching its track.

    Keyed off the ACTUAL landing path rather than the computed destination, so a
    collision-diverted ``.1`` file keeps its lyrics.

    The re-prune is not cosmetic: beets prunes the vacated dir DURING the move,
    while the sidecars are still sitting in it, so that prune is a no-op and the
    now-empty dir would outlive every future sweep (``find_orphan_folders``
    deliberately ignores empty dirs). Pruning again with beets' own arguments
    makes the on-disk result identical to a sidecar-free move. Never raises: the
    audio has already moved and the unit's outcome must stay truthful about it.
    """
    if not move_sidecars(old_path, new_path):
        return
    directory = os.path.dirname(old_path)
    try:
        prune_dirs(directory, lib.directory, clutter=beets.config["clutter"].as_str_seq())
    except OSError:
        _log.warning("pruning vacated dir failed: %r", directory, exc_info=True)


def _carry_sidecars(
    lib: Any, pending: list[tuple[int, bytes, bytes]], after: dict[int, bytes]
) -> None:
    """``carry_sidecars`` for every item of a unit that beets has just moved."""
    for item_id, old_path, _dest in pending:
        # Default = the old path: an item beets silently skipped (source file
        # missing) has not moved, and ``move_sidecars`` no-ops on an unchanged
        # path, so its sidecars stay put. ONE gate for that, tested there.
        carry_sidecars(lib, old_path, after.get(item_id, old_path))


def _commonpath_of_dirs(paths: list[bytes]) -> str:
    """The deepest directory shared by all of ``paths`` (the unit's album root).

    commonpath over the item DIRS yields the album root even for multi-disc
    layouts ($album/Disc N/...). Falls back to the first dir on the rare
    ValueError (mixed roots / empty)."""
    dirs = [os.path.dirname(os.fsdecode(p)) for p in paths]
    if not dirs:
        return ""
    try:
        return os.path.commonpath(dirs)
    except ValueError:
        return dirs[0]


def album_label(album: Any) -> str:
    artist = str(getattr(album, "albumartist", "") or "").strip() or "Unknown"
    return f"{artist} - {album.album}"


def singleton_label(item: Any) -> str:
    artist = str(item.artist or item.albumartist or "").strip() or "Unknown"
    return f"{artist} - {item.title}"


def album_scope_label(handle: LibraryHandle, album_id: int) -> str | None:
    """``album_label`` for an album id, or ``None`` when the album is missing.

    Typed lookup for the router's 404-or-start decision, so it never calls
    ``lib.get_album`` itself (rule 3). Scalar reads; no path expansion needed.
    """
    album = handle.lib.get_album(album_id)
    if album is None:
        return None
    return album_label(album)


def _describe_unit(
    lib: Any,
    *,
    kind: ReorganizeMoveKind,
    label: str,
    items: list[Any],
    album: Any | None = None,
) -> ReorganizeMove | ReorganizeConflict | None:
    """One preview row for a unit: a CONFLICT (would be refused), a MOVE, or None
    when it is already in place. A conflicted unit is never also a move — the
    preview must not offer a refusal as an ordinary relocation."""
    if not items:
        return None
    dests = _unit_dests(lib, items)
    moving = [i for i, dest in dests if bytes(i.path) != dest]
    if not moving:
        return None
    # commonpath over the item DIRS: for a lone singleton that is just its dir.
    from_path = _commonpath_of_dirs([i.path for i, _dest in dests])
    collisions = _collisions(lib, dests)
    if album is not None:
        art = art_preflight(lib, album, dests)
        if art.collision is not None:
            collisions.append(art.collision)
    if collisions:
        return ReorganizeConflict(
            kind=kind, label=label, from_path=from_path, collisions=collisions
        )
    return ReorganizeMove(
        kind=kind,
        label=label,
        from_path=from_path,
        to_path=_commonpath_of_dirs([dest for _i, dest in dests]),
        track_count=len(moving),
    )


def _describe_album(lib: Any, album: Any) -> ReorganizeMove | ReorganizeConflict | None:
    return _describe_unit(
        lib, kind="album", label=album_label(album), items=list(album.items()), album=album
    )


def _describe_singleton(lib: Any, item: Any) -> ReorganizeMove | ReorganizeConflict | None:
    return _describe_unit(lib, kind="singleton", label=singleton_label(item), items=[item])


def _scope_label(
    *, scope: ReorganizeScope, artist: str | None, album_id: int | None, albums: list[Any]
) -> str:
    if scope == "album":
        return album_label(albums[0]) if albums else f"album {album_id}"
    if scope == "artist":
        return artist or "Unknown"
    return "library"


def _dest_dir_parts(item: Any) -> list[str]:
    """The directory components of where the path template wants this item."""
    rel = os.fsdecode(item.destination(relative_to_libdir=True))
    return os.path.dirname(rel).split(os.sep)


def _disc_dir_levels(item: Any) -> int:
    """How many TRAILING directory components of a destination the disc number
    controls: 1 for ``$albumartist/$album/Disc $disc/$track $title``, 0 for a template
    that gives discs no directory of their own.

    Probed by rendering one item's destination twice with different disc numbers
    instead of parsing the template text, which would miss ``%if``/function forms.
    A template that only emits the disc dir CONDITIONALLY changes the component
    COUNT, and that returns 0 — too ambiguous to strip. Computed once per call from
    one album, so a library whose query-keyed path formats disagree about disc
    nesting is read through the first album's shape; the fallback is simply less
    protection, never more.

    The second render runs on ``Model.copy()``, which duplicates the field values
    but keeps ``_db`` AND the row id (beets ``dbcore/db.py:406-419``) — the probe is
    DB-ATTACHED, not a detached value object. Only ``destination()`` may ever be
    called on it: ``store()`` on the copy would write the fake disc number to the
    real row.
    """
    try:
        here = _dest_dir_parts(item)
        probe = item.copy()
        probe.disc = int(item.disc or 0) + 1
        there = _dest_dir_parts(probe)
    except Exception:  # a template that cannot render must not fail the sweep
        _log.debug("disc-level probe failed", exc_info=True)
        return 0
    if len(here) != len(there):
        return 0
    shared = 0
    while shared < len(here) and here[shared] == there[shared]:
        shared += 1
    return len(here) - shared


def _template_dirs(item: Any, music_dir: str, disc_levels: int) -> set[str]:
    """The dirs the path template says this album occupies: the item's destination
    dir, plus the dir ``disc_levels`` above it when each disc gets its own directory.

    This is the ONE piece of information that separates an album's own folder from a
    mere container of albums. The two are isomorphic on disk —
    ``Artist/{Good Album/audio, Old Album/art}`` (husk, must sweep) and
    ``Album/{CD1/audio, Scans (LP)/art}`` (art, must keep) are the same tree — so no
    rule over the filesystem plus the set of roots can tell them apart. The template
    can: it names where THIS album belongs.
    """
    try:
        dest_rel = os.path.dirname(os.fsdecode(item.destination(relative_to_libdir=True)))
    except Exception:  # same reason as _disc_dir_levels: degrade, never fail
        _log.debug("destination probe for album protection failed", exc_info=True)
        return set()
    dest_dir = os.path.normpath(os.path.join(music_dir, dest_rel))
    above = dest_dir
    for _ in range(disc_levels):
        above = os.path.dirname(above)
    return {d for d in (dest_dir, above) if _under(d, music_dir)}


def _album_dirs_above(item: Any, root: str, music_dir: str, disc_levels: int) -> set[str]:
    """The dirs a live album owns ABOVE its actual root, per the path template.

    An album whose audio all sits in ONE subfolder (``Album/CD1/*.flac`` — a one-disc
    box set, or a disc-bearing template with a single disc) folds to that subfolder,
    so the ``$album`` dir holding its ``Scans (LP)`` is not covered by the root alone.

    Only :func:`_template_dirs` entries that strictly CONTAIN the album's actual root
    are taken, and that is the whole discriminator — it is what keeps an ARTIST
    container out of the set. For an album filed where the template wants it the
    destination dir IS the root, so there is nothing above to add; for a misfiled one
    the destination is somewhere else entirely and contains no part of the root. So a
    genuine husk sitting beside a live album
    (``Artist/{Good Album/01.flac, Old Album/cover.jpg}``) is still swept.
    """
    return {d for d in _template_dirs(item, music_dir, disc_levels) if _under(root, d)}


def _poison_if_foreign_container(
    parent: str,
    roots: dict[str, Any],
    music_dir: str,
    disc_levels: int,
    keeps: dict[str, bool],
    poisoned: set[str],
) -> None:
    """Poison ``parent`` when it is another album's root the template does not
    claim as that album's own dir (``keeps`` memoizes the template answer)."""
    if parent not in roots or parent in poisoned:
        return
    if parent not in keeps:
        own = _template_dirs(roots[parent], music_dir, disc_levels)
        keeps[parent] = parent in own
    if not keeps[parent]:
        poisoned.add(parent)


def _drop_container_roots(
    roots: dict[str, Any], music_dir: str, disc_levels: int
) -> dict[str, Any]:
    """Drop a root that strictly CONTAINS another album's root — UNLESS the template
    says that dir is the containing album's OWN.

    Two shapes fold a root high enough to swallow another album's, and they are
    indistinguishable on disk (both split the album's items across sibling folders of
    the contained root):

    * a half-finished move — one album's items left across ``Artist/Half A`` and
      ``Artist/Half B`` commonpath UP to the ARTIST dir. Keeping that would silently
      stop every husk under that artist from ever being swept, so it is dropped. Both
      folders it occupies hold audio directly, so its own art subfolders keep the
      ``has_own_audio`` protection they always had.
    * an ordinary NESTED ALBUM — a bonus disc imported as its own row inside
      ``A/Main``, whose multi-disc parent legitimately roots at ``A/Main``. Dropping
      that one let the sweep trash ``A/Main/Sleeve Photos`` (deep-review F3).

    :func:`_template_dirs` separates them: ``A/Main`` is where the template puts that
    album, ``Artist`` is not. BOUND (deliberate, err toward sweeping over
    over-protecting): a MISFILED album that also contains another album's root matches
    no template dir, so it is still dropped and its art subfolders are sweepable.
    """
    keeps: dict[str, bool] = {}
    poisoned: set[str] = set()
    for root in roots:
        parent = os.path.dirname(root)
        while _under(parent, music_dir):
            _poison_if_foreign_container(parent, roots, music_dir, disc_levels, keeps, poisoned)
            nxt = os.path.dirname(parent)
            if nxt == parent:
                break
            parent = nxt
    return {root: item for root, item in roots.items() if root not in poisoned}


def live_album_roots(lib: Any) -> frozenset[str]:
    """Normalized absolute dirs owned by LIVE albums, for ``protected_dirs``.

    Per album: the commonpath of its item dirs (``_commonpath_of_dirs`` — the fold
    ``app.beets.trash._album_root`` applies, including its ValueError fallback for
    mixed absolute/relative rows), plus whatever :func:`_album_dirs_above` says the
    path template puts above that. Items with no album row (singletons) are skipped,
    and so is any root at or outside the music dir: a root-level album would
    otherwise protect the whole library.

    Cost: one ``lib.items()`` pass grouped by album id plus one ``destination()``
    render per album — O(items + albums). That is the same "seconds at 75k tracks"
    class of full-library read ``app.beets.trash._folder_is_shared`` documents, paid
    once per sweep/preview beside their own full-disk ``os.walk``. If it ever
    measures slow, the named escape is a raw-SQL fold over ``items`` (the
    ``_folder_is_shared`` pattern). One spot measurement, 2026-08-28, synthetic
    1000 albums x 10 tracks on the dev box: 0.73 s, 0.45 s of it the item
    materialization — a dated data point, not a live figure; re-measure rather than
    quoting it.

    KNOWN LIMIT (named residual, not handled here): when a reorganize MOVES a live
    album, an untracked art folder left behind in the VACATED dir is still sweepable
    — the set is read after the moves, and the vacated dir is no longer any album's.
    """
    music_dir = os.path.normpath(_abs_path(lib, lib.directory))
    item_paths: dict[int, list[bytes]] = {}
    reps: dict[int, Any] = {}
    for item in lib.items():
        album_id = item.album_id
        if album_id is None:
            continue
        key = int(album_id)
        item_paths.setdefault(key, []).append(os.fsencode(_abs_path(lib, item.path)))
        reps.setdefault(key, item)
    roots: dict[str, Any] = {}
    for key, paths in item_paths.items():
        root = os.path.normpath(_commonpath_of_dirs(paths))
        if _under(root, music_dir):
            roots[root] = reps[key]
    if not roots:
        return frozenset()
    # Before the container drop, which needs the same template reading to tell an
    # album's own folder from a mere container of albums.
    disc_levels = _disc_dir_levels(next(iter(roots.values())))
    kept = _drop_container_roots(roots, music_dir, disc_levels)
    protected = set(kept)
    for root, item in kept.items():
        protected |= _album_dirs_above(item, root, music_dir, disc_levels)
    return frozenset(protected)


def _orphan_preview(
    lib: Any,
    *,
    scope: ReorganizeScope,
    trash_dir: Path | None,
    ignore_dirs: tuple[Path, ...],
) -> tuple[list[OrphanFolder], int]:
    """Read-only orphan candidates for the preview (no move). Empty when no trash_dir
    is configured, and empty for a non-library scope: a scoped run's husks only form
    DURING the run (post-move), so they are surfaced in the result count, not the
    pre-move preview. Library scope lists the pre-existing backlog."""
    if trash_dir is None or scope != "library":
        return [], 0
    music_dir = Path(os.fsdecode(lib.directory))
    # After the early returns: a preview that shows no orphan section must not pay
    # for the DB pass. The executor derives the same set, so preview == outcome.
    folders = find_orphan_folders(
        music_dir,
        seeds=None,
        trash_dir=trash_dir,
        ignore_dirs=ignore_dirs,
        protected_dirs=live_album_roots(lib),
    )
    rows: list[OrphanFolder] = []
    for f in folders[:PREVIEW_ROW_CAP]:
        try:
            rel = str(f.relative_to(music_dir))
        except ValueError:
            rel = str(f)
        file_count = sum(1 for _d, _s, files in os.walk(f) for _f in files)
        rows.append(OrphanFolder(name=f.name, path=rel, file_count=file_count))
    return rows, len(folders)


def plan_reorganize(
    lib: Any,
    *,
    scope: ReorganizeScope,
    artist: str | None = None,
    album_id: int | None = None,
    trash_dir: Path | None = None,
    ignore_dirs: tuple[Path, ...] = (),
) -> ReorganizePlan:
    """Read-only dry run: what would move under the current path config + (when a
    trash_dir is given) the audio-empty husks that would be moved to Trash."""
    with lib.music_dir_context():
        albums = _albums_for_scope(lib, scope=scope, artist=artist, album_id=album_id)
        singletons = _singletons_for_scope(lib, scope=scope)
        total = len(albums) + len(singletons)
        all_moves: list[ReorganizeMove] = []
        all_conflicts: list[ReorganizeConflict] = []
        rows = [_describe_album(lib, a) for a in albums]
        rows += [_describe_singleton(lib, i) for i in singletons]
        for row in rows:
            if isinstance(row, ReorganizeMove):
                all_moves.append(row)
            elif isinstance(row, ReorganizeConflict):
                all_conflicts.append(row)
        will_move = len(all_moves)
        conflicts_total = len(all_conflicts)
        moves = all_moves[:PREVIEW_ROW_CAP]
        orphans, orphans_total = _orphan_preview(
            lib, scope=scope, trash_dir=trash_dir, ignore_dirs=ignore_dirs
        )
        return ReorganizePlan(
            scope=scope,
            scope_label=_scope_label(scope=scope, artist=artist, album_id=album_id, albums=albums),
            total=total,
            will_move=will_move,
            already_in_place=total - will_move - conflicts_total,
            moves=moves,
            truncated=will_move > len(moves),
            orphans=orphans,
            orphans_total=orphans_total,
            conflicts=all_conflicts[:PREVIEW_ROW_CAP],
            conflicts_total=conflicts_total,
        )


def reorganize_album(lib: Any, album: Any) -> ReorganizeOutcome:
    """Move one album to match the current path config. Never raises.

    Skips empty albums and already-organized albums, and REFUSES a colliding one:
    a unit whose move would make beets divert a file to a ``.N`` sibling is failed
    before ``Album.move`` runs, so nothing on disk and nothing in the DB is touched
    (see ``_collisions``). Otherwise ``Album.move`` relocates all items + art,
    prunes the vacated dirs, and updates DB paths (store=True).

    The art pre-flight refuses a predicted divert BEFORE the move; after the
    move, a backstop re-checks the art actually landed at its predicted name
    and reports a divert that slipped in between pre-flight and move (same
    doctrine as ``_verify_moves``: mutation happens, then the failure is
    reported honestly).
    """
    label = album_label(album)
    pending: list[tuple[int, bytes, bytes]] = []  # bound before try: the except path reads it
    with lib.music_dir_context():
        try:
            items = list(album.items())
            if not items:
                return ReorganizeOutcome(status="skipped", label=label)
            dests = _unit_dests(lib, items)
            if not any(bytes(i.path) != dest for i, dest in dests):
                return ReorganizeOutcome(status="skipped", label=label)
            collisions = _collisions(lib, dests)
            art = art_preflight(lib, album, dests)
            if art.collision is not None:
                collisions.append(art.collision)
            if collisions:
                return ReorganizeOutcome(
                    status="failed", label=label, error=_collision_error(collisions)
                )
            source_dir = _commonpath_of_dirs([i.path for i in items])  # before the move
            pending = [
                (int(i.id), bytes(i.path), dest) for i, dest in dests if bytes(i.path) != dest
            ]
            with lib.transaction():
                album.move(MoveOperation.MOVE, store=True)
            # Album.move re-fetches its own item objects; the local `items` list is
            # NOT mutated, so re-read the album's items to see the post-move paths.
            after = {int(i.id): bytes(i.path) for i in album.items()}
            _carry_sidecars(lib, pending, after)
            problems = _verify_moves(pending, after)
            if art.expected_dest is not None and album.artpath:
                landed = os.path.normpath(bytes(album.artpath))
                # A unique_path divert always changes the BASENAME (.N) and
                # never the directory; a dir-only difference is a misprediction
                # (beets skipped or refused the predicted mover), not a taken
                # name — reporting it would be a false "already taken".
                if os.path.basename(landed) != os.path.basename(art.expected_dest):
                    problems.append(
                        f"{_rel_to_music(lib, art.expected_dest)!r}: the album art's"
                        " computed name was already taken on disk, so the art landed at"
                        f" {_rel_to_music(lib, landed)!r}"
                        "; check this album's folder for what is holding that name"
                    )
            if problems:
                return ReorganizeOutcome(status="failed", label=label, error="; ".join(problems))
            return ReorganizeOutcome(status="moved", label=label, source_dir=source_dir or None)
        # FilesystemError (permission/disk-full/NAS I/O from beets' util.move/copy)
        # subclasses HumanReadableError(Exception), NOT OSError — catch it too, or
        # one bad album escapes this "never raises" adapter and aborts the whole sweep.
        except (ValueError, OSError, FilesystemError) as exc:
            # Album.move moves+stores ONE ITEM AT A TIME, and beets' Transaction
            # commits even while an exception propagates (dbcore/db.py __exit__ has
            # no rollback branch). Items moved before a mid-album crash are
            # therefore permanently re-pathed — they never re-enter `pending` on a
            # later run, so this is the ONLY chance to carry their sidecars before
            # the vacated folder decays into a husk the orphan sweep trashes.
            # Suppress everything: this adapter never raises, and no carry problem
            # may displace the original failure being reported in `exc`.
            if pending:
                with contextlib.suppress(Exception):
                    _carry_sidecars(lib, pending, {int(i.id): bytes(i.path) for i in album.items()})
            return ReorganizeOutcome(
                status="failed", label=label, error=str(exc) or exc.__class__.__name__
            )


def reorganize_singleton(lib: Any, item: Any) -> ReorganizeOutcome:
    """Move one singleton (album_id is None) to match the config. Never raises.

    Same collision pre-flight as ``reorganize_album``: a destination already held
    by anything but this item's own file is refused before the move."""
    label = singleton_label(item)
    pending: list[tuple[int, bytes, bytes]] = []  # bound before try: the except path reads it
    with lib.music_dir_context():
        try:
            old_path = bytes(item.path)
            dest = bytes(item.destination(basedir=lib.directory))
            if old_path == dest:
                return ReorganizeOutcome(status="skipped", label=label)
            collisions = _collisions(lib, [(item, dest)])
            if collisions:
                return ReorganizeOutcome(
                    status="failed", label=label, error=_collision_error(collisions)
                )
            source_dir = os.path.dirname(os.fsdecode(item.path))  # before the move
            pending = [(int(item.id), old_path, dest)]
            with lib.transaction():
                item.move(MoveOperation.MOVE, with_album=False, store=True)
            # item.move mutates this same object's `.path`, so compare it directly.
            after = {int(item.id): bytes(item.path)}
            _carry_sidecars(lib, pending, after)
            problems = _verify_moves(pending, after)
            if problems:
                return ReorganizeOutcome(status="failed", label=label, error="; ".join(problems))
            return ReorganizeOutcome(status="moved", label=label, source_dir=source_dir or None)
        except (ValueError, OSError, FilesystemError) as exc:  # see reorganize_album
            # Same crash-carry as reorganize_album. A failed single-file move
            # usually leaves item.path unchanged, making this a no-op — but a
            # partially-applied move (art moved, then store raised) is cheap to
            # cover with the identical best-effort pass.
            if pending:
                with contextlib.suppress(Exception):
                    _carry_sidecars(lib, pending, {int(item.id): bytes(item.path)})
            return ReorganizeOutcome(
                status="failed", label=label, error=str(exc) or exc.__class__.__name__
            )
