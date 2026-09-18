"""The app's own directories, by inode, refused at the point of destruction.

``store_layout`` walks the inner path's SPELLED ancestors, and a mount point's
ancestors are its own rather than the source's, so a bind mount whose mount
point is the inner path passes every "contains" row — measured with ``-v
/srv/music/musicdrop:/data/beets``, which booted clean and let Empty Trash
remove the beets dir. The identity question is asked again where a tree is moved
or removed, against the ``(st_dev, st_ino)`` of every DIRECTORY in it: one stat
per directory in the tree it walks.

A leaf module — ``app.config``, ``app.fsutil`` and nothing else of this app's,
both leaves themselves — so the movers, the remover and ``store_layout`` can all
reach it. Residuals live in one place, the BACKLOG entry for this slice.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from app.config import Settings, app_owned_dirs, export_dir
from app.fsutil import BELOW_FLAGS

logger = logging.getLogger(__name__)

#: ``(st_dev, st_ino)`` -> (what the directory is, the setting that spells it).
ProtectedIds = Mapping[tuple[int, int], tuple[str, str]]

_Action = Literal["moved", "removed"]


class ProtectedTreeError(Exception):
    """A tree about to be moved or removed holds one of the app's directories.

    Answered 503; ``empty_all`` raises it after removing the entries it could,
    and says how many.
    """


@dataclass(frozen=True)
class ProtectedTrees:
    """What the guard refuses, resolved once beside the layout check.

    ``trash`` is the Trash's own identity at that moment, which
    :func:`open_checked_dir` re-asks the kernel for through an ``O_NOFOLLOW``
    descriptor. ``None`` when the Trash was not there to stat, which that
    function refuses on rather than skipping the compare.

    ``trash_alias`` is the first OTHER participant sharing that identity, and is
    carried separately because ``ids`` keeps one owner per inode: the Trash is
    listed third, so for every directory after it ``ids[trash]`` says "the Trash
    directory" and an alias reads as no alias at all.
    """

    ids: ProtectedIds
    trash: tuple[int, int] | None
    trash_alias: tuple[str, str] | None


#: What :func:`protected_trees` calls the Trash. Read back there to find the
#: OTHER participants sharing its inode: a bind mount aliases the Trash onto one
#: of the app's own directories, and every spelled row allows it.
_TRASH_NAME: Final = "the Trash directory"


def _ident(path: str | Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)``, or ``None`` when the ``stat`` raises.

    No "is it a directory" test: only directories are looked up, so a setting
    naming a file contributes an id nothing matches.
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
    the file itself is not a tree — plus the exports, the stores and the caches.
    Read twice: :func:`protected_trees` turns it into identities for the movers,
    and ``api/reorganize._ignore_dirs`` hands the paths to the orphan sweep, so
    the sweep spares what the movers refuse.
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
    trash_ident: tuple[int, int] | None = None,
) -> ProtectedTrees:
    """Identify the directories a mover or a remover may not act on.

    :func:`protected_entries`, by identity. A path that is not there yet has no
    identity and drops out; it gets one the next time this runs, which is once
    per destructive request.

    ``trash_ident`` is the Trash's identity taken from a descriptor the CALLER
    already holds — ``store_layout._ensure_trash_root``'s anchored walk — and it
    replaces the ``stat`` by name this would otherwise take. Measured (security
    seat M-1): 18 µs separated the two, and a racer who swapped an intermediate
    component inside that window was followed by this stat and by the mover's own
    open alike, so the two agreed and the files left the library. ``None`` keeps
    the by-name stat, for the callers that hold no descriptor.
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
    trash: tuple[int, int] | None = None
    seen: list[tuple[tuple[int, int], str, str]] = []
    for path, name, setting in entries:
        ident = trash_ident if name == _TRASH_NAME and trash_ident is not None else _ident(path)
        if ident is None:
            continue
        if name == _TRASH_NAME:
            trash = ident  # the caller's descriptor, or the stat the loop took
        seen.append((ident, name, setting))
        # First writer wins, so the five the rule is about name themselves when a
        # store shares their directory (the default `library:` sits in the beets
        # dir, whose parent entry was added first).
        ids.setdefault(ident, (name, setting))
    # Read off the full list rather than `ids`, so the answer does not depend on
    # where the Trash sits in `protected_entries`.
    alias = next(
        ((n, s) for ident, n, s in seen if ident == trash and n != _TRASH_NAME),
        None,
    )
    return ProtectedTrees(ids=ids, trash=trash, trash_alias=alias)


def _note_walk_error(exc: OSError) -> None:
    """A directory the guard could not open. Logged, not refused.

    Its OWN identity was already compared, from its parent's descriptor; what is
    missed is an alias below it. Refusing instead would turn one permission bit
    into a Trash no route can clear.
    """
    logger.warning("the protected-tree guard could not read %r: %s", str(exc.filename), exc)


def _reads(found: tuple[str, str], verb: str) -> str:
    """One wording for every refusal: what the directory is, and what spells it."""
    return f"{verb} {found[0]} (same inode as {found[1]})"


def protected_id_match(ident: tuple[int, int], protected: ProtectedTrees) -> str | None:
    """``"contains the music library (same inode as ...)"`` when ``ident`` is ours.

    The lookup :func:`protected_match`'s walk makes at every directory, for a
    caller that already holds the identity. The Trash remover asks it at each
    level it descends, through the descriptor it is about to remove through, so
    a tree that arrives after the walk is still compared.
    """
    found = protected.ids.get(ident)
    return None if found is None else _reads(found, "contains")


def _match(
    root: str | Path, protected: ProtectedTrees, dir_fd: int | None
) -> tuple[bool, str] | None:
    """The walk both public forms run. ``None`` when nothing in the tree is ours.

    ``(is the root itself, how the answer reads)``.
    """
    top = str(root)
    st = _own_stat(top, dir_fd)
    if st is None or not stat.S_ISDIR(st.st_mode):
        return None
    found = protected.ids.get((st.st_dev, st.st_ino))
    if found is not None:
        return (True, _reads(found, "is"))
    try:
        for _dirpath, dirs, _files, fd in os.fwalk(top, onerror=_note_walk_error, dir_fd=dir_fd):
            for name in dirs:
                child = _own_stat(name, fd)
                if child is None:
                    continue
                found = protected.ids.get((child.st_dev, child.st_ino))
                if found is not None:
                    return (False, _reads(found, "contains"))
    except OSError as exc:
        # ``os.fwalk`` hands a sub-directory it cannot open to ``onerror`` and
        # RE-RAISES for the ROOT. Same trade either way: the root's own identity
        # was compared above, from the parent's descriptor.
        _note_walk_error(exc)
    return None


def protected_match(
    root: str | Path, protected: ProtectedTrees, *, dir_fd: int | None = None
) -> str | None:
    """``"is the music library (same inode as ...)"`` when ``root`` holds one of ours.

    ``None`` when it does not, and for a ``root`` that is a symlink: the callers
    act on the link, not on what it points at.

    Every directory is stat'd from its PARENT's descriptor, so an unlistable one
    is still compared — ``stat`` needs the parent's ``x`` bit only, and a mode-000
    app store used to be invisible here while the album delete moved it into
    Trash. ``os.fwalk`` keeps every open fd-relative too: a tree deeper than
    PATH_MAX is walked whole, where ``os.walk`` went blind at 4096 characters and
    the fd-relative ``rmtree`` behind it did not.

    ``dir_fd`` makes ``root`` a name resolved from that descriptor, so a caller
    holding the Trash open asks about an entry of THAT directory.
    """
    hit = _match(root, protected, dir_fd)
    return None if hit is None else hit[1]


def protected_tree_error(root: str | Path, clause: str, action: _Action) -> ProtectedTreeError:
    """The refusal, from a clause a caller already has. One sentence, one owner."""
    return ProtectedTreeError(
        f"Refused: {os.path.basename(str(root))!r} {clause}. Nothing was {action}."
    )


def refuse_protected_tree(root: str | Path, protected: ProtectedTrees, *, action: _Action) -> None:
    """Raise if any directory at or under ``root`` is one of the app's own."""
    hit = _match(root, protected, None)
    if hit is not None:
        raise protected_tree_error(root, hit[1], action)


def open_if_one_of_ours(root: str | Path, protected: ProtectedTrees) -> int | None:
    """A descriptor on ``root`` when it IS one of ours; ``None`` otherwise.

    The identity question only — no walk, so it says nothing about what the tree
    CONTAINS — answered by ``fstat`` on the descriptor the caller then acts
    through, as :func:`open_checked_dir` does for the Trash. The caller owns the
    descriptor and must close it.

    ``BELOW_FLAGS``, so a symlink at the name answers ``None``
    (``test_a_symlink_at_a_store_name_is_not_one_of_ours``); the callers act on
    the name in front of them.

    ``ValueError`` beside ``OSError``: a NUL in a stored path makes ``os.open``
    raise that instead (measured), and this answers ``None`` for every path it
    cannot open rather than raising past a caller that has descriptors open.

    The caller is the per-item delete, which writes a keep-file into each store
    beets' prune would otherwise remove; the descriptor is what keeps that write
    inside the directory whose identity was checked.
    """
    try:
        fd = os.open(root, BELOW_FLAGS)
    except (OSError, ValueError):
        return None
    try:
        if _fstat_id(fd) in protected.ids:
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def open_checked_dir(path: Path, protected: ProtectedTrees) -> int:
    """A descriptor on the Trash, refusing anything but the directory checked.

    Three comparisons, all before a name is read: the path is not a symlink
    (``O_NOFOLLOW`` — one planted there used to be followed by ``iterdir()``);
    ``protected.trash``, the identity :func:`protected_trees` stat'd, is present
    (``None`` REFUSES rather than skipping the compare — a rename of the music
    dir onto that path in the window was measured to empty the library); and that
    identity is the Trash's own rather than one of the app's other directories,
    which is what a bind mount aliases and every spelled row allows.

    The caller acts through this descriptor — the remover enumerates, the
    move-aside creates its container under it — so a swap of the Trash ROOT
    after the open reaches neither. Each ENTRY name resolves anew inside it:
    that identity is ``trash_manage._remove_checked_entry``'s to pin, and
    measured, a rename onto an entry's name after this returns was enough to
    delete the tree it named.

    The refusals say "Nothing was removed", which is the remover's wording and
    is pinned as a string by ``tests/test_trash_api.py``; a MOVER surfaces them
    as its own ``OSError`` and keeps this exception only as ``__cause__``.
    """
    expected = protected.trash
    if expected is None:
        raise ProtectedTreeError(
            f"Refused: {str(path)!r} could not be examined when MusicDrop checked it."
            " Nothing was removed."
        )
    alias = protected.trash_alias
    if alias is not None:
        raise ProtectedTreeError(
            f"Refused: the Trash directory is {alias[0]} (same inode as {alias[1]})."
            " Nothing was removed."
        )
    try:
        fd = os.open(path, BELOW_FLAGS)
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
