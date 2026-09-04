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
from collections.abc import Callable, Collection
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
    """True if ``path`` is strictly inside ``root``."""
    return path != root and path.startswith(root + os.sep)


def _real(path: str) -> str:
    """``path`` with every symlink collapsed, normalised, and made absolute.

    ``os.path.realpath`` and not ``Path.resolve``: it returns ``str`` (what this
    module compares) and does not raise on a symlink loop — it stops at the loop
    and hands back a path, which for an exclusion root is the safe direction. A
    RELATIVE root is joined to the process CWD, the same join ``open()`` makes
    when ``app.playlists.reexport.export_dir_for`` writes an ``.m3u8`` into it.
    """
    return os.path.normpath(os.path.realpath(path))


def _exclude_roots_for_walk(root: str, exclude_roots: tuple[str, ...]) -> tuple[str, ...]:
    """Each exclude root as ``os.walk`` will spell it under ``root``, or dropped.

    The walk root is beets' ``lib.directory``, which is ``normpath``'d and NOT
    realpath'd (``beets/util/__init__.py:178``), while the exclusion roots reach
    this module already resolved — so under ``/music -> /mnt/tank/music``, the
    Docker norm, the two sides are different strings for the same directory and
    every string comparison in this module misses. Measured in the review round:
    with a symlinked library the beets data dir was REPORTED, and moving the
    reported folder took ``library.db`` and ``config.yaml`` with it.

    Translating the roots INTO the walk's spelling — rather than realpath'ing
    every directory the walk yields — costs one ``realpath`` per exclusion root
    instead of one per directory, and it is exact: ``os.walk`` does not descend
    into symlinked directories, so every ``dirpath`` it yields is ``root``
    followed by real components, and ``realpath(dirpath)`` is ``realpath(root)``
    followed by the same ones.

    Two roots are dropped rather than translated:

    * one that resolves OUTSIDE the walked tree — the beets data dir on the
      shipped layout, for one. Housekeeping rather than behaviour: removing this
      arm leaves the suite green, because translating such a root produces a
      spelling that is still outside the tree and ``os.walk`` yields nothing
      outside ``root``. It keeps the tuple to paths the walk can produce, and
      keeps ``_ancestor_chain`` from folding in every directory up to ``/``;
    * one at or ABOVE the walk root, which would match EVERY walked directory
      (``_excluded_predicate`` is a prefix test) and return an empty sweep for
      the whole library. Before this function it was a silent no-op, so it gets
      the one WARNING line this module logs. ``MUSICDROP_PLAYLISTS_EXPORT_DIR`` is the reachable
      way in: it is an operator-set absolute path with no rule of its own
      (``app.beets.store_layout`` refuses a Trash or origin store there).
    """
    real_root = _real(root)
    kept: list[str] = []
    for r in exclude_roots:
        real_r = _real(r)
        if real_r == real_root or _under(real_root, real_r):
            _log.warning(
                "orphan sweep: ignoring the exclude root %r — it is at or above the music "
                "root %r, and excluding it would exclude the whole library",
                r,
                root,
            )
            continue
        if not _under(real_r, real_root):
            continue
        kept.append(os.path.normpath(os.path.join(root, os.path.relpath(real_r, real_root))))
    return tuple(kept)


def _scan_tree(
    root: str, excluded: Callable[[str], bool]
) -> tuple[dict[str, bool], dict[str, bool], dict[str, bool]]:
    """Bottom-up walk: ``has_audio[dir]`` / ``has_file[dir]`` / ``has_own_audio[dir]``
    for every dir at/under ``root``. ``has_audio`` folds in descendants; ``has_own_audio``
    is audio DIRECTLY in the dir (marks a live album dir). Excluded subtrees (trash,
    ignore-dirs, dotdirs/NAS names) are skipped entirely (never recorded, never
    counted as audio for their parent)."""
    has_audio: dict[str, bool] = {}
    has_file: dict[str, bool] = {}
    has_own_audio: dict[str, bool] = {}
    for dirpath, dirnames, filenames in os.walk(root, topdown=False, onerror=lambda _e: None):
        dp = os.path.normpath(dirpath)
        if excluded(dp):
            continue
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


def _library_orphans(root: str, excluded: Callable[[str], bool]) -> list[str]:
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


def _subtree(dirpath: str, excluded: Callable[[str], bool]) -> tuple[bool, bool]:
    """(has_audio, has_file) for a single subtree (used by seeds mode)."""
    audio = False
    has_files = False
    for d, _dirs, files in os.walk(dirpath, onerror=lambda _e: None):
        dn = os.path.normpath(d)
        if excluded(dn):
            continue
        if files:
            has_files = True
            if any(_is_audio(f) for f in files):
                return True, True
    return audio, has_files


def _seed_orphan(seed: str, root: str, excluded: Callable[[str], bool]) -> str | None:
    """The top-most audio-empty (and non-empty) ancestor of ``seed`` below ``root``,
    or None. Starts at the nearest existing ancestor (the seed itself may have been
    pruned)."""
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


def _excluded_predicate(root: str, exclude_roots: tuple[str, ...]) -> Callable[[str], bool]:
    """Whether a dir is outside the sweep: inside a trash/ignore root, or under a
    dotdir / known NAS/OS housekeeping dir name (relative to ``root``)."""

    def excluded(dp: str) -> bool:
        for r in exclude_roots:
            if dp == r or dp.startswith(r + os.sep):
                return True
        rel = os.path.relpath(dp, root)
        return rel != os.curdir and any(_skip_name(seg) for seg in rel.split(os.sep))

    return excluded


def _seeded_orphans(seeds: list[Path], root: str, excluded: Callable[[str], bool]) -> list[str]:
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

    ``_excluded_predicate`` keeps the sweep out of Trash and every ``ignore_dirs``
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
    where the other direction moves an excluded dir into Trash. A husk whose
    parent holds audio directly, or whose parent is the root, is outside the run
    and is still reported; both cases are pinned in ``tests/test_orphans.py``.
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
    """
    if not protected_dirs:
        return raw
    roots = {os.path.normpath(p) for p in protected_dirs}
    ancestors = _ancestor_chain(roots)
    return [
        dp
        for dp in raw
        if dp not in roots and os.path.dirname(dp) not in roots and dp not in ancestors
    ]


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
    origin store, the beets data dir). Directories whose name is a dotdir or a
    known NAS/OS housekeeping name are skipped by name.

    What the exclusion is measured to give (``tests/test_orphans.py``), and the
    two places it stops:

    * a returned path is not at, below or above ``trash_dir`` or any
      ``ignore_dirs`` entry — the mover takes a reported folder's whole subtree,
      so an ancestor of an excluded dir would hand that dir over anyway (see
      :func:`_drop_excluded_ancestors`). Both modes get it from the same drop:
      seeds mode stops its climb AT an excluded ancestor, which is a different
      guard, because the candidate it has already banked below that stop can
      still be an ancestor of a different excluded dir;
    * the comparison runs on ``realpath`` forms on both sides
      (:func:`_exclude_roots_for_walk`), so it holds whichever side arrives
      through a symlink;
    * RESIDUAL — an exclude root at or above ``music_dir`` is DROPPED with a
      WARNING rather than excluding the whole library, so directories under such
      a root are reported like any other. ``app.beets.store_layout`` refuses that
      position for the Trash, the origin store and the beets data dir, which
      leaves ``MUSICDROP_PLAYLISTS_EXPORT_DIR`` as the way to reach it;
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
    exclude_roots = _exclude_roots_for_walk(root, tuple(str(d) for d in (trash_dir, *ignore_dirs)))
    excluded = _excluded_predicate(root, exclude_roots)
    if seeds is None:
        raw = _library_orphans(root, excluded)
    else:
        raw = _seeded_orphans(seeds, root, excluded)
    kept = _top_most(_drop_protected(_drop_excluded_ancestors(raw, exclude_roots), protected_dirs))
    return sorted(Path(p) for p in kept)
