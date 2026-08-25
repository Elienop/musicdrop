"""Find orphan "husk" folders: directories under the music root that hold only
art/sidecars (no audio anywhere beneath). Pure filesystem — audio is detected by
file extension on disk, NOT via the beets DB, so a folder containing an
*untracked* audio file is never reported. Inside the beets-adapter boundary
(CLAUDE.md rule 3) for proximity to trash.py, though it touches no beets API.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

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
# genuine husk (structurally identical). Skipping these names is the backstop.
# False-negative-only: a stale husk that happens to bear one of these names is
# merely left in place — the module errs toward keeping.
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


def find_orphan_folders(
    music_dir: Path,
    *,
    seeds: list[Path] | None,
    trash_dir: Path,
    ignore_dirs: tuple[Path, ...] = (),
) -> list[Path]:
    """Top-most audio-empty, non-empty folders under ``music_dir`` to move to Trash.

    ``seeds is None`` -> scan the whole library (clears the backlog). Otherwise seed
    from the given (vacated) dirs and climb to each one's top-most audio-empty
    ancestor (a renamed husk is a sibling of the new folder). Never returns the root
    or anything inside ``trash_dir``; deduped, with no path that is an ancestor of
    another in the result.

    ``ignore_dirs`` are extra absolute roots to skip (e.g. the playlists export
    dir). Directories whose name is a dotdir or a known NAS/OS housekeeping name are
    always skipped.
    """
    root = os.path.normpath(str(music_dir))
    exclude_roots = tuple(os.path.normpath(str(d)) for d in (trash_dir, *ignore_dirs))
    excluded = _excluded_predicate(root, exclude_roots)
    if seeds is None:
        raw = _library_orphans(root, excluded)
    else:
        raw = _seeded_orphans(seeds, root, excluded)
    kept = _top_most(raw)
    return sorted(Path(p) for p in kept)
