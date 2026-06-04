import os
from pathlib import Path

from beets.library import Library

from app.beets.artist_art import get_artist_dirs, write_artist_art

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, "image/png")
JPG = (b"\xff\xd8\xff" + b"\x00" * 40, "image/jpeg")


def _artist_of(lib: Library) -> str:
    return str(next(iter(lib.albums())).albumartist)


def test_get_artist_dirs_returns_album_parent(edit_lib: Library) -> None:
    name = _artist_of(edit_lib)
    dirs = get_artist_dirs(edit_lib, name)
    assert dirs
    album = next(iter(edit_lib.albums(f'albumartist:"{name}"')))  # read for assert only
    assert Path(os.fsdecode(album.item_dir())).parent in dirs


def test_write_creates_poster_and_background(edit_lib: Library) -> None:
    name = _artist_of(edit_lib)
    out = write_artist_art(edit_lib, name, poster=PNG, background=JPG, force=True)
    assert out.status == "written"
    for d in get_artist_dirs(edit_lib, name):
        assert (d / "artist-poster.png").exists()
        assert (d / "artist-background.jpg").exists()
        assert (d / "artist-poster.png").stat().st_mode & 0o777 == 0o644


def test_skip_existing_unless_force(edit_lib: Library) -> None:
    name = _artist_of(edit_lib)
    write_artist_art(edit_lib, name, poster=PNG, background=None, force=True)
    out = write_artist_art(edit_lib, name, poster=JPG, background=None, force=False)
    assert out.status == "skipped"
    # the original .png survives (skip-existing), no .jpg added
    for d in get_artist_dirs(edit_lib, name):
        assert (d / "artist-poster.png").exists()
        assert not (d / "artist-poster.jpg").exists()


def test_force_overwrites_and_unlinks_old_ext(edit_lib: Library) -> None:
    name = _artist_of(edit_lib)
    write_artist_art(edit_lib, name, poster=PNG, background=None, force=True)
    write_artist_art(edit_lib, name, poster=JPG, background=None, force=True)
    for d in get_artist_dirs(edit_lib, name):
        assert (d / "artist-poster.jpg").exists()
        assert not (d / "artist-poster.png").exists()  # old ext unlinked


def test_no_art_when_both_none(edit_lib: Library) -> None:
    out = write_artist_art(edit_lib, _artist_of(edit_lib), poster=None, background=None, force=True)
    assert out.status == "no_art"


def test_no_folder_for_unknown_artist(edit_lib: Library) -> None:
    out = write_artist_art(edit_lib, "No Such Artist 99", poster=PNG, background=None, force=True)
    assert out.status == "no_folder"
