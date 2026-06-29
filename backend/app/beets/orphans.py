"""Find orphan "husk" folders: directories under the music root that hold only
art/sidecars (no audio anywhere beneath). Pure filesystem — audio is detected by
file extension on disk, NOT via the beets DB, so a folder containing an
*untracked* audio file is never reported. Inside the beets-adapter boundary
(CLAUDE.md rule 3) for proximity to trash.py, though it touches no beets API.
"""

from __future__ import annotations

import os
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


def _under(path: str, root: str) -> bool:
    """True if ``path`` is strictly inside ``root``."""
    return path != root and path.startswith(root + os.sep)


def _scan_tree(root: str, trash_norm: str) -> tuple[dict[str, bool], dict[str, bool]]:
    """Bottom-up walk: ``has_audio[dir]`` / ``has_file[dir]`` for every dir at/under
    ``root``. The ``trash`` subtree is skipped entirely (never recorded, never
    counted as audio for its parent)."""
    has_audio: dict[str, bool] = {}
    has_file: dict[str, bool] = {}
    for dirpath, dirnames, filenames in os.walk(root, topdown=False, onerror=lambda _e: None):
        dp = os.path.normpath(dirpath)
        if dp == trash_norm or dp.startswith(trash_norm + os.sep):
            continue
        audio = any(_is_audio(f) for f in filenames)
        has_files = bool(filenames)
        for d in dirnames:
            child = os.path.normpath(os.path.join(dp, d))
            audio = audio or has_audio.get(child, False)
            has_files = has_files or has_file.get(child, False)
        has_audio[dp] = audio
        has_file[dp] = has_files
    return has_audio, has_file


def _library_orphans(root: str, trash_norm: str) -> list[str]:
    has_audio, has_file = _scan_tree(root, trash_norm)
    out: list[str] = []
    for dp, audio in has_audio.items():
        if dp == root or audio or not has_file[dp]:
            continue  # root / has audio / truly empty (beets prunes empties)
        parent = os.path.dirname(dp)
        # Top-most husk = audio-empty dir whose parent is "kept" (has audio, or is
        # the root). If the parent is itself audio-empty it will be reported instead.
        if parent == root or has_audio.get(parent, False):
            out.append(dp)
    return out


def _subtree(dirpath: str, trash_norm: str) -> tuple[bool, bool]:
    """(has_audio, has_file) for a single subtree (used by seeds mode)."""
    audio = False
    has_files = False
    for d, _dirs, files in os.walk(dirpath, onerror=lambda _e: None):
        dn = os.path.normpath(d)
        if dn == trash_norm or dn.startswith(trash_norm + os.sep):
            continue
        if files:
            has_files = True
            if any(_is_audio(f) for f in files):
                return True, True
    return audio, has_files


def _seed_orphan(seed: str, root: str, trash_norm: str) -> str | None:
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
        has_audio, has_file = _subtree(d, trash_norm)
        if has_audio:
            break
        if has_file:
            candidate = d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return candidate


def find_orphan_folders(
    music_dir: Path, *, seeds: list[Path] | None, trash_dir: Path
) -> list[Path]:
    """Top-most audio-empty, non-empty folders under ``music_dir`` to move to Trash.

    ``seeds is None`` -> scan the whole library (clears the backlog). Otherwise seed
    from the given (vacated) dirs and climb to each one's top-most audio-empty
    ancestor (a renamed husk is a sibling of the new folder). Never returns the root
    or anything inside ``trash_dir``; deduped, with no path that is an ancestor of
    another in the result.
    """
    root = os.path.normpath(str(music_dir))
    trash_norm = os.path.normpath(str(trash_dir))
    if seeds is None:
        raw = _library_orphans(root, trash_norm)
    else:
        seen: set[str] = set()
        raw = []
        for seed in seeds:
            hit = _seed_orphan(os.path.normpath(str(seed)), root, trash_norm)
            if hit is not None and hit not in seen:
                seen.add(hit)
                raw.append(hit)
    # Drop any path that is a descendant of another in the set (keep top-most).
    kept = [p for p in raw if not any(p != q and _under(p, q) for q in raw)]
    return sorted(Path(p) for p in kept)
