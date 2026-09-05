"""The app's own directories, by inode, refused at the point of destruction.

``store_layout`` compares SPELLINGS. A bind mount whose mount point is the inner
path passes every "contains" row, because a mount point's ancestors are its own
and not the source's — measured in the review round with ``-v
/srv/music/musicdrop:/data/beets``, which booted clean and let Empty Trash
remove the beets dir. So the identity question is asked again where a tree is
moved or removed, against the ``(st_dev, st_ino)`` of every directory in it.

Only DIRECTORIES are stat'd, so an album folder costs 1-3 stats and the Trash
costs one per entry rather than one per file. A leaf module: it imports
``Settings`` and nothing else of this app's, so the movers, the remover and
``store_layout`` can all reach it.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, NamedTuple

from app.config import APP_STORES, EXPORT_STORE, Settings, app_cache_dirs, store_dir

logger = logging.getLogger(__name__)

#: ``(st_dev, st_ino)`` -> (what the directory is, the setting that spells it).
ProtectedIds = Mapping[tuple[int, int], tuple[str, str]]

_Action = Literal["moved", "removed"]


class ProtectedTreeError(Exception):
    """A tree about to be moved or removed holds one of the app's directories.

    Answered 503 at the call sites — the tier that promises nothing happened.
    """


@dataclass(frozen=True)
class ProtectedTrees:
    """What the guard refuses, resolved once beside the layout check.

    ``trash`` is the Trash's own identity at that moment, which
    :func:`open_checked_dir` re-asks the kernel for through an ``O_NOFOLLOW``
    descriptor. ``None`` when the Trash was not there to stat, which that
    function refuses on rather than skipping the compare.
    """

    ids: ProtectedIds
    trash: tuple[int, int] | None


#: What :func:`protected_trees` calls the Trash. Read back by
#: :func:`open_checked_dir`: when the Trash's own inode is named as something
#: ELSE, a bind mount has aliased it onto one of the app's other directories.
_TRASH_NAME: Final = "the Trash directory"


def app_owned_dirs(settings: Settings, beets_dir: Path) -> list[tuple[Path, str, str]]:
    """``(path, what it is, setting)`` for every directory the app owns.

    The five stores under the beets dir, the playlist exports under the music
    library, and the two image caches. All of it off ``app.config``'s one table
    and one formula, which the app's own resolvers call too — so the rule, the
    guard and the app cannot name different directories.
    """
    return [
        *(
            (store_dir(settings, store, beets_dir), store.name, store.setting)
            for store in APP_STORES
        ),
        *app_cache_dirs(settings),
    ]


def export_dir(settings: Settings, music_dir: Path) -> Path:
    """Where the ``.m3u8`` exports live. Same owner as ``reexport.export_dir_for``."""
    return store_dir(settings, EXPORT_STORE, music_dir)


def _ident(path: str | Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)``, or ``None`` for a path this process cannot stat.

    No "is it a directory" test: a file's inode cannot collide with a
    directory's, so a setting that names a file contributes an id nothing can
    match. Measured — adding the test back was an equivalent mutant.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _own_stat(path: str, dir_fd: int | None) -> os.stat_result | None:
    """The path's OWN ``stat`` (links not followed), or ``None`` if it cannot be taken."""
    try:
        return os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return None


def protected_entries(
    *,
    settings: Settings,
    music_dir: Path,
    beets_dir: Path,
    trash_dir: Path,
    origins_dir: Path,
    library_path: Path,
) -> list[tuple[Path, str, str]]:
    """``(path, what it is, setting)`` for every directory this app owns.

    The five the layout rule is about — with ``library_path``'s DIRECTORY, since
    the file itself is not a tree — plus the playlist exports, the five stores
    and the two image caches. One list, read twice: :func:`protected_trees` turns
    it into identities for the movers, and ``api/reorganize._ignore_dirs`` hands
    the paths to the orphan sweep, which is why the sweep spares what the movers
    refuse.
    """
    return [
        (music_dir, "the music library", "`directory:` in config.yaml"),
        (beets_dir, "the beets data directory", "MUSICDROP_BEETS_DIR"),
        (trash_dir, _TRASH_NAME, "MUSICDROP_TRASH_DIR"),
        (origins_dir, "the Trash origin store", "MUSICDROP_TRASH_ORIGINS_DIR"),
        (library_path.parent, "the beets database's folder", "`library:` in config.yaml"),
        (export_dir(settings, music_dir), "the playlist exports", "MUSICDROP_PLAYLISTS_EXPORT_DIR"),
        *app_owned_dirs(settings, beets_dir),
    ]


def protected_trees(
    *,
    settings: Settings,
    music_dir: Path,
    beets_dir: Path,
    trash_dir: Path,
    origins_dir: Path,
    library_path: Path,
) -> ProtectedTrees:
    """Identify the directories a mover or a remover may not act on.

    :func:`protected_entries`, by identity. A path that is not there yet has no
    identity and drops out; it gets one the next time this runs, which is once
    per destructive request.
    """
    entries = protected_entries(
        settings=settings,
        music_dir=music_dir,
        beets_dir=beets_dir,
        trash_dir=trash_dir,
        origins_dir=origins_dir,
        library_path=library_path,
    )
    ids: dict[tuple[int, int], tuple[str, str]] = {}
    for path, name, setting in entries:
        ident = _ident(path)
        # First writer wins, so the five the rule is about name themselves when a
        # store shares their directory (the default `library:` sits in the beets
        # dir, whose parent entry was added first).
        if ident is not None:
            ids.setdefault(ident, (name, setting))
    return ProtectedTrees(ids=ids, trash=_ident(trash_dir))


def _note_walk_error(exc: OSError) -> None:
    """A directory the guard could not open. Logged, not refused.

    Its OWN identity was already compared, from its parent's descriptor; what is
    missed is an alias below it. Refusing instead would turn one permission bit
    into a Trash no route can clear.
    """
    logger.warning("the protected-tree guard could not read %r: %s", str(exc.filename), exc)


class _Match(NamedTuple):
    """Which of the guard's two answers this is, and how it reads."""

    is_root: bool
    clause: str


def _match(root: str | Path, protected: ProtectedTrees, dir_fd: int | None) -> _Match | None:
    """The walk both public forms run. ``None`` when nothing in the tree is ours."""
    top = str(root)
    st = _own_stat(top, dir_fd)
    if st is None or not stat.S_ISDIR(st.st_mode):
        return None
    found = protected.ids.get((st.st_dev, st.st_ino))
    if found is not None:
        return _Match(True, f"is {found[0]} (same inode as {found[1]})")
    for _dirpath, dirnames, _files, fd in os.fwalk(top, onerror=_note_walk_error, dir_fd=dir_fd):
        for name in dirnames:
            child = _own_stat(name, fd)
            if child is None:
                continue
            found = protected.ids.get((child.st_dev, child.st_ino))
            if found is not None:
                return _Match(False, f"contains {found[0]} (same inode as {found[1]})")
    return None


def protected_match(
    root: str | Path, protected: ProtectedTrees, *, dir_fd: int | None = None
) -> str | None:
    """``"is the music library (same inode as ...)"`` when ``root`` holds one of ours.

    ``None`` when it does not. A ``root`` that is a symlink answers ``None``
    without walking: the callers act on the link, not on what it points at.

    Every directory is stat'd from its PARENT's descriptor rather than when the
    walk reaches it, so one this process cannot LIST is still compared — ``stat``
    needs the parent's ``x`` bit only. A mode-000 app store used to be invisible
    here and the album delete moved it into Trash. ``os.fwalk`` also keeps every
    open relative to a descriptor: a tree deeper than PATH_MAX is walked whole,
    where ``os.walk`` joined paths and went blind at 4096 characters while the
    ``rmtree`` behind it, being fd-relative, did not.

    ``dir_fd`` makes ``root`` a name resolved from that descriptor, so a caller
    holding the Trash open asks about an entry of THAT directory rather than
    about a path something may have swapped.
    """
    hit = _match(root, protected, dir_fd)
    return None if hit is None else hit.clause


def protected_tree_error(root: str | Path, clause: str, action: _Action) -> ProtectedTreeError:
    """The refusal, from a clause a caller already has. One sentence, one owner."""
    return ProtectedTreeError(
        f"Refused: {os.path.basename(str(root))!r} {clause}. Nothing was {action}."
    )


def refuse_protected_tree(root: str | Path, protected: ProtectedTrees, *, action: _Action) -> None:
    """Raise if any directory at or under ``root`` is one of the app's own."""
    hit = _match(root, protected, None)
    if hit is not None:
        raise protected_tree_error(root, hit.clause, action)


def refuse_a_held_store(root: str | Path, protected: ProtectedTrees, *, action: _Action) -> bool:
    """Refuse a tree that HOLDS one of ours; answer ``True`` when it IS one.

    Two different questions for a mover. A tree holding an app store must not be
    relocated at all — there is no safe way to move it. A tree that IS one has a
    per-item path: the album's FILES go, the directory stays. Split so the
    delete route and ``delete_artist``'s pre-check refuse the same set.
    """
    hit = _match(root, protected, None)
    if hit is None:
        return False
    if not hit.is_root:
        raise protected_tree_error(root, hit.clause, action)
    return True


def open_checked_dir(path: Path, protected: ProtectedTrees) -> int:
    """A descriptor on the Trash, refusing anything but the directory checked.

    Three comparisons, all before a name is read:

    * the path is not a symlink (``O_NOFOLLOW``) — one planted at the Trash path
      used to be followed by ``iterdir()``;
    * ``protected.trash`` is the identity :func:`protected_trees` stat'd, and
      ``None`` (the Trash was not there to stat) REFUSES rather than skipping the
      compare: a rename of the music dir onto that path in the window was
      measured to empty the library;
    * that identity is the Trash's own. When it names one of the app's other
      directories a bind mount has aliased them, which every spelled row in
      ``store_layout`` allows.

    The caller enumerates AND removes through this descriptor, so a swap after
    the open changes nothing it acts on.
    """
    expected = protected.trash
    if expected is None:
        raise ProtectedTreeError(
            f"Refused: {str(path)!r} could not be examined when MusicDrop checked it."
            " Nothing was removed."
        )
    alias = protected.ids.get(expected)
    if alias is not None and alias[0] != _TRASH_NAME:
        raise ProtectedTreeError(
            f"Refused: the Trash directory is {alias[0]} (same inode as {alias[1]})."
            " Nothing was removed."
        )
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ProtectedTreeError(
            f"Refused: {str(path)!r} is not the directory MusicDrop checked"
            f" ({exc.strerror}). Nothing was removed."
        ) from exc
    try:
        current = _fstat_id(fd)
    except OSError:
        os.close(fd)
        raise
    if current != expected:
        os.close(fd)
        raise ProtectedTreeError(
            f"Refused: {str(path)!r} is not the directory MusicDrop checked"
            " (it changed between the check and the open). Nothing was removed."
        )
    return fd


def _fstat_id(fd: int) -> tuple[int, int]:
    st = os.fstat(fd)
    return (st.st_dev, st.st_ino)
