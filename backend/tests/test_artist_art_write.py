import errno
import logging
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from beets.library import Library

from app.beets.artist_art import ArtTrashStore, get_artist_dirs, write_artist_art
from app.beets.trash_manage import list_trashed_albums
from app.beets.trash_origins import read_trash_origin
from tests.conftest import protected_for

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, "image/png")
JPG = (b"\xff\xd8\xff" + b"\x00" * 40, "image/jpeg")


def _artist_of(lib: Library) -> str:
    return str(next(iter(lib.albums())).albumartist)


@pytest.fixture
def art_trash(tmp_path: Path) -> ArtTrashStore:
    """The Trash store a forced write moves the files it replaces into.

    The real identity record, over a Trash dir CREATED first — the shape
    ``store_layout.checked_protected_trees`` builds, which creates the directory
    one line before it takes the identity. A store built over an absent Trash
    carries ``trash=None``, which every mover now refuses; the refusals live in
    ``tests/test_trash_replaced_root.py``.
    """
    trash = tmp_path / "trash"
    trash.mkdir()
    return ArtTrashStore(
        trash_dir=trash,
        origins_dir=tmp_path / "trash-origins",
        protected=protected_for(trash_dir=trash, origins_dir=tmp_path / "trash-origins"),
    )


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


def test_the_written_mode_is_the_umask_and_not_a_fixed_0o644(
    edit_lib: Library, art_trash: ArtTrashStore
) -> None:
    """This caller passes ``mode=None``, so the operator's umask decides.

    The test above cannot see the difference: ``0o644 & ~0o022 == 0o644``, so a
    fixed ``mode=0o644`` reads identically there. Under 0o077 it does not — a
    fixed mode would publish a world-readable file where the old 0o666 create
    published an owner-only one.
    """
    name = _artist_of(edit_lib)
    old_umask = os.umask(0o077)
    try:
        write_artist_art(edit_lib, name, poster=PNG, background=None, force=True, trash=art_trash)
    finally:
        os.umask(old_umask)

    for d in get_artist_dirs(edit_lib, name):
        assert (d / "artist-poster.png").stat().st_mode & 0o777 == 0o600


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
        # Not "items": these are loose files, not an album's tracks, so neither a
        # move-back nor an import is on offer (trash_origins.MovedShape).
        assert record.moved == "files"

    listed = list_trashed_albums(
        art_trash.trash_dir,
        origins_dir=art_trash.origins_dir,
        music_dir=os.fsdecode(edit_lib.directory),
    )
    assert [row.folder for row in listed] == [p.name for p in entries]
    assert [row.restore_mode for row in listed] == ["by_hand"] * len(entries)


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
    store = ArtTrashStore(
        trash_dir=tmp_path / "trash",
        origins_dir=blocked,
        protected=protected_for(trash_dir=tmp_path / "trash", origins_dir=blocked),
    )
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
    assert list(art_trash.trash_dir.iterdir()) == []  # no container left behind


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


def test_force_refuses_the_write_when_no_trash_store_was_given(
    edit_lib: Library, caplog: pytest.LogCaptureFixture
) -> None:
    """No store to name is the same answer as an unusable one: do not write.

    Seeded with the extension the new file WOULD take, because that is the shape
    with no second chance — ``os.replace`` would destroy the curated file in
    place and leave nothing behind to notice.

    One WARNING per refused folder, naming it and how many files stayed: this
    arm used to raise with nothing logged at all, and the outer arm that logged
    for it also logged a second time for the mover's own refusals.
    """
    name = _artist_of(edit_lib)
    dirs = get_artist_dirs(edit_lib, name)
    for d in dirs:
        (d / "artist-poster.jpg").write_bytes(PNG[0])

    with caplog.at_level(logging.WARNING, logger="app.beets.artist_art"):
        out = write_artist_art(edit_lib, name, poster=JPG, background=None, force=True, trash=None)

    assert (out.status, out.written) == ("failed", 0)
    for d in dirs:
        assert (d / "artist-poster.jpg").read_bytes() == PNG[0]
    lines = [r for r in caplog.records if r.name == "app.beets.artist_art"]
    assert len(lines) == len(dirs), "one line per refused folder"
    assert "could not be moved to Trash" in lines[0].getMessage()
    assert lines[0].exc_info is None, "there is no exception to trace here"


def test_the_fsynced_directory_is_the_folder_the_walk_opened_and_never_a_reopen(
    edit_lib: Library, art_trash: ArtTrashStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recipe's tail fsyncs the artist folder, and it must be THIS fd.

    Reopening the folder by name was the last re-resolution left in the write:
    the bytes land through the anchored descriptor and then the fsync that makes
    the rename durable followed a path again — which is the whole window
    ``_open_folder`` closes. Pinned by identity, not by flags: the flags belong
    to the shared writer's own suite (``tests/test_playlists_atomic.py``), and
    what is this module's is WHICH descriptor.
    """
    real_open, real_fsync = os.open, os.fsync
    by_path: list[str] = []
    fsynced: list[tuple[int, int]] = []

    def spy_open(*args: Any, **kwargs: Any) -> int:
        if kwargs.get("dir_fd") is None:
            by_path.append(os.fspath(args[0]))
        return real_open(*args, **kwargs)

    def spy_fsync(fd: int) -> None:
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode):
            fsynced.append((st.st_dev, st.st_ino))
        real_fsync(fd)

    # `os` is one shared module object, so patching it here is what the adapter
    # sees. (Reaching through `app.beets.artist_art.os` instead fails mypy
    # strict: the module does not explicitly export the name.)
    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "fsync", spy_fsync)

    name = _artist_of(edit_lib)
    out = write_artist_art(edit_lib, name, poster=PNG, background=None, force=True, trash=art_trash)

    assert out.status == "written"
    for d in get_artist_dirs(edit_lib, name):
        st = d.stat()
        assert (st.st_dev, st.st_ino) in fsynced, f"{d} was not fsynced"
        # The only opens by path are the library root and the app's own stores;
        # the folder itself is reached part by part through a dir_fd.
        assert str(d) not in by_path


def test_a_move_that_dies_mid_copy_leaves_no_container_behind(
    tmp_path: Path, art_trash: ArtTrashStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cross-filesystem move is a COPY: dying mid-write leaves a part-copied
    file in the container with nothing moved.

    The Docker default takes that arm for every move (Trash on ``/data``, the
    library on ``/music``), so it is forced here with EXDEV on the descriptor
    rename and an ENOSPC four bytes into the copy. ``rmdir`` refused that dir,
    so the container stayed in Trash as a row no origin record names and no
    source folder is missing files for; the container is this call's own claim,
    so its children go and then it does.
    """
    import shutil

    from app.beets.trash import trash_replaced_files

    folder = tmp_path / "music" / "Artist"
    folder.mkdir(parents=True)
    curated = folder / "artist-poster.png"
    curated.write_bytes(PNG[0])
    real_rename = os.rename

    def across_a_device_boundary(*args: Any, **kwargs: Any) -> None:
        if "dst_dir_fd" in kwargs:  # the mover's own rename, not the app's others
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_rename(*args, **kwargs)

    def part_copied_then_dies(reader: Any, writer: Any, length: int = 0) -> None:
        writer.write(PNG[0][:4])  # the copy got this far
        writer.flush()
        raise OSError(errno.ENOSPC, "No space left on device")

    # `os` / `shutil` are one shared module object each, so patching them here
    # is what the adapter sees. (Reaching through `app.beets.trash.shutil`
    # instead fails mypy strict: the module does not explicitly export the name.)
    monkeypatch.setattr(os, "rename", across_a_device_boundary)
    monkeypatch.setattr(shutil, "copyfileobj", part_copied_then_dies)

    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError):
            trash_replaced_files(
                [curated.name],
                src_dir_fd=fd,
                container_name="Artist - artist art",
                origin=folder,
                trash_dir=art_trash.trash_dir,
                origins_dir=art_trash.origins_dir,
                protected=art_trash.protected,
            )
    finally:
        os.close(fd)

    assert list(art_trash.trash_dir.iterdir()) == []  # no phantom container
    assert curated.read_bytes() == PNG[0]  # the source never left
    assert read_trash_origin(art_trash.origins_dir, "Artist - artist art") is None


def test_a_cross_device_move_copies_through_the_descriptors_and_unlinks_last(
    tmp_path: Path, art_trash: ArtTrashStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The EXDEV arm with the REAL copy: what the user reads back is the file.

    Mode and mtime are carried because a curated poster in Trash is something a
    person copies back by hand, and a Trash dir on another filesystem is the
    Docker default. A symlink is recreated from its own target rather than
    copied through. The source is unlinked LAST, so this end state is either
    "moved" or "still there", never both.
    """
    from app.beets.trash import trash_replaced_files

    folder = tmp_path / "music" / "Artist"
    folder.mkdir(parents=True)
    curated = folder / "artist-poster.png"
    curated.write_bytes(PNG[0])
    curated.chmod(0o640)
    os.utime(curated, ns=(1_500_000_000_000_000_000, 1_400_000_000_000_000_000))
    linked = folder / "artist-background.png"
    linked.symlink_to("../elsewhere/wall.png")
    real_rename = os.rename

    def across_a_device_boundary(*args: Any, **kwargs: Any) -> None:
        if "dst_dir_fd" in kwargs:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_rename(*args, **kwargs)

    monkeypatch.setattr(os, "rename", across_a_device_boundary)

    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    # umask 0o077 trims the create's own mode, so 0o640 below can only come from
    # the ``fchmod`` on the copy's fd — the twin of the shared writer's
    # ``test_the_mode_is_pinned_against_the_umask``.
    old_umask = os.umask(0o077)
    try:
        dest = trash_replaced_files(
            [curated.name, linked.name],
            src_dir_fd=fd,
            container_name="Artist - artist art",
            origin=folder,
            trash_dir=art_trash.trash_dir,
            origins_dir=art_trash.origins_dir,
            protected=art_trash.protected,
        )
    finally:
        os.umask(old_umask)
        os.close(fd)

    moved = dest / "artist-poster.png"
    assert moved.read_bytes() == PNG[0]
    assert moved.stat().st_mode & 0o777 == 0o640
    assert moved.stat().st_mtime_ns == 1_400_000_000_000_000_000
    assert (dest / "artist-background.png").is_symlink()
    assert os.readlink(dest / "artist-background.png") == "../elsewhere/wall.png"
    assert sorted(p.name for p in folder.iterdir()) == []  # both sources gone
    record = read_trash_origin(art_trash.origins_dir, dest.name)
    assert record is not None
    assert record.moved == "files"


def test_a_source_swapped_before_the_cross_device_copy_is_refused(
    tmp_path: Path, art_trash: ArtTrashStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy arm reaches the source by NAME through the folder's descriptor,
    so the file it opens can be a different inode from the one the guard read.

    ``fstat`` against that identity is what refuses it: without the check the
    copy would put a file nobody asked to replace into Trash and unlink it.
    """
    from app.beets.trash import trash_replaced_files

    folder = tmp_path / "music" / "Artist"
    folder.mkdir(parents=True)
    curated = folder / "artist-poster.png"
    curated.write_bytes(PNG[0])
    stranger = folder / "stranger.png"
    stranger.write_bytes(b"somebody else's bytes")
    real_rename = os.rename

    def swap_then_cross_a_boundary(*args: Any, **kwargs: Any) -> None:
        if "dst_dir_fd" not in kwargs:
            real_rename(*args, **kwargs)
            return
        real_rename(stranger, curated)  # another inode, on the guarded name
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "rename", swap_then_cross_a_boundary)

    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError):
            trash_replaced_files(
                [curated.name],
                src_dir_fd=fd,
                container_name="Artist - artist art",
                origin=folder,
                trash_dir=art_trash.trash_dir,
                origins_dir=art_trash.origins_dir,
                protected=art_trash.protected,
            )
    finally:
        os.close(fd)

    assert curated.read_bytes() == b"somebody else's bytes"  # left where it was
    assert list(art_trash.trash_dir.iterdir()) == []  # no container, nothing copied
    assert read_trash_origin(art_trash.origins_dir, "Artist - artist art") is None


def test_a_refused_write_after_the_move_leaves_the_art_only_in_trash(
    edit_lib: Library, art_trash: ArtTrashStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The move-aside commits before the write, so a folder whose only write is
    refused reports ``failed`` with nothing on disk: its art is in Trash alone.
    """
    import app.beets.artist_art as artist_art

    name = _artist_of(edit_lib)
    write_artist_art(edit_lib, name, poster=PNG, background=None, force=True, trash=art_trash)

    def refused(
        path: Path, data: bytes, *, mode: int | None = None, dir_fd: int | None = None
    ) -> None:
        # the move-aside has already committed when the write is attempted, read
        # through the folder's own descriptor (closed: the dup shares the offset)
        assert dir_fd is not None
        with os.scandir(dir_fd) as entries:
            assert [e.name for e in entries if e.name.startswith("artist-poster")] == []
        raise OSError(13, "Permission denied", str(path))

    monkeypatch.setattr(artist_art, "write_atomic_bytes", refused)
    out = write_artist_art(edit_lib, name, poster=JPG, background=None, force=True, trash=art_trash)

    assert (out.status, out.written) == ("failed", 0)
    for d in get_artist_dirs(edit_lib, name):
        assert list(d.glob("artist-poster.*")) == []  # nothing landed
        assert (art_trash.trash_dir / f"{d.name} - artist art" / "artist-poster.png").is_file()


def test_a_container_that_appears_before_the_claim_is_refused_not_emptied(
    tmp_path: Path, art_trash: ArtTrashStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The allocator finds a name free; a directory can still land on it before
    the ``mkdir``. With ``exist_ok`` that directory passed as ours, and a move
    that then failed had ``rmtree`` delete it with whatever it held. The
    ``os.mkdir(dest.name, dir_fd=trash_fd)`` is the claim: an entry already
    there answers EEXIST, before any move.
    """
    from app.beets.trash import trash_replaced_files

    folder = tmp_path / "music" / "Artist"
    folder.mkdir(parents=True)
    curated = folder / "artist-poster.png"
    curated.write_bytes(PNG[0])

    def allocated_then_taken(trash_dir: Path, origins_dir: Path, name: str) -> Path:
        dest = trash_dir / name
        dest.mkdir()  # somebody else's entry, on the name just handed out
        (dest / "precious.flac").write_bytes(b"not ours")
        return dest

    monkeypatch.setattr("app.beets.trash._unique_trash_dest", allocated_then_taken)

    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(FileExistsError):
            trash_replaced_files(
                [curated.name],
                src_dir_fd=fd,
                container_name="Artist - artist art",
                origin=folder,
                trash_dir=art_trash.trash_dir,
                origins_dir=art_trash.origins_dir,
                protected=art_trash.protected,
            )
    finally:
        os.close(fd)

    stranger = art_trash.trash_dir / "Artist - artist art" / "precious.flac"
    assert stranger.read_bytes() == b"not ours"  # the entry that was there stays whole
    assert curated.read_bytes() == PNG[0]  # the source never left


def test_a_stranger_that_takes_the_claimed_name_is_refused_and_not_emptied(
    tmp_path: Path, art_trash: ArtTrashStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim does not hold the name on its own: ``rename`` REPLACES an EMPTY
    directory (measured), so a stranger's entry can take the name between the
    ``mkdir`` and the ``os.open`` under it.

    The emptiness probe on the claimed descriptor is what refuses that, and it
    refuses without touching anything: a non-empty directory is somebody's
    content, and the ``moved == 0`` cleanup is for the container this call made.

    The impostor is created while the claimed directory still holds its inode
    and renamed in afterwards, so the two inodes cannot be the same one reused
    (ext4 hands a freed inode straight back).
    """
    from app.beets.trash import trash_replaced_files

    folder = tmp_path / "music" / "Artist"
    folder.mkdir(parents=True)
    curated = folder / "artist-poster.png"
    curated.write_bytes(PNG[0])
    real_mkdir, real_rename = os.mkdir, os.rename
    idents: list[int] = []

    def claim_then_swapped(path: Any, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        if dir_fd is None:  # the Trash dir itself, and the store probes
            real_mkdir(path, mode)
            return
        real_mkdir(path, mode, dir_fd=dir_fd)
        idents.append(os.stat(path, dir_fd=dir_fd).st_ino)  # ours, still allocated
        impostor = art_trash.trash_dir / "impostor"
        impostor.mkdir()
        (impostor / "precious.flac").write_bytes(b"not ours")
        idents.append(impostor.stat().st_ino)
        real_rename(impostor, art_trash.trash_dir / str(path))

    monkeypatch.setattr(os, "mkdir", claim_then_swapped)

    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError):
            trash_replaced_files(
                [curated.name],
                src_dir_fd=fd,
                container_name="Artist - artist art",
                origin=folder,
                trash_dir=art_trash.trash_dir,
                origins_dir=art_trash.origins_dir,
                protected=art_trash.protected,
            )
    finally:
        os.close(fd)

    assert idents[0] != idents[1], "the impostor reused the claimed inode"
    entry = art_trash.trash_dir / "Artist - artist art"
    assert sorted(p.name for p in entry.iterdir()) == ["precious.flac"]  # untouched
    assert (entry / "precious.flac").read_bytes() == b"not ours"
    assert curated.read_bytes() == PNG[0]  # the source never left
    assert read_trash_origin(art_trash.origins_dir, entry.name) is None


def test_a_directory_swapped_onto_a_guarded_name_is_put_back_and_refused(
    tmp_path: Path, art_trash: ArtTrashStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard lstat'd a FILE, and a rename moves whatever the name means at
    that instant — a directory swapped in was moved whole.

    The lstat through the CONTAINER's descriptor is what decides whether what
    arrived may stay: a directory is renamed back and the call refuses, so this
    mover still relocates no tree.

    The ``unlink`` + ``mkdir`` below gives the impostor a new inode on this box,
    which the identity compare alone catches — but a filesystem that reissues the
    freed number would hand it the SAME one. The type clause is what makes this
    test filesystem-independent, and
    ``tests/test_trash_replaced_names.py::
    test_a_directory_whose_identity_was_staged_is_not_relocated`` is the one that
    pins it directly.
    """
    from app.beets.trash import trash_replaced_files

    folder = tmp_path / "music" / "Artist"
    folder.mkdir(parents=True)
    curated = folder / "artist-poster.png"
    curated.write_bytes(PNG[0])
    real_rename = os.rename
    swapped: list[bool] = []

    def swap_then_rename(*args: Any, **kwargs: Any) -> None:
        if "dst_dir_fd" in kwargs and not swapped:
            swapped.append(True)
            curated.unlink()
            (folder / "artist-poster.png").mkdir()
            (folder / "artist-poster.png" / "inside.flac").write_bytes(b"a tree")
        real_rename(*args, **kwargs)

    monkeypatch.setattr(os, "rename", swap_then_rename)

    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError):
            trash_replaced_files(
                [curated.name],
                src_dir_fd=fd,
                container_name="Artist - artist art",
                origin=folder,
                trash_dir=art_trash.trash_dir,
                origins_dir=art_trash.origins_dir,
                protected=art_trash.protected,
            )
    finally:
        os.close(fd)

    assert swapped, "the swap never ran"
    put_back = folder / "artist-poster.png"
    assert put_back.is_dir()  # back where it was, whole
    assert (put_back / "inside.flac").read_bytes() == b"a tree"
    assert list(art_trash.trash_dir.iterdir()) == []  # the container went with it
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
    from app.beets.artist_art import _move_aside, _open_folder

    music = tmp_path / "music"
    folder = music / ".hack"
    folder.mkdir(parents=True)
    curated = folder / "artist-poster.png"
    curated.write_bytes(PNG[0])

    fd = _open_folder(music, folder)
    try:
        _move_aside(folder, [curated.name], fd=fd, trash=art_trash)
    finally:
        os.close(fd)

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
    from app.playlists.atomic import write_atomic_bytes as real

    calls: list[Path] = []

    def flaky(
        path: Path, data: bytes, *, mode: int | None = None, dir_fd: int | None = None
    ) -> None:
        calls.append(path)
        if len(calls) > 1:  # the background, after the poster has landed
            raise OSError(13, "Permission denied", str(path))
        real(path, data, mode=mode, dir_fd=dir_fd)

    monkeypatch.setattr(artist_art, "write_atomic_bytes", flaky)
    out = write_artist_art(edit_lib, name, poster=JPG, background=JPG, force=True, trash=art_trash)

    assert (out.status, out.written) == ("failed", 1)
    for d in get_artist_dirs(edit_lib, name):
        assert (d / "artist-poster.jpg").read_bytes() == JPG[0]  # the replacement landed
        assert not (d / "artist-background.jpg").exists()  # the one that failed did not
        # and the file it replaced is in Trash, not back in the folder
        assert (art_trash.trash_dir / f"{d.name} - artist art" / "artist-poster.png").is_file()


def test_a_symlinked_artist_folder_is_refused_and_nothing_is_written_outside(
    edit_lib: Library,
    art_trash: ArtTrashStore,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A symlinked artist folder is written THROUGH today, outside the library.

    Measured on the tree before this: ``music/Artist -> ../outside`` had the
    poster land at ``outside/artist-poster.jpg``, and a forced run moved files
    from OUTSIDE the library into Trash under the link's name. Owner ruling
    2026-09-12: a bind mount is the supported spelling for a folder on another
    disk, so a link below the root is refused — the folder reports ``failed``
    with one log line, and the write lands nowhere.
    """
    name = _artist_of(edit_lib)
    (folder,) = get_artist_dirs(edit_lib, name)
    outside = tmp_path / "outside"
    folder.rename(outside)  # the real tree, now out of the library
    folder.symlink_to(outside)
    curated = outside / "artist-poster.png"
    curated.write_bytes(PNG[0])

    with caplog.at_level(logging.WARNING, logger="app.beets.artist_art"):
        out = write_artist_art(
            edit_lib, name, poster=JPG, background=None, force=True, trash=art_trash
        )

    assert (out.status, out.written) == ("failed", 0)
    assert folder.is_symlink()  # the link itself is left alone
    assert curated.read_bytes() == PNG[0]  # nothing outside the library moved
    assert not (outside / "artist-poster.jpg").exists()  # and nothing was written there
    assert list(art_trash.trash_dir.iterdir()) == []
    lines = [r.getMessage() for r in caplog.records if r.name == "app.beets.artist_art"]
    assert len(lines) == 1
    assert str(folder) in lines[0]
    assert "bind mount" in lines[0]


def test_a_folder_outside_the_library_root_fails_without_raising(
    edit_lib: Library, art_trash: ArtTrashStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row can name a folder outside the root — a library whose ``directory:``
    moved, an import that predates it — and the anchored open answers
    ``ValueError`` there, not ``OSError``.

    Same refusal, one arm: ``failed``, nothing written, nothing raised out of
    ``write_artist_art``.
    """
    import app.beets.artist_art as artist_art

    stranger = tmp_path / "elsewhere" / "Artist"
    stranger.mkdir(parents=True)
    monkeypatch.setattr(artist_art, "get_artist_dirs", lambda lib, name: [stranger])

    out = write_artist_art(
        edit_lib, _artist_of(edit_lib), poster=PNG, background=None, force=True, trash=art_trash
    )

    assert (out.status, out.written) == ("failed", 0)
    assert list(stranger.iterdir()) == []  # the write went nowhere
    assert list(art_trash.trash_dir.iterdir()) == []
