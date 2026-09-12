"""Write artist-poster/-background into the library's $albumartist/ folders for Plex.

All artist-art disk writes live here (rule 3). Mirrors cover.py: bind
``music_dir_context`` and write through the shared atomic recipe
(``app.playlists.atomic``) at the umask default so Plex can read it (0o644 at
umask 022). Plex's Local Media Assets reads ``artist-poster.<ext>`` +
``artist-background.<ext>`` from the artist folder.

Every syscall for one artist folder — the listing, the writes, the move-aside —
goes through ONE descriptor opened part by part below the library root
(:func:`_open_folder`), so a folder reached through a symlinked component is
reported failed instead of written outside the library.

Those filenames are also what a person curates by hand, so a ``force`` write
does not unlink what it replaces: the files it would overwrite go to the app's
Trash first (:func:`app.beets.trash.trash_replaced_files`). Nothing is written
into a folder whose old files did not all move aside: that folder is reported
failed, the files that did not move are still there, and any that did are in
the folder's recorded Trash entry.

The move-aside commits before the write, so a folder reported ``failed`` may
have moved its old art and then failed to write the new file: its art is then
only in that Trash entry (pinned). A failed folder is a reason to look in Trash
before emptying it.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from beets.dbcore.query import MatchQuery

from app.beets.library import _music_dir
from app.beets.trash import safe_container_name, trash_replaced_files
from app.beets.trash_origins import TrashOriginsStoreUnusableError
from app.fsutil import open_below
from app.models.artist_art import ArtistArtOutcome, ArtistArtStatus
from app.playlists.atomic import write_atomic_bytes

_log = logging.getLogger(__name__)

_POSTER = "artist-poster"
_BACKGROUND = "artist-background"
_MIME_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}


@dataclass(frozen=True)
class ArtTrashStore:
    """Where a replaced poster/background goes. Both halves of the Trash store,
    taken together because :mod:`app.beets.trash` needs both for every move."""

    trash_dir: Path
    origins_dir: Path


class ArtTrashRefusedError(Exception):
    """A forced write met an existing file it could not move to Trash.

    One type for every way that can happen — no store resolved for this run, a
    store the app cannot use, an entry that is not a file, a failed move — so
    the caller has one arm to take, and nothing is written into that folder when
    it is taken. ``trash_replaced_files`` moves file by file and keeps a record
    for whatever moved, so a move that failed part-way through leaves the
    earlier files in that Trash entry and the rest where they were.
    """


def get_artist_dirs(lib: Any, name: str) -> list[Path]:
    """Distinct $albumartist parent dirs for an artist's non-compilation albums.

    Skips compilations / Various Artists and any album whose parent is the
    library root (a non-$albumartist/$album layout — don't pollute the root).

    The root is ``library._music_dir``'s spelling, which is what
    :func:`write_artist_art` anchors every write against: two spellings of one
    path would make a folder inside the library look outside it.
    """
    lib_root = Path(_music_dir(lib))
    dirs: set[Path] = set()
    with lib.music_dir_context():
        for album in lib.albums(MatchQuery("albumartist", name)):
            if getattr(album, "comp", 0):
                continue
            try:
                album_dir = Path(os.fsdecode(album.item_dir()))
            except ValueError:
                continue  # empty album (no items)
            parent = album_dir.parent
            if parent == lib_root:
                continue  # flat layout — refuse to write into the library root
            dirs.add(parent)
    return sorted(dirs)


def _open_folder(root: Path, directory: Path) -> int:
    """The ONE descriptor every syscall for ``directory`` goes through.

    ``root`` itself is opened FOLLOWING links — an operator's beets
    ``directory:`` may be one — and every part below it ``O_NOFOLLOW``, so a
    symlinked artist folder is refused instead of followed: measured before
    this, a ``music/Artist -> ../outside`` link had the poster written to
    ``outside/artist-poster.jpg`` and a forced run moved files from OUTSIDE the
    library into Trash under the link's name. Owner ruling 2026-09-12: a bind
    mount is the supported spelling for a folder on another disk.

    ``relative_to`` raises ``ValueError`` for a folder outside ``root`` — a
    legacy row, a library whose ``directory:`` moved — which is the same refusal
    as the walk's, so the caller has one arm.

    The fd is the caller's to close. Because it is what the listing, the writes
    and the move-aside all resolve against, a component cannot be re-pointed
    between this check and the write.
    """
    return open_below(root, directory.relative_to(root))


@dataclass(frozen=True)
class _PendingWrite:
    """One planned file: its NAME in the folder, what goes in it, what is there now."""

    name: str
    data: bytes
    existing: tuple[str, ...]


def _folder_plan(
    fd: int, *, poster: tuple[bytes, str] | None, background: tuple[bytes, str] | None
) -> list[_PendingWrite]:
    """The planned writes for one folder, with the files each one would replace.

    Listed once through ``fd``, ahead of any write, so the two kinds' replaced
    files can go to Trash in ONE container — and so nothing is written into a
    folder whose old files did not make it.

    ``fnmatchcase`` over the names, which is the case-sensitive twin of the
    ``glob`` this replaces; the names are what every later syscall passes, so
    the plan cannot name a file in another directory.
    """
    # Closed before the writes: an fd scandir dups the fd and the dup shares its
    # offset, so an open iterator makes a later enumeration read [] (measured).
    with os.scandir(fd) as entries:
        names = sorted(entry.name for entry in entries)
    plan: list[_PendingWrite] = []
    for stem, asset in ((_POSTER, poster), (_BACKGROUND, background)):
        if asset is None:
            continue
        data, mime = asset
        plan.append(
            _PendingWrite(
                name=f"{stem}{_MIME_EXT.get(mime, '.jpg')}",
                data=data,
                existing=tuple(n for n in names if fnmatch.fnmatchcase(n, f"{stem}.*")),
            )
        )
    return plan


def _container_name(directory: Path) -> str:
    """The Trash container's name for one artist folder.

    The shared sanitizer (``trash.safe_container_name``) does the work: a folder
    name holds no path separator, so what it neutralises here is the leading dot
    that would keep the container off the Trash page while Empty-all still
    removed it.
    """
    return safe_container_name(directory.name, " - artist art")


def _move_aside(
    directory: Path, replaced: list[str], *, fd: int, trash: ArtTrashStore | None
) -> None:
    """Move this folder's replaced poster/background files to Trash, or refuse.

    One Trash entry per artist FOLDER per run, holding both kinds: the origin
    record names a folder, and an artist can have several (one per album parent),
    so a single entry for the whole Apply could name only one of them. Both
    files of one folder in one entry keeps the Trash page to one row per folder
    touched.

    ``replaced`` are names in the folder ``fd`` is open on, the same descriptor
    the writes use, so the files moved aside are the ones the plan read.
    """
    if trash is None:
        raise ArtTrashRefusedError("no Trash store was resolved for this run")
    try:
        trash_replaced_files(
            replaced,
            src_dir_fd=fd,
            container_name=_container_name(directory),
            origin=directory,
            trash_dir=trash.trash_dir,
            origins_dir=trash.origins_dir,
        )
    except (OSError, TrashOriginsStoreUnusableError) as exc:
        _log.warning(
            "artist art was not written to %r: the %d file(s) it would replace could not"
            " be moved to Trash",
            str(directory),
            len(replaced),
            exc_info=True,
        )
        raise ArtTrashRefusedError(str(exc)) from exc


def _write_folder(
    directory: Path, plan: list[_PendingWrite], *, fd: int, force: bool, trash: ArtTrashStore | None
) -> tuple[int, bool]:
    """Write one folder's planned files; returns ``(written, failed)``.

    Order is the contract: every file this write replaces goes to Trash FIRST,
    and a refusal there raises before anything is written, so nothing is written
    into a folder whose old art did not move aside.

    A failed write is caught per FILE rather than left to the caller: the
    folder's second file can fail after its first has already landed, and a
    count lost at that point would report an artist whose art was replaced as
    having written nothing.

    ``mode=None`` takes the umask default, which is what the 0o666 create this
    writer used to run did (0o644 at umask 022). Its preserve-on-rewrite arm is
    unreachable from here: a file at the target name matches the plan's own
    ``{stem}.*`` and has just been moved aside, so every write is a create.
    ``dir_fd`` is :func:`_open_folder`'s descriptor, so the shared writer opens
    no path of its own.
    """
    if force:
        replaced = [old for pending in plan for old in pending.existing]
        if replaced:
            _move_aside(directory, replaced, fd=fd, trash=trash)
    written = 0
    failed = False
    for pending in plan:
        if pending.existing and not force:
            continue  # skip-existing
        try:
            write_atomic_bytes(Path(pending.name), pending.data, mode=None, dir_fd=fd)
        except OSError:
            _log.warning(
                "artist art was not written to %r in %r",
                pending.name,
                str(directory),
                exc_info=True,
            )
            failed = True
        else:
            written += 1
    return written, failed


def has_background(lib: Any, name: str) -> bool:
    """True iff EVERY one of the artist's folders already holds an
    ``artist-background.*`` file (or the artist has no folders).

    Lets the sweep skip the expensive fanart.tv background fetch on a non-force run
    when the write would be a no-op anyway: the skip-existing gate in
    :func:`_write_folder` is per-folder, so the fetch is only wasted when ALL
    folders already have the file.
    False when any folder lacks it (the fetch is still needed to fill that one)."""
    return all(any(d.glob(f"{_BACKGROUND}.*")) for d in get_artist_dirs(lib, name))


def write_artist_art(
    lib: Any,
    name: str,
    *,
    poster: tuple[bytes, str] | None,
    background: tuple[bytes, str] | None,
    force: bool,
    trash: ArtTrashStore | None,
) -> ArtistArtOutcome:
    """Write poster/background into each of the artist's folders. An IO error is
    recorded (status='failed', ``written`` still counting what landed), never
    raised.

    ``force`` replaces what is already there; without it a folder that has the
    file is skipped. Replacing means moving the old file to Trash, so ``trash``
    is required rather than defaulted — a caller that has no store to name says
    so, and every folder holding art it would replace is then reported failed
    with its files untouched.

    ``written`` counts the files that landed and ``status`` is ``failed`` as
    soon as one did not, so a run that replaced some art and then failed reports
    both halves instead of one.

    A folder this cannot reach by name below the library root — a symlinked
    component, a file in the way, a row pointing outside the root — is reported
    ``failed`` with one log line and nothing written or moved. Neither the
    ``ValueError`` nor the ``OSError`` that answers it leaves this function.
    """
    root = Path(_music_dir(lib))
    dirs = get_artist_dirs(lib, name)
    if not dirs:
        return ArtistArtOutcome(artist=name, status="no_folder", written=0, dirs=0)
    if poster is None and background is None:
        return ArtistArtOutcome(artist=name, status="no_art", written=0, dirs=len(dirs))
    written = 0
    failed = False
    for directory in dirs:
        try:
            fd = _open_folder(root, directory)
        except (OSError, ValueError):
            # One line, and it names the folder: the two causes are a symlinked
            # or unreachable component and a folder outside the root, and the
            # errno cannot tell a link from a file in the way (measured).
            _log.warning(
                "artist art was not written to %r: it is not reachable by name below the"
                " library root — use a bind mount for a folder on another disk",
                str(directory),
                exc_info=True,
            )
            failed = True
            continue
        try:
            wrote, folder_failed = _write_folder(
                directory,
                _folder_plan(fd, poster=poster, background=background),
                fd=fd,
                force=force,
                trash=trash,
            )
        except (OSError, ArtTrashRefusedError):
            failed = True
        else:
            written += wrote
            failed = failed or folder_failed
        finally:
            os.close(fd)
    status: ArtistArtStatus
    if failed:
        status = "failed"
    elif written:
        status = "written"
    else:
        status = "skipped"
    return ArtistArtOutcome(artist=name, status=status, written=written, dirs=len(dirs))
