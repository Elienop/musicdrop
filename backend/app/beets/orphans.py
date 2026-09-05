"""Find orphan "husk" folders: directories under the music root that hold only
art/sidecars (no audio anywhere beneath). Pure filesystem — audio is detected by
file extension on disk, NOT via the beets DB.

"Beneath" is what the walk reaches: it does not follow symlinks and it prunes
dotdirs and NAS names, so audio behind either does not count for the parent.
Measured: ``<M>/Various/.sync/Album/01.flac`` leaves ``Various`` reading as a
husk, and the mover takes it with that audio inside.

Inside the beets-adapter boundary (CLAUDE.md rule 3) for proximity to trash.py,
though it touches no beets API: the DB-derived protection a caller may pass as
``protected_dirs`` is computed over in ``app.beets.reorganize.live_album_roots``
and arrives here as plain paths.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Collection
from pathlib import Path

_log = logging.getLogger(__name__)

#: Lowercase audio extensions. Over-inclusive on purpose: a husk is only reported
#: when NO file of ANY of these types exists beneath it, so a missing exotic
#: format would risk trashing real audio — err toward keeping.
AUDIO_EXTS: frozenset[str] = frozenset(
    {
        ".mp3",
        ".flac",
        ".m4a",
        ".m4b",
        ".aac",
        ".alac",
        ".ogg",
        ".oga",
        ".opus",
        ".wav",
        ".wave",
        ".aiff",
        ".aif",
        ".aifc",
        ".wma",
        ".ape",
        ".wv",
        ".mpc",
        ".mp2",
        ".mp1",
        ".dsf",
        ".dff",
        ".tta",
        ".tak",
        ".ac3",
        ".dts",
        ".mka",
        ".spx",
        ".ra",
    }
)


def _is_audio(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in AUDIO_EXTS


# Directory basenames the sweep must never treat as a stale artist/album husk:
# app-owned exports (the default playlists export dir is <music>/.playlists — and any
# dotdir) plus NAS/OS housekeeping dirs that legitimately hold no audio.
SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {"@eaDir", "#recycle", "lost+found", "$RECYCLE.BIN", "System Volume Information"}
)

# Well-known art/booklet/rip-artifact subfolder basenames (lowercased). A live
# MULTI-DISC album keeps its audio in ``Disc N/`` children, so the album dir has no
# DIRECT audio and the has_own_audio guard can't tell its ``Scans/`` folder from a
# genuine husk (structurally identical).
# PRIMARY protection for that shape is now ``protected_dirs`` — the beets DB's own
# live album dirs, which spare an album's art folder whatever it is named. This list
# stays as the BACKSTOP: art no album row owns (artist-level, box-set-level), and
# every caller that passes no set. False-negative-only: a stale husk that happens to
# bear one of these names is merely left in place — the module errs toward keeping.
ART_DIR_NAMES: frozenset[str] = frozenset(
    {
        "scans",
        "scan",
        "artwork",
        "art",
        "art-scans",
        "artscans",
        "covers",
        "cover",
        "booklet",
        "booklets",
        "digital booklet",
        "images",
        "logs",
    }
)


def _skip_name(name: str) -> bool:
    return name.startswith(".") or name in SKIP_DIR_NAMES


def _under(path: str, root: str) -> bool:
    """True if ``path`` is strictly inside ``root``.

    ``commonpath`` and not ``startswith(root + os.sep)``: for ``root == '/'`` the
    prefix becomes ``'//'`` and every path reads as OUTSIDE, which stops the
    seeds climb under ``directory: /`` before it starts. The at-or-above WARNING
    does not depend on this — ``_exclude_ids`` decides that by inode.
    """
    if path == root:
        return False
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # a relative and an absolute path have nothing in common
        return False


#: ``(st_dev, st_ino)``, the pair this module decides sameness by — the same
#: question ``app.beets.store_layout._same_rung`` asks, for the same reason.
PathId = tuple[int, int]


def _path_id(path: str) -> PathId | None:
    """``(st_dev, st_ino)`` for a path this process can stat, ``None`` otherwise.

    ``None`` covers absent AND unreadable. Both are safe here: an exclude root
    that cannot be stat'd is one ``os.walk`` cannot reach either, and a walked
    directory that cannot be stat'd is one the walk is about to fail on.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _at_or_above(target: PathId, start: str) -> bool:
    """Whether ``target`` names ``start`` itself or one of its ancestors."""
    d = os.path.normpath(start)
    while True:
        if _path_id(d) == target:
            return True
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent


def _in_walk_spelling(path: str, root: str) -> str | None:
    """``path`` respelled under ``root``, or ``None`` when it is not inside it.

    The walk root is ``lib.directory`` — ``normpath``'d, never ``realpath``'d —
    while a row imported in place keeps the spelling it was imported from. The
    two name one directory and compare unequal, so a foreign-spelled album root
    was dropped as "outside the library" and a foreign-spelled seed never
    started its climb. Translating by identity puts both back in the walk's
    namespace, which is the only one the rest of this module compares in.

    One ``stat`` per level, paid only when the lexical test already failed.
    """
    target = _path_id(root)
    if target is None:
        return None
    parts: list[str] = []
    d = os.path.normpath(path)
    while True:
        if _path_id(d) == target:
            return os.path.join(root, *reversed(parts)) if parts else root
        parent = os.path.dirname(d)
        if parent == d:
            return None
        parts.append(os.path.basename(d))
        d = parent


def _exclude_ids(
    root: str, exclude_roots: tuple[str, ...]
) -> tuple[frozenset[PathId], tuple[str, ...]]:
    """The identity of every exclude root the walk stops at, and their spellings.

    Identity, not a path prefix: two aliases defeat the string form — a
    symlinked library (the walk root is ``lib.directory``, normpath'd not
    realpath'd) and a BIND MOUNT of a library directory at the Trash path, where
    ``realpath`` dropped the Trash as "outside the tree", the sweep reported it
    as a husk and the mover deleted every entry (measured under ``unshare -Urm``).
    A mount point's ancestors are its own, so only the root's inode sees it.

    A root that cannot be stat'd keeps its SPELLING, which still spares its
    ancestors. A root at or ABOVE the walk root is dropped with the one WARNING
    this module logs; ``MUSICDROP_PLAYLISTS_EXPORT_DIR`` is the way in.

    "At or above" is asked twice, because a root with no identity is compared by
    spelling and the spelling can name the walk root itself:
    ``MUSICDROP_PLAYLISTS_EXPORT_DIR=<M>/typo/..`` normpaths to ``<M>``, and
    measured, it silenced the whole sweep with no warning at all.
    """
    ids: set[PathId] = set()
    kept: list[str] = []
    for r in exclude_roots:
        rid = _path_id(r)
        norm = os.path.normpath(r)
        above = (
            _at_or_above(rid, root)
            if rid is not None
            else norm == root or root.startswith(norm + os.sep)
        )
        if above:
            _log.warning(
                "orphan sweep: ignoring the exclude root %r — it is at or above the music "
                "root %r, and excluding it would exclude the whole library",
                r,
                root,
            )
            continue
        if rid is not None:
            ids.add(rid)
        kept.append(norm)
    return frozenset(ids), tuple(kept)


def _scan_tree(
    root: str, excluded: _Exclusion
) -> tuple[dict[str, bool], dict[str, bool], dict[str, bool]]:
    """Bottom-up fold: ``has_audio`` / ``has_file`` / ``has_own_audio`` per dir.

    ``has_audio`` folds in descendants; ``has_own_audio`` is audio DIRECTLY in
    the dir (a live album). Excluded subtrees are never recorded and count as no
    audio for their parent. The walk is TOP-DOWN so ``dirnames`` can be pruned
    (an excluded root is identified once, by inode); the fold runs in reverse.

    A directory ``os.walk`` cannot read is marked audio-BEARING, not ignored:
    ``music/Perm/Album`` at mode 000 with ``music/Perm/cover.jpg`` beside it made
    ``Perm`` read as a husk whose audio the mover trashed.
    """
    has_audio: dict[str, bool] = {}
    has_file: dict[str, bool] = {}
    has_own_audio: dict[str, bool] = {}
    unreadable: list[str] = []

    def _note(err: OSError) -> None:
        name = getattr(err, "filename", None)
        if name:
            unreadable.append(os.path.normpath(os.fsdecode(name)))

    levels: list[tuple[str, list[str], list[str]]] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=_note):
        dp = os.path.normpath(dirpath)
        if excluded(dp):
            dirnames[:] = []
            continue
        levels.append((dp, list(dirnames), filenames))
    # Seeded BEFORE the fold, so the parent's roll-up picks it up like any other
    # child: the failing path is a directory the walk never yielded.
    for bad in unreadable:
        has_audio[bad] = True
        has_file[bad] = True
    for dp, dirnames, filenames in reversed(levels):
        own_audio = any(_is_audio(f) for f in filenames)
        audio = own_audio
        has_files = bool(filenames)
        for d in dirnames:
            child = os.path.normpath(os.path.join(dp, d))
            audio = audio or has_audio.get(child, False)
            has_files = has_files or has_file.get(child, False)
        has_audio[dp] = audio
        has_file[dp] = has_files
        has_own_audio[dp] = own_audio
    return has_audio, has_file, has_own_audio


def _library_orphans(root: str, excluded: _Exclusion) -> list[str]:
    has_audio, has_file, has_own_audio = _scan_tree(root, excluded)
    out: list[str] = []
    for dp, audio in has_audio.items():
        if dp == root or audio or not has_file[dp]:
            continue  # root / has audio / truly empty (beets prunes empties)
        if os.path.basename(dp).lower() in ART_DIR_NAMES:
            continue  # well-known art/booklet folder (protects multi-disc album art)
        parent = os.path.dirname(dp)
        # A live album dir holds audio files DIRECTLY; its audio-empty subdirs
        # (``Album/Scans/``, ``Album/Artwork/`` booklet scans) are the album's own
        # art, NOT stale husks — never trash them. A genuine husk's parent is an
        # artist container whose audio comes only from OTHER album subdirs (no
        # own-audio), so it still flags.
        if has_own_audio.get(parent, False):
            continue
        # Top-most husk = audio-empty dir whose parent is "kept" (has audio, or is
        # the root). If the parent is itself audio-empty it will be reported instead.
        if parent == root or has_audio.get(parent, False):
            out.append(dp)
    return out


def _subtree(dirpath: str, excluded: _Exclusion) -> tuple[bool, bool]:
    """(has_audio, has_file) for a single subtree (used by seeds mode).

    A walk error answers audio-bearing, the same direction :func:`_scan_tree`
    takes and for the same measured reason: an unreadable album dir under the
    seed's ancestor made that ancestor read as a husk.
    """
    has_files = False
    failed = False

    def _note(_err: OSError) -> None:
        nonlocal failed
        failed = True

    for d, dirs, files in os.walk(dirpath, onerror=_note):
        dn = os.path.normpath(d)
        if excluded(dn):
            dirs[:] = []
            continue
        if files:
            has_files = True
            if any(_is_audio(f) for f in files):
                return True, True
    if failed:
        return True, True
    return False, has_files


def _nearest_existing_dir(seed: str) -> str | None:
    """The nearest ancestor of ``seed`` that is a directory (the seed may be pruned)."""
    d = os.path.normpath(seed)
    while d and not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return d


def _climb_to_husk(start: str, root: str, excluded: _Exclusion) -> str | None:
    """The top-most audio-empty (non-empty) ancestor of ``start`` below ``root``."""
    d = start
    candidate: str | None = None
    while _under(d, root):
        if excluded(d):  # an excluded ancestor stops the climb; never a candidate
            break
        if os.path.islink(d):
            break
        has_audio, has_file = _subtree(d, excluded)
        if has_audio:
            break
        if has_file:
            candidate = d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return candidate


def _seed_orphan(seed: str, root: str, excluded: _Exclusion) -> str | None:
    """The top-most audio-empty (non-empty) ancestor of ``seed`` below ``root``.

    Starts at the nearest EXISTING ancestor — the seed may have been pruned — and
    the climb stops at a SYMLINK: ``shutil.move`` moves the link, so everything
    beneath it leaves the library at once and the Trash row is unrestorable
    (``_record_origin`` writes nothing for a symlinked destination). Measured on
    the parent commit: the symlink itself was reported and the mover took it.

    A seed spelled through another alias of the walk root (an in-place import's
    row keeps the spelling it was imported from) is RESPELLED by identity before
    the climb; without that the climb never started and the husk was reported in
    library mode only.

    ASYMMETRY, deliberate: ``os.path.isdir`` follows links, so the nearest
    existing ancestor can sit BELOW one and this mode reports a directory library
    mode never descends into (``os.walk`` does not follow links). Measured, the
    mover then relocates the real directory and writes the origin in the link
    spelling; restore lands it on the library volume if the link is gone by then.
    Refusing to climb through a link would make this mode blind under a
    legitimately symlinked subtree (``music/artists -> /mnt/big/artists``), which
    is the only mode that sweeps there at all — so the reach is kept and named.
    """
    d = _nearest_existing_dir(seed)
    if d is None:
        return None
    if not _under(d, root):
        respelled = _in_walk_spelling(d, root)
        if respelled is None:
            return None
        d = respelled
    return _climb_to_husk(d, root, excluded)


class _Exclusion:
    """Whether a walked directory is outside the sweep — and which roots were hit.

    Three ways out, in cost order: inside an already-matched subtree (a prefix,
    but on strings this walk produced); the directory IS an exclude root, by
    ``(st_dev, st_ino)``, which is the test that sees an alias
    (:func:`_exclude_ids`); a dotdir or a known NAS/OS name.

    ``hits`` is the WALK's spelling of each matched root, which is what
    :func:`_drop_excluded_ancestors` needs — the caller may spell it otherwise.
    """

    def __init__(self, root: str, exclude_ids: frozenset[PathId], spelled: tuple[str, ...]) -> None:
        self._root = root
        self._ids = exclude_ids
        # The caller's own spelling, kept beside the walk's: a root the walk
        # never reaches (it sits under a symlinked component, which ``os.walk``
        # does not descend) still has to spare its ancestors.
        self.hits: list[str] = [os.path.normpath(s) for s in spelled]

    def __call__(self, dp: str) -> bool:
        for h in self.hits:
            if dp == h or dp.startswith(h + os.sep):
                return True
        if self._ids and _path_id(dp) in self._ids:
            self.hits.append(dp)
            return True
        rel = os.path.relpath(dp, self._root)
        return rel != os.curdir and any(_skip_name(seg) for seg in rel.split(os.sep))


def _seeded_orphans(seeds: list[Path], root: str, excluded: _Exclusion) -> list[str]:
    """Each seed's top-most audio-empty ancestor below ``root``, deduped in
    seed order (seeds mode of :func:`find_orphan_folders`)."""
    seen: set[str] = set()
    raw: list[str] = []
    for seed in seeds:
        hit = _seed_orphan(os.path.normpath(str(seed)), root, excluded)
        if hit is not None and hit not in seen:
            seen.add(hit)
            raw.append(hit)
    return raw


def _top_most(paths: list[str]) -> list[str]:
    """Keep only the top-most of the set."""
    # Drop any path that is a descendant of another in the set (keep top-most).
    return [p for p in paths if not any(p != q and _under(p, q) for q in paths)]


def _ancestor_chain(roots: Collection[str]) -> set[str]:
    """Every strict ancestor of every path in ``roots``, folded once.

    A set lookup rather than a scan per candidate, so both callers stay
    O(candidates) however many roots they were handed. Stops at the filesystem
    root (``dirname('/') == '/'``), and skips a chain already walked — two
    siblings share everything above them.
    """
    ancestors: set[str] = set()
    for root in roots:
        d = os.path.dirname(os.path.normpath(root))
        while d and d not in ancestors:
            ancestors.add(d)
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    return ancestors


def _drop_excluded_ancestors(raw: list[str], exclude_roots: tuple[str, ...]) -> list[str]:
    """Drop any candidate that CONTAINS an excluded root.

    The mover takes a reported folder WITH ITS SUBTREE, so reporting an ancestor
    of an excluded dir hands that dir over. It happens because an excluded
    subtree is never recorded and so contributes no audio above it: measured on
    ``d65e635`` with ``ignore_dirs=(<M>/data/exports,)`` and a stray
    ``<M>/data/notes.txt``, ``find_orphan_folders`` returned ``data``; without
    the stray file, ``[]``.

    The whole ancestor CHAIN, not the immediate parent — sparing one level moves
    the report one level up. COST: the dropped candidate is the top of an
    audio-empty run and nothing below it is re-selected, so a real husk beside an
    excluded root is kept
    (``test_a_husk_under_an_audio_free_ancestor_of_an_ignored_dir_is_kept``).
    The ancestor need not be audio-free on disk — measured, an ancestor whose
    only audio sits INSIDE the excluded root reads as audio-free here, since an
    excluded subtree is never recorded.
    """
    if not exclude_roots:
        return raw
    ancestors = _ancestor_chain(exclude_roots)
    return [dp for dp in raw if dp not in ancestors]


def _drop_protected(raw: list[str], protected_dirs: Collection[str]) -> list[str]:
    """Drop the candidates a LIVE album owns (see :func:`find_orphan_folders`).

    Three ways ``dp`` can belong to an album root: it IS one, its PARENT is one
    (the album's own booklet folder, whatever its basename), or a root lies
    strictly under it (a lone album whose audio is gone reports the ARTIST dir).
    The ancestor test is a set lookup over each root's folded chain, so the
    filter stays O(candidates).

    All three run in the same ``(st_dev, st_ino)``-beside-the-string namespace
    the exclusion uses: with the walk root a symlink, the string form reported
    the live album's ``Scans (LP)`` folder and the link-spelled set did not.
    ``live_album_roots`` respells a foreign row into the walk's namespace, so
    the identity arm here is a second reader rather than the only one.
    """
    if not protected_dirs:
        return raw
    roots = {os.path.normpath(p) for p in protected_dirs}
    ancestors = _ancestor_chain(roots)
    root_ids = {i for i in (_path_id(p) for p in roots) if i is not None}
    ancestor_ids = {i for i in (_path_id(p) for p in ancestors) if i is not None}

    def kept(dp: str) -> bool:
        parent = os.path.dirname(dp)
        if dp in roots or parent in roots or dp in ancestors:
            return False
        dp_id = _path_id(dp)
        if dp_id is not None and (dp_id in root_ids or dp_id in ancestor_ids):
            return False
        parent_id = _path_id(parent)
        return not (parent_id is not None and parent_id in root_ids)

    return [dp for dp in raw if kept(dp)]


def find_orphan_folders(
    music_dir: Path,
    *,
    seeds: list[Path] | None,
    trash_dir: Path,
    ignore_dirs: tuple[Path, ...] = (),
    protected_dirs: Collection[str] = (),
) -> list[Path]:
    """Top-most audio-empty, non-empty folders under ``music_dir`` to move to Trash.

    ``seeds is None`` -> scan the whole library. Otherwise seed from the vacated
    dirs and climb to each one's top-most audio-empty ancestor. Deduped, no path
    an ancestor of another, root excluded. ``ignore_dirs`` are extra roots to
    skip; dotdirs and known NAS/OS names are skipped by name.

    A returned path is not at, below or ABOVE ``trash_dir`` or any
    ``ignore_dirs`` entry (:func:`_drop_excluded_ancestors`), compared by
    ``(st_dev, st_ino)`` where the root has one, so a symlink and a bind mount
    both hold (:func:`_exclude_ids`); a root with no identity — absent, or
    unreadable — is compared by its normalised SPELLING, which an alias defeats.
    ``protected_dirs`` are LIVE album roots: nothing at, directly under, or above
    one is returned.

    Two behaviours err toward keeping, both pinned in ``tests/test_orphans.py``:
    an exclude root at or above ``music_dir`` is DROPPED with a WARNING (it used
    to match every candidate and return nothing), and a husk beside an excluded
    root is kept. The residual list is the BACKLOG entry for this slice.
    """
    root = os.path.normpath(str(music_dir))
    exclude_ids, spelled = _exclude_ids(root, tuple(str(d) for d in (trash_dir, *ignore_dirs)))
    excluded = _Exclusion(root, exclude_ids, spelled)
    if seeds is None:
        raw = _library_orphans(root, excluded)
    else:
        raw = _seeded_orphans(seeds, root, excluded)
    # ``excluded.hits`` and not ``spelled``: the walk's own spelling of every root
    # it matched, which is the one the candidates are in.
    kept = _top_most(
        _drop_protected(_drop_excluded_ancestors(raw, tuple(excluded.hits)), protected_dirs)
    )
    return sorted(Path(p) for p in kept)
