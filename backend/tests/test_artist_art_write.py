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
    old_umask = os.umask(0o022)
    try:
        out = write_artist_art(edit_lib, name, poster=PNG, background=JPG, force=True)
    finally:
        os.umask(old_umask)
    assert out.status == "written"
    for d in get_artist_dirs(edit_lib, name):
        assert (d / "artist-poster.png").exists()
        assert (d / "artist-background.jpg").exists()
        assert (d / "artist-poster.png").stat().st_mode & 0o777 == 0o644


def test_atomic_write_bytes_preserves_mode_on_rewrite(tmp_path: Path) -> None:
    from app.beets.artist_art import _atomic_write_bytes

    dst = tmp_path / "artist-poster.png"
    old_umask = os.umask(0o022)
    try:
        _atomic_write_bytes(dst, PNG[0])
    finally:
        os.umask(old_umask)
    assert dst.stat().st_mode & 0o777 == 0o644  # umask 0o022 -> 0o644 on first write
    dst.chmod(0o600)
    _atomic_write_bytes(dst, JPG[0])
    assert dst.stat().st_mode & 0o777 == 0o600  # rewrite preserves the tightened mode
    assert dst.read_bytes() == JPG[0]  # and the content was replaced


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


def test_has_background_true_only_when_every_folder_has_it(edit_lib: Library) -> None:
    from app.beets.artist_art import has_background

    name = _artist_of(edit_lib)
    assert has_background(edit_lib, name) is False  # nothing written yet
    write_artist_art(edit_lib, name, poster=None, background=JPG, force=True)
    assert has_background(edit_lib, name) is True  # every folder now has one
    dirs = get_artist_dirs(edit_lib, name)
    next(iter(dirs[0].glob("artist-background.*"))).unlink()  # drop it from one folder
    assert has_background(edit_lib, name) is False  # fetch is needed again
