"""Find orphan "husk" folders: directories under the music root that hold only
art/sidecars (no audio anywhere beneath). Pure filesystem — audio is detected by
file extension on disk, NOT via the beets DB, so a folder containing an
*untracked* audio file is never reported. Inside the beets-adapter boundary
(CLAUDE.md rule 3) for proximity to trash.py, though it touches no beets API:
the DB-derived protection a caller may pass as ``protected_dirs`` is computed
over in ``app.beets.reorganize.live_album_roots`` and arrives here as plain paths.
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
    prefix becomes ``'//'`` and every path reads as OUTSIDE. Measured before this:
    an exclude root of ``/`` was dropped with no WARNING at all, which is the one
    line this module logs, while every other at-or-above root logged exactly one.
    """
    if path == root:
        return False
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # a relative and an absolute path have nothing in common
        return False


#: ``(st_dev, st_ino)``, the pair this module decides sameness by — the same
#: question ``app.beets.store_layout._same_path`` asks, for the same reason.
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


def _exclude_ids(
    root: str, exclude_roots: tuple[str, ...]
) -> tuple[frozenset[PathId], tuple[str, ...]]:
    """The identity of every exclude root the walk should stop at, and their spellings.

    Identity and not a path prefix, because a path prefix cannot see an ALIAS.
    Two aliases were measured to defeat the string form:

    * a symlinked library — ``/music -> /mnt/tank/music`` — where the walk root
      is beets' ``lib.directory`` (``normpath``'d, not realpath'd) while the
      exclusion roots arrive resolved. The previous fix translated the roots into
      the walk's spelling with ``realpath``, which handled this one;
    * a BIND MOUNT of a directory inside the library at the Trash path
      (``-v /srv/music:/music`` plus ``-v /srv/music/Trash:/trash``). ``realpath``
      does not collapse a bind mount, so the translation dropped the Trash as
      "outside the tree", the sweep reported the Trash itself as a husk, and the
      mover deleted every entry in it — measured under ``unshare -Urm``, with the
      copy-into-itself fallback ``shutil.move`` takes when ``rename`` returns
      EXDEV. Walking the ALIAS's ancestors cannot find the library either: a
      mount point's ancestors are the mount point's, never the source's. Only
      comparing the root's own inode against each walked directory sees it.

    A root that cannot be stat'd contributes no identity — it is not there (or
    not readable), so ``os.walk`` will not reach it either — but it keeps its
    SPELLING, which is what spares its ancestors from being reported.

    A root at or ABOVE the walk root is dropped entirely, with the one WARNING
    this module logs: excluding it would exclude the whole library.
    ``MUSICDROP_PLAYLISTS_EXPORT_DIR`` is the reachable way in — an operator-set
    absolute path with no rule of its own (``app.beets.store_layout`` refuses a
    Trash, origin store or beets dir there).
    """
    ids: set[PathId] = set()
    kept: list[str] = []
    for r in exclude_roots:
        rid = _path_id(r)
        if rid is not None and _at_or_above(rid, root):
            _log.warning(
                "orphan sweep: ignoring the exclude root %r — it is at or above the music "
                "root %r, and excluding it would exclude the whole library",
                r,
                root,
            )
            continue
        if rid is not None:
            ids.add(rid)
        kept.append(os.path.normpath(r))
    return frozenset(ids), tuple(kept)


def _scan_tree(
    root: str, excluded: _Exclusion
) -> tuple[dict[str, bool], dict[str, bool], dict[str, bool]]:
    """Bottom-up fold: ``has_audio[dir]`` / ``has_file[dir]`` / ``has_own_audio[dir]``
    for every dir at/under ``root``. ``has_audio`` folds in descendants; ``has_own_audio``
    is audio DIRECTLY in the dir (marks a live album dir). Excluded subtrees (trash,
    ignore-dirs, dotdirs/NAS names) are skipped entirely (never recorded, never
    counted as audio for their parent).

    The walk itself is TOP-DOWN so ``dirnames`` can be pruned — an excluded root
    is then identified once, by inode, rather than re-tested for every directory
    beneath it — and the fold runs over the collected list in reverse, which is
    the bottom-up order the ``has_audio`` roll-up needs.

    A directory ``os.walk`` cannot read is marked audio-BEARING rather than
    ignored. It used to be swallowed by ``onerror``, so it was never recorded and
    contributed no audio to its parent: measured, ``music/Perm/Album`` at mode 000
    with ``music/Perm/cover.jpg`` beside it made ``Perm`` read as a husk, and the
    mover relocated the audio into Trash. Err toward keeping is this module's
    posture, and "I could not look" is not "there is nothing there".
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
    audio = False
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
    return audio, has_files


def _seed_orphan(seed: str, root: str, excluded: _Exclusion) -> str | None:
    """The top-most audio-empty (and non-empty) ancestor of ``seed`` below ``root``,
    or None. Starts at the nearest existing ancestor (the seed itself may have been
    pruned).

    The climb stops at a SYMLINK. A symlink is not a husk the mover may relocate:
    ``shutil.move`` moves the link, so every path beneath it leaves the library
    at once and the Trash row it makes is not restorable (``trash._record_origin``
    writes no record for a symlinked destination, and the listing renders such an
    entry "refused"). Measured on the parent commit, where a library with a
    symlinked component and an exclude root spelled through it reported the
    symlink itself and the mover took it.
    """
    d = os.path.normpath(seed)
    while d and not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
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


class _Exclusion:
    """Whether a walked directory is outside the sweep — and which roots were hit.

    Three ways out, asked in this order because they cost that order:

    1. inside a subtree already matched (a string prefix, but on strings this
       walk itself produced, so no spelling question arises);
    2. the directory IS an exclude root, by ``(st_dev, st_ino)``. This is the one
       that sees an alias — a bind mount or a symlinked library — where a path
       prefix does not (see :func:`_exclude_ids`);
    3. a dotdir or a known NAS/OS housekeeping name, relative to ``root``.

    ``hits`` is the walk's own spelling of every root matched, which is what
    :func:`_drop_excluded_ancestors` needs: an ancestor of an excluded dir may
    not be spelled the way the CALLER spelled that dir, and the mover takes a
    reported folder's whole subtree.
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

    :class:`_Exclusion` keeps the sweep out of Trash and every ``ignore_dirs``
    subtree, and that is the whole guard only if a report is the thing that gets
    trashed. It is not: the mover takes the reported folder WITH ITS SUBTREE, so
    reporting an ancestor of an excluded dir hands over the excluded dir too.

    It happens for one reason: an excluded subtree is never recorded, so it
    contributes no audio to the dir above it. Give that dir a non-audio file of
    its own and it reads as a husk. Measured on ``d65e635`` with
    ``ignore_dirs=(<M>/data/exports,)`` and a stray ``<M>/data/notes.txt`` —
    ``find_orphan_folders`` returned ``data``, whose subtree holds the exports;
    remove the stray file and it returned ``[]`` (an only-child parent records no
    ``has_file`` and empty dirs are skipped), which is why the hole needed a file
    to show at all.

    Sparing the whole ancestor CHAIN, not just the immediate parent, is what
    makes this a guard rather than a nudge: sparing one level only moves the
    report one level up when the excluded dir is nested deeper. Same shape (and
    the same helper) as :func:`_drop_protected`'s live-album-root ancestors.

    What it costs: the dropped candidate is the TOP of an audio-empty run, and
    nothing below it is re-selected, so a real husk under an audio-free ancestor
    of an excluded root is kept rather than reported. Err toward keeping is this
    module's posture — a husk left alone is a folder somebody deletes by hand,
    where the other direction moves an excluded dir into Trash.

    Outside the run — still reported — are a husk whose parent IS the root, and
    (library mode) one whose parent holds audio somewhere BENEATH it while
    holding none directly. Both are pinned in ``tests/test_orphans.py``; the
    second is the shape of ``test_a_husk_under_an_audio_BEARING_ancestor_...``.
    A parent holding audio DIRECTLY does not reach this filter in library mode at
    all — ``_library_orphans``' multi-disc art guard has already skipped the
    child — which is measured, and is why that shape is not named here.
    """
    if not exclude_roots:
        return raw
    ancestors = _ancestor_chain(exclude_roots)
    return [dp for dp in raw if dp not in ancestors]


def _drop_protected(raw: list[str], protected_dirs: Collection[str]) -> list[str]:
    """Drop the candidates a LIVE album owns (see :func:`find_orphan_folders`).

    Three ways a candidate ``dp`` can belong to an album whose root is in the set:
    ``dp`` IS a root (a live album whose audio has vanished from disk — err toward
    keeping until a disk-sync says otherwise), ``dp``'s PARENT is a root (the album's
    own ``Scans (LP)``/booklet folder, whatever its basename — the case ART_DIR_NAMES
    could not cover), or a root lies strictly UNDER ``dp`` (``dp`` is an ancestor:
    a lone album whose audio is gone reports the whole ARTIST dir, and trashing that
    would take the live album's folder with it).

    The ancestor test is a set lookup, not a scan of the roots: every root's ancestor
    chain is folded once, so the filter stays O(candidates) however large the set.

    All three comparisons run in the SAME identity namespace the exclusion does —
    ``(st_dev, st_ino)`` beside the string. A protected root can arrive in a
    different spelling from the walk's: measured with the walk root a symlink
    (``music -> tank/music``) and ``protected_dirs={<real>/Live/Box}``, the string
    form reported the live album's ``Scans (LP)`` folder, where the link-spelled
    set did not.

    Note what this does NOT reach: ``app.beets.reorganize.live_album_roots`` filters
    a foreign-spelled row out of the set BEFORE this function is called, with a
    lexical containment test of its own. That is upstream of this module and is
    recorded as a residual rather than fixed here.
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

    ``seeds is None`` -> scan the whole library (clears the backlog). Otherwise seed
    from the given (vacated) dirs and climb to each one's top-most audio-empty
    ancestor (a renamed husk is a sibling of the new folder). The result is
    deduped, holds no path that is an ancestor of another, and excludes the root
    itself.

    ``ignore_dirs`` are extra roots to skip (the playlists export dir, the Trash
    origin store, the beets data dir, and the directory holding the beets
    database). Directories whose name is a dotdir or a known NAS/OS housekeeping
    name are skipped by name.

    What the exclusion is measured to give (``tests/test_orphans.py``), and where
    it stops:

    * a returned path is not at, below or above ``trash_dir`` or any
      ``ignore_dirs`` entry — the mover takes a reported folder's whole subtree,
      so an ancestor of an excluded dir would hand that dir over anyway (see
      :func:`_drop_excluded_ancestors`). Both modes get it from the same drop:
      seeds mode stops its climb AT an excluded ancestor, which is a different
      guard, because the candidate it has already banked below that stop can
      still be an ancestor of a different excluded dir;
    * the comparison is by ``(st_dev, st_ino)`` (:func:`_exclude_ids`), so it
      holds whichever side arrives through a symlink AND whichever side arrives
      through a bind mount — the two aliases measured to defeat the string form;
    * RESIDUAL — an exclude root at or above ``music_dir`` is DROPPED with a
      WARNING rather than excluding the whole library, so directories under such
      a root are reported like any other. It is a deliberate change of DIRECTION:
      measured on the parent commit, such a root matched every candidate and the
      sweep returned nothing, so a misconfiguration did nothing; now it sweeps.
      Two settings reach the position — ``MUSICDROP_PLAYLISTS_EXPORT_DIR``, which
      ``export_dir_for`` hands through unchanged, and ``library:``, whose parent
      directory joins the list and has no rule against sitting above ``M``.
      ``app.beets.store_layout`` refuses the position for the Trash, the origin
      store and the beets data dir;
    * RESIDUAL — a husk sitting beside an excluded root under an ancestor that
      holds no audio ANYWHERE is kept rather than reported: the ancestor drop
      removes the top of the audio-empty run and nothing below it is re-selected.
      That is this module's err-toward-keeping posture, pinned in
      ``test_a_husk_under_an_audio_free_ancestor_of_an_ignored_dir_is_kept``.

    ``protected_dirs`` are normalized absolute dirs owned by LIVE beets albums
    (``app.beets.reorganize.live_album_roots``): nothing at, directly under, or above
    one of them is ever returned, in either mode. Empty by default, so a caller that
    knows nothing about the DB keeps exactly the old behaviour.
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
