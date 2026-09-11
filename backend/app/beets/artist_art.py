"""Write artist-poster/-background into the library's $albumartist/ folders for Plex.

All artist-art disk writes live here (rule 3). Mirrors cover.py: bind
``music_dir_context`` and write via the atomic tmp->rename recipe at 0o644 so
Plex can read it. Plex's Local Media Assets reads ``artist-poster.<ext>`` +
``artist-background.<ext>`` from the artist folder.

Those filenames are also what a person curates by hand, so a ``force`` write
does not unlink what it replaces: the files it would overwrite go to the app's
Trash first (:func:`app.beets.trash.trash_replaced_files`), and a folder whose
old files could not be moved aside is left alone and reported failed.
"""

from __future__ import annotations

import logging
import os
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
    the caller has one arm to take and the file it would have replaced is
    provably still there when it is taken.
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


def _atomic_write_bytes(dst: Path, data: bytes) -> None:
    """Atomic write (bytes variant of config_editor.atomic_write): tmp in same dir
    -> fsync -> dst mode preserved on rewrite (umask default on first write)
    -> os.replace -> fsync parent dir."""
    tmp = dst.parent / f".{dst.name}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if dst.exists():
            shutil.copymode(dst, tmp)  # mode preserved on rewrite; umask default on first write
        os.replace(tmp, dst)
        dir_fd = os.open(dst.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
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
            container_name=f"{directory.name} - artist art",
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
) -> int:
    """Write one folder's planned files; returns how many were written.

    Order is the contract: every file this write replaces goes to Trash FIRST,
    and a refusal there raises before anything is written, so a folder whose old
    art could not be moved aside still holds it.
    """
    if force:
        replaced = [old for pending in plan for old in pending.existing]
        if replaced:
            _move_aside(directory, replaced, trash=trash)
    written = 0
    for pending in plan:
        if pending.existing and not force:
            continue  # skip-existing
        _atomic_write_bytes(pending.dst, pending.data)
        written += 1
    return written


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
    """Write poster/background into each of the artist's folders. A single-folder
    IO error is recorded (status='failed' if nothing else wrote), never raised.

    ``force`` replaces what is already there; without it a folder that has the
    file is skipped. Replacing means moving the old file to Trash, so ``trash``
    is required rather than defaulted — a caller that has no store to name says
    so, and every folder holding art it would replace is then reported failed
    with its files untouched.
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
            written += _write_folder(
                directory,
                _folder_plan(directory, poster=poster, background=background),
                force=force,
                trash=trash,
            )
        except (OSError, ArtTrashRefusedError):
            failed = True
    status: ArtistArtStatus
    if written:
        status = "written"
    elif failed:
        status = "failed"
    else:
        status = "skipped"
    return ArtistArtOutcome(artist=name, status=status, written=written, dirs=len(dirs))
