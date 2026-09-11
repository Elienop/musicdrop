"""Write artist-poster/-background into the library's $albumartist/ folders for Plex.

All artist-art disk writes live here (rule 3). Mirrors cover.py: bind
``music_dir_context`` and write via the atomic tmp->rename recipe at 0o644 so
Plex can read it. Plex's Local Media Assets reads ``artist-poster.<ext>`` +
``artist-background.<ext>`` from the artist folder.

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

import logging
import os
import secrets
import shutil
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from beets.dbcore.query import MatchQuery

from app.beets.trash import trash_replaced_files
from app.beets.trash_origins import TrashOriginsStoreUnusableError
from app.models.artist_art import ArtistArtOutcome, ArtistArtStatus

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
    """
    lib_root = Path(os.fsdecode(lib.directory))
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


#: Flags for creating the atomic-write temp file, beside the unpredictable name
#: :func:`_tmp_path` picks. ``O_EXCL`` refuses an existing path instead of
#: opening it, ``O_NOFOLLOW`` refuses a symlink. Same pair, same reason, as
#: ``lyrics._TMP_CREATE_FLAGS``.
_TMP_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


def _tmp_path(dst: Path) -> Path:
    """A temp sibling of ``dst`` under a name picked per call, not derived.

    This writer's directory is inside the music library, which this
    deployment's threat model treats as attacker-writable, and a derived
    ``.<name>.tmp`` is a path something else can occupy first: a symlink planted
    there was followed by ``open(tmp, "wb")``, and ``os.replace`` then published
    the link itself as ``dst`` — measured, ``library.db`` overwritten with JPEG
    bytes while the run reported the file written.

    The name does NOT embed ``dst.name``, so its length does not grow with the
    destination's — the same shape as ``lyrics._tmp_path``, which needs that
    because its destination names run to NAME_MAX. Same naming rule as
    ``app.playlists.atomic``, which also keeps two concurrent writers of one
    target off a single inode.
    """
    return dst.parent / f".{os.getpid()}.{secrets.token_hex(8)}{dst.suffix}.tmp"


def _atomic_write_bytes(dst: Path, data: bytes) -> None:
    """Atomic write (bytes variant of config_editor.atomic_write): an
    unpredictable tmp in the same dir (:func:`_tmp_path`) -> fsync -> dst mode
    preserved on rewrite (umask default on first write) -> os.replace -> fsync
    parent dir."""
    tmp = _tmp_path(dst)
    try:
        # 0o666 so the first write still takes the umask default, as the mode
        # test pins; a rewrite has its mode copied from dst below.
        fd = os.open(tmp, _TMP_CREATE_FLAGS, 0o666)
        try:
            stream = os.fdopen(fd, "wb")
        except BaseException:  # pragma: no cover - fdopen fails only on a bad fd
            os.close(fd)
            raise
        with stream as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if dst.exists():
            shutil.copymode(dst, tmp)  # mode preserved on rewrite; umask default on first write
        os.replace(tmp, dst)
        # O_DIRECTORY: a FIFO swapped in here blocks forever without it (measured: 2 s, no error).
        dir_fd = os.open(dst.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        # Whatever is at the temp path: ours, unless something guessed the name
        # this call picked and got there first — in which case the create above
        # already failed and this unlinks the squatter (a dangling symlink
        # excepted: ``exists()`` follows it and reads absent). See ``lyrics``
        # for the one residual (a process killed mid-write leaves the dotfile).
        if tmp.exists():
            with suppress(OSError):
                tmp.unlink()


@dataclass(frozen=True)
class _PendingWrite:
    """One planned file: where it goes, what goes in it, and what is there now."""

    dst: Path
    data: bytes
    existing: tuple[Path, ...]


def _folder_plan(
    directory: Path, *, poster: tuple[bytes, str] | None, background: tuple[bytes, str] | None
) -> list[_PendingWrite]:
    """The planned writes for one folder, with the files each one would replace.

    Globbed once per stem, ahead of any write, so the two kinds' replaced files
    can go to Trash in ONE container — and so nothing is written into a folder
    whose old files did not make it.
    """
    plan: list[_PendingWrite] = []
    for stem, asset in ((_POSTER, poster), (_BACKGROUND, background)):
        if asset is None:
            continue
        data, mime = asset
        plan.append(
            _PendingWrite(
                dst=directory / f"{stem}{_MIME_EXT.get(mime, '.jpg')}",
                data=data,
                existing=tuple(sorted(directory.glob(f"{stem}.*"))),
            )
        )
    return plan


#: What a container is called when the artist folder's name is all dots. Any
#: word does; this one reads in the Trash page's single column.
_UNNAMED_FOLDER = "artist"


def _container_name(directory: Path) -> str:
    """The Trash container's name for one artist folder.

    Leading dots are dropped because ``trash_manage._audio_free_entries`` skips
    a dot-leading top-level entry when it lists the Trash, while
    ``empty_all`` still removes it: an artist folder named ``.hack`` would put a
    container in Trash that the page never shows and Empty-all deletes.
    """
    return f"{directory.name.lstrip('.') or _UNNAMED_FOLDER} - artist art"


def _move_aside(directory: Path, replaced: list[Path], *, trash: ArtTrashStore | None) -> None:
    """Move this folder's replaced poster/background files to Trash, or refuse.

    One Trash entry per artist FOLDER per run, holding both kinds: the origin
    record names a folder, and an artist can have several (one per album parent),
    so a single entry for the whole Apply could name only one of them. Both
    files of one folder in one entry keeps the Trash page to one row per folder
    touched.
    """
    if trash is None:
        raise ArtTrashRefusedError("no Trash store was resolved for this run")
    try:
        trash_replaced_files(
            replaced,
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
    directory: Path, plan: list[_PendingWrite], *, force: bool, trash: ArtTrashStore | None
) -> tuple[int, bool]:
    """Write one folder's planned files; returns ``(written, failed)``.

    Order is the contract: every file this write replaces goes to Trash FIRST,
    and a refusal there raises before anything is written, so nothing is written
    into a folder whose old art did not move aside.

    A failed write is caught per FILE rather than left to the caller: the
    folder's second file can fail after its first has already landed, and a
    count lost at that point would report an artist whose art was replaced as
    having written nothing.
    """
    if force:
        replaced = [old for pending in plan for old in pending.existing]
        if replaced:
            _move_aside(directory, replaced, trash=trash)
    written = 0
    failed = False
    for pending in plan:
        if pending.existing and not force:
            continue  # skip-existing
        try:
            _atomic_write_bytes(pending.dst, pending.data)
        except OSError:
            _log.warning("artist art was not written to %r", str(pending.dst), exc_info=True)
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
    """
    dirs = get_artist_dirs(lib, name)
    if not dirs:
        return ArtistArtOutcome(artist=name, status="no_folder", written=0, dirs=0)
    if poster is None and background is None:
        return ArtistArtOutcome(artist=name, status="no_art", written=0, dirs=len(dirs))
    written = 0
    failed = False
    for directory in dirs:
        try:
            wrote, folder_failed = _write_folder(
                directory,
                _folder_plan(directory, poster=poster, background=background),
                force=force,
                trash=trash,
            )
        except (OSError, ArtTrashRefusedError):
            failed = True
        else:
            written += wrote
            failed = failed or folder_failed
    status: ArtistArtStatus
    if failed:
        status = "failed"
    elif written:
        status = "written"
    else:
        status = "skipped"
    return ArtistArtOutcome(artist=name, status=status, written=written, dirs=len(dirs))
