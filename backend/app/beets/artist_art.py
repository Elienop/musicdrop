"""Write artist-poster/-background into the library's $albumartist/ folders for Plex.

All artist-art disk writes live here (rule 3). Mirrors cover.py: bind
``music_dir_context`` and write via the atomic tmp->rename recipe at 0o644 so
Plex can read it. Plex's Local Media Assets reads ``artist-poster.<ext>`` +
``artist-background.<ext>`` from the artist folder.
"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from beets.dbcore.query import MatchQuery

from app.models.artist_art import ArtistArtOutcome, ArtistArtStatus

_POSTER = "artist-poster"
_BACKGROUND = "artist-background"
_MIME_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}


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
    -> fsync -> chmod 0o644 -> os.replace -> fsync parent dir."""
    tmp = dst.parent / f".{dst.name}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # world-readable so Plex can read it
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


def _write_one(directory: Path, stem: str, asset: tuple[bytes, str] | None, *, force: bool) -> bool:
    """Write one asset (poster or background) into ``directory``; True if written."""
    if asset is None:
        return False
    existing = sorted(directory.glob(f"{stem}.*"))
    if existing and not force:
        return False  # skip-existing
    data, mime = asset
    if force:
        for old in existing:
            with suppress(OSError):
                old.unlink()  # drop any artist-<kind>.* (e.g. a stale .png) first
    _atomic_write_bytes(directory / f"{stem}{_MIME_EXT.get(mime, '.jpg')}", data)
    return True


def has_background(lib: Any, name: str) -> bool:
    """True iff EVERY one of the artist's folders already holds an
    ``artist-background.*`` file (or the artist has no folders).

    Lets the sweep skip the expensive fanart.tv background fetch on a non-force run
    when the write would be a no-op anyway: :func:`_write_one` skip-existing is
    per-folder, so the fetch is only wasted when ALL folders already have the file.
    False when any folder lacks it (the fetch is still needed to fill that one)."""
    return all(any(d.glob(f"{_BACKGROUND}.*")) for d in get_artist_dirs(lib, name))


def write_artist_art(
    lib: Any,
    name: str,
    *,
    poster: tuple[bytes, str] | None,
    background: tuple[bytes, str] | None,
    force: bool,
) -> ArtistArtOutcome:
    """Write poster/background into each of the artist's folders. A single-folder
    IO error is recorded (status='failed' if nothing else wrote), never raised."""
    dirs = get_artist_dirs(lib, name)
    if not dirs:
        return ArtistArtOutcome(artist=name, status="no_folder", written=0, dirs=0)
    if poster is None and background is None:
        return ArtistArtOutcome(artist=name, status="no_art", written=0, dirs=len(dirs))
    written = 0
    failed = False
    for directory in dirs:
        try:
            if _write_one(directory, _POSTER, poster, force=force):
                written += 1
            if _write_one(directory, _BACKGROUND, background, force=force):
                written += 1
        except OSError:
            failed = True
    status: ArtistArtStatus
    if written:
        status = "written"
    elif failed:
        status = "failed"
    else:
        status = "skipped"
    return ArtistArtOutcome(artist=name, status=status, written=written, dirs=len(dirs))
