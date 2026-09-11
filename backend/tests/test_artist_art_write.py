import os
from pathlib import Path
from typing import Any

import pytest
from beets.library import Library

from app.beets.artist_art import ArtTrashStore, get_artist_dirs, write_artist_art
from app.beets.trash_manage import list_trashed_albums
from app.beets.trash_origins import read_trash_origin

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, "image/png")
JPG = (b"\xff\xd8\xff" + b"\x00" * 40, "image/jpeg")


def _artist_of(lib: Library) -> str:
    return str(next(iter(lib.albums())).albumartist)


@pytest.fixture
def art_trash(tmp_path: Path) -> ArtTrashStore:
    """The Trash store a forced write moves the files it replaces into."""
    return ArtTrashStore(trash_dir=tmp_path / "trash", origins_dir=tmp_path / "trash-origins")


def test_get_artist_dirs_returns_album_parent(edit_lib: Library) -> None:
    name = _artist_of(edit_lib)
    dirs = get_artist_dirs(edit_lib, name)
    assert dirs
    album = next(iter(edit_lib.albums(f'albumartist:"{name}"')))  # read for assert only
    assert Path(os.fsdecode(album.item_dir())).parent in dirs


def test_write_creates_poster_and_background(edit_lib: Library, art_trash: ArtTrashStore) -> None:
    name = _artist_of(edit_lib)
    old_umask = os.umask(0o022)
    try:
        out = write_artist_art(
            edit_lib, name, poster=PNG, background=JPG, force=True, trash=art_trash
        )
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


def test_skip_existing_unless_force(edit_lib: Library, art_trash: ArtTrashStore) -> None:
    name = _artist_of(edit_lib)
    write_artist_art(edit_lib, name, poster=PNG, background=None, force=True, trash=art_trash)
    out = write_artist_art(edit_lib, name, poster=JPG, background=None, force=False, trash=None)
    assert out.status == "skipped"
    # the original .png survives (skip-existing), no .jpg added
    for d in get_artist_dirs(edit_lib, name):
        assert (d / "artist-poster.png").exists()
        assert not (d / "artist-poster.jpg").exists()


def test_force_moves_the_replaced_file_to_trash(
    edit_lib: Library, art_trash: ArtTrashStore
) -> None:
    """A forced write REPLACES, and what it replaces is a file somebody may have
    put there by hand (Plex reads these names), so it goes to Trash with an
    origin record instead of being unlinked."""
    name = _artist_of(edit_lib)
    write_artist_art(edit_lib, name, poster=PNG, background=None, force=True, trash=art_trash)
    out = write_artist_art(edit_lib, name, poster=JPG, background=None, force=True, trash=art_trash)

    assert out.status == "written"
    dirs = get_artist_dirs(edit_lib, name)
    for d in dirs:
        assert (d / "artist-poster.jpg").read_bytes() == JPG[0]
        assert not (d / "artist-poster.png").exists()  # gone from the artist folder

    entries = sorted(p for p in art_trash.trash_dir.iterdir())
    assert [p.name for p in entries] == [f"{d.name} - artist art" for d in dirs]
    for entry, d in zip(entries, dirs, strict=True):
        assert (entry / "artist-poster.png").read_bytes() == PNG[0]  # the old file itself
        record = read_trash_origin(art_trash.origins_dir, entry.name)
        assert record is not None
        assert record.origin == str(d)  # the artist folder it came out of
        assert record.moved == "items"  # no move-back: the entry is not that folder

    listed = list_trashed_albums(
        art_trash.trash_dir,
        origins_dir=art_trash.origins_dir,
        music_dir=os.fsdecode(edit_lib.directory),
    )
    assert [row.folder for row in listed] == [p.name for p in entries]
    assert [row.restore_mode for row in listed] == ["import"] * len(entries)


def test_force_refuses_the_write_when_the_replaced_file_cannot_be_trashed(
    edit_lib: Library, tmp_path: Path
) -> None:
    """No usable Trash store means the curated file stays and the folder fails —
    the one thing a forced write must never do is destroy it anyway."""
    name = _artist_of(edit_lib)
    # A regular FILE where the origin store should be: require_usable_store's
    # mkdir probe raises EEXIST on it for EVERY user (its docstring measured
    # that shape). A path under / would instead lean on EACCES, which root does
    # not get — as root it created the directory, the write went through, and
    # this test went RED for the wrong reason while littering /x-musicdrop.
    blocked = tmp_path / "origins-is-a-file"
    blocked.write_bytes(b"")
    store = ArtTrashStore(trash_dir=tmp_path / "trash", origins_dir=blocked)
    dirs = get_artist_dirs(edit_lib, name)
    for d in dirs:  # the curated file, seeded by hand
        (d / "artist-poster.png").write_bytes(PNG[0])
    out = write_artist_art(edit_lib, name, poster=JPG, background=None, force=True, trash=store)

    assert (out.status, out.written) == ("failed", 0)
    for d in dirs:
        assert (d / "artist-poster.png").read_bytes() == PNG[0]  # untouched
        assert not (d / "artist-poster.jpg").exists()


def test_force_puts_both_kinds_of_one_folder_in_one_trash_entry(
    edit_lib: Library, art_trash: ArtTrashStore
) -> None:
    name = _artist_of(edit_lib)
    write_artist_art(edit_lib, name, poster=PNG, background=PNG, force=True, trash=art_trash)
    write_artist_art(edit_lib, name, poster=JPG, background=JPG, force=True, trash=art_trash)

    entries = sorted(art_trash.trash_dir.iterdir())
    assert len(entries) == len(get_artist_dirs(edit_lib, name))  # one per folder, not per file
    for entry in entries:
        assert sorted(p.name for p in entry.iterdir()) == [
            "artist-background.png",
            "artist-poster.png",
        ]


def test_force_refuses_a_folder_whose_art_name_is_a_directory(
    edit_lib: Library, art_trash: ArtTrashStore
) -> None:
    """A directory matching the glob is not a file to move aside, and this mover
    relocates no tree: the folder fails and the directory stays."""
    name = _artist_of(edit_lib)
    for d in get_artist_dirs(edit_lib, name):
        (d / "artist-poster.png").mkdir()
    out = write_artist_art(edit_lib, name, poster=JPG, background=None, force=True, trash=art_trash)

    assert (out.status, out.written) == ("failed", 0)
    for d in get_artist_dirs(edit_lib, name):
        assert (d / "artist-poster.png").is_dir()
        assert not (d / "artist-poster.jpg").exists()
    assert not art_trash.trash_dir.exists()  # no container left behind


def test_no_art_when_both_none(edit_lib: Library) -> None:
    out = write_artist_art(
        edit_lib, _artist_of(edit_lib), poster=None, background=None, force=True, trash=None
    )
    assert out.status == "no_art"


def test_no_folder_for_unknown_artist(edit_lib: Library) -> None:
    out = write_artist_art(
        edit_lib, "No Such Artist 99", poster=PNG, background=None, force=True, trash=None
    )
    assert out.status == "no_folder"


def test_has_background_true_only_when_every_folder_has_it(
    edit_lib: Library, art_trash: ArtTrashStore
) -> None:
    from app.beets.artist_art import has_background

    name = _artist_of(edit_lib)
    assert has_background(edit_lib, name) is False  # nothing written yet
    write_artist_art(edit_lib, name, poster=None, background=JPG, force=True, trash=art_trash)
    assert has_background(edit_lib, name) is True  # every folder now has one
    dirs = get_artist_dirs(edit_lib, name)
    next(iter(dirs[0].glob("artist-background.*"))).unlink()  # drop it from one folder
    assert has_background(edit_lib, name) is False  # fetch is needed again


def test_a_trashed_art_container_can_be_emptied(
    edit_lib: Library, art_trash: ArtTrashStore
) -> None:
    """The Trash page's per-row Empty removes the container and its record —
    nothing about this entry needs a new delete path."""
    from app.beets.trash_manage import empty_one
    from tests.conftest import protected_for

    name = _artist_of(edit_lib)
    write_artist_art(edit_lib, name, poster=PNG, background=None, force=True, trash=art_trash)
    write_artist_art(edit_lib, name, poster=JPG, background=None, force=True, trash=art_trash)
    entry = next(iter(art_trash.trash_dir.iterdir()))

    result = empty_one(
        str(entry),
        origins_dir=art_trash.origins_dir,
        protected=protected_for(
            edit_lib, trash_dir=art_trash.trash_dir, origins_dir=art_trash.origins_dir
        ),
    )

    assert result.removed == 1
    assert not entry.exists()
    assert read_trash_origin(art_trash.origins_dir, entry.name) is None


def test_force_refuses_the_write_when_no_trash_store_was_given(edit_lib: Library) -> None:
    """No store to name is the same answer as an unusable one: do not write.

    Seeded with the extension the new file WOULD take, because that is the shape
    with no second chance — ``os.replace`` would destroy the curated file in
    place and leave nothing behind to notice.
    """
    name = _artist_of(edit_lib)
    dirs = get_artist_dirs(edit_lib, name)
    for d in dirs:
        (d / "artist-poster.jpg").write_bytes(PNG[0])

    out = write_artist_art(edit_lib, name, poster=JPG, background=None, force=True, trash=None)

    assert (out.status, out.written) == ("failed", 0)
    for d in dirs:
        assert (d / "artist-poster.jpg").read_bytes() == PNG[0]


def test_a_symlink_at_the_derived_tmp_path_is_neither_followed_nor_published(
    tmp_path: Path,
) -> None:
    """The temp file's name is this call's, so a planted one is not in the way.

    The artist folder is inside the music library, which this deployment treats
    as attacker-writable. With a derived ``.<name>.tmp`` a symlink planted there
    was followed by the write and then published AS the destination by
    ``os.replace`` — measured, ``library.db`` overwritten with image bytes while
    the run reported the file written.
    """
    from app.beets.artist_art import _atomic_write_bytes

    secret = tmp_path / "library.db"
    secret.write_bytes(b"the beets database")
    dst = tmp_path / "artist-poster.png"
    planted = tmp_path / f".{dst.name}.tmp"  # exactly the path the writer used to derive
    planted.symlink_to(secret)

    _atomic_write_bytes(dst, PNG[0])

    assert secret.read_bytes() == b"the beets database"  # not written through
    assert not dst.is_symlink()  # the destination is the file itself, not the link
    assert dst.is_file()
    assert dst.read_bytes() == PNG[0]
    assert planted.is_symlink()  # left where it was, not deleted by our cleanup
    # and no temp of ours survived the write
    assert sorted(p.name for p in tmp_path.iterdir()) == [planted.name, dst.name, secret.name]


def test_the_tmp_file_is_created_exclusively_and_without_following_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flags themselves, read off the ``os.open`` call — the twin of
    ``test_lyrics_sidecars``' pin on the same pair.

    Measured: dropping ``O_EXCL | O_NOFOLLOW`` from this writer left all 3446
    tests green. The symlink test next door still passed because the unguessable
    temp name keeps a planted link out of the way on its own, so nothing else
    here reads these flags.
    """
    from app.beets.artist_art import _atomic_write_bytes

    real_open = os.open
    created: list[int] = []

    def spy(path: Any, flags: int, *rest: Any) -> int:
        if flags & os.O_CREAT:
            created.append(flags)
        return real_open(path, flags, *rest)

    # `os` is one shared module object, so patching it here is what the adapter
    # sees. (Reaching through `app.beets.artist_art.os` instead fails mypy
    # strict: the module does not explicitly export the name.)
    monkeypatch.setattr(os, "open", spy)

    _atomic_write_bytes(tmp_path / "artist-poster.png", PNG[0])

    assert created, "the temp file was not created through os.open"
    assert created[0] & os.O_EXCL
    assert created[0] & os.O_NOFOLLOW


def test_a_move_that_dies_mid_copy_leaves_no_container_behind(
    tmp_path: Path, art_trash: ArtTrashStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cross-filesystem ``shutil.move`` is a copy: dying mid-write leaves a
    part-copied file in the container with nothing moved.

    ``rmdir`` refused that dir, so the container stayed in Trash as a row no
    origin record names and no source folder is missing files for. The container
    is this call's own fresh name, so it is removed with its contents.
    """
    import shutil

    from app.beets.trash import trash_replaced_files

    folder = tmp_path / "music" / "Artist"
    folder.mkdir(parents=True)
    curated = folder / "artist-poster.png"
    curated.write_bytes(PNG[0])

    def part_copied_then_dies(src: str, dst: str) -> None:
        Path(dst).write_bytes(PNG[0][:4])  # the copy half of the move got this far
        raise OSError(28, "No space left on device", dst)

    # `shutil` is one shared module object, so patching it here is what the
    # adapter sees. (Reaching through `app.beets.trash.shutil` instead fails
    # mypy strict: the module does not explicitly export the name.)
    monkeypatch.setattr(shutil, "move", part_copied_then_dies)

    with pytest.raises(OSError):
        trash_replaced_files(
            [curated],
            container_name="Artist - artist art",
            origin=folder,
            trash_dir=art_trash.trash_dir,
            origins_dir=art_trash.origins_dir,
        )

    assert list(art_trash.trash_dir.iterdir()) == []  # no phantom container
    assert curated.read_bytes() == PNG[0]  # the source never left
    assert read_trash_origin(art_trash.origins_dir, "Artist - artist art") is None


def test_a_dot_leading_artist_folder_gets_a_listed_trash_container(
    tmp_path: Path, art_trash: ArtTrashStore
) -> None:
    """Every container this mover makes must show up on the Trash page.

    ``trash_manage._audio_free_entries`` skips a dot-leading top-level entry,
    while ``empty_all`` still removes it — so a folder named ``.hack`` (or a
    beets path whose last component is ``..``) would put a row in Trash the user
    never sees and Empty-all deletes.
    """
    from app.beets.artist_art import _move_aside

    music = tmp_path / "music"
    folder = music / ".hack"
    folder.mkdir(parents=True)
    curated = folder / "artist-poster.png"
    curated.write_bytes(PNG[0])

    _move_aside(folder, [curated], trash=art_trash)

    listed = list_trashed_albums(
        art_trash.trash_dir, origins_dir=art_trash.origins_dir, music_dir=str(music)
    )
    assert [row.folder for row in listed] == ["hack - artist art"]
    assert (art_trash.trash_dir / "hack - artist art" / "artist-poster.png").read_bytes() == PNG[0]


def test_a_failed_second_write_keeps_the_count_of_the_first(
    edit_lib: Library, art_trash: ArtTrashStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement that landed is reported even when the next file fails.

    The old poster is in Trash and the new one is on disk, so an outcome of
    ``written=0`` would tell the user nothing happened to a folder that was just
    rewritten. The folder still reports ``failed`` — one file did not make it.
    """
    import app.beets.artist_art as artist_art

    name = _artist_of(edit_lib)
    write_artist_art(edit_lib, name, poster=PNG, background=None, force=True, trash=art_trash)
    real = artist_art._atomic_write_bytes
    calls: list[Path] = []

    def flaky(dst: Path, data: bytes) -> None:
        calls.append(dst)
        if len(calls) > 1:  # the background, after the poster has landed
            raise OSError(13, "Permission denied", str(dst))
        real(dst, data)

    monkeypatch.setattr(artist_art, "_atomic_write_bytes", flaky)
    out = write_artist_art(edit_lib, name, poster=JPG, background=JPG, force=True, trash=art_trash)

    assert (out.status, out.written) == ("failed", 1)
    for d in get_artist_dirs(edit_lib, name):
        assert (d / "artist-poster.jpg").read_bytes() == JPG[0]  # the replacement landed
        assert not (d / "artist-background.jpg").exists()  # the one that failed did not
        # and the file it replaced is in Trash, not back in the folder
        assert (art_trash.trash_dir / f"{d.name} - artist art" / "artist-poster.png").is_file()
