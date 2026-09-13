from __future__ import annotations

import os
import shutil
import stat
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from beets import config
from beets.library import Item, Library

from app.beets.trash import trash_album
from app.beets.trash_manage import (
    TrashEmptyPartialError,
    TrashEntryUnreadableError,
    _link_name_under,
    empty_all,
    empty_one,
    list_trashed_albums,
    resolve_trash_child,
    restore_album,
)
from app.beets.trash_origins import read_trash_origin, write_trash_origin
from tests.conftest import build_library, origins_for, protected_for

SAMPLE = Path(__file__).parent / "fixtures" / "silent.flac"


@pytest.fixture(autouse=True)
def _serial() -> Iterator[None]:
    # Imports run single-threaded; the starter's copy:yes is the manual default
    # (run_import_worker snapshots/restores move/copy around each run).
    config["threaded"] = False
    config["import"]["copy"] = True
    config["import"]["move"] = False
    yield
    config["threaded"] = False


def _tagged_flac(dst: Path, *, artist: str, album: str, title: str, track: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SAMPLE, dst)
    _tagged_flac_raw(os.fsencode(str(dst)), artist=artist, album=album, title=title, track=track)


def _tagged_flac_raw(dst: bytes, *, artist: str, album: str, title: str, track: int) -> None:
    """Tag a copy of the sample at a RAW path — the name need not be valid UTF-8."""
    if not os.path.exists(dst):
        shutil.copyfile(SAMPLE, os.fsdecode(dst))
    item = Item(album=album, albumartist=artist, artist=artist, title=title, track=track)
    item.path = dst
    item.write()


def _new_library(tmp_path: Path) -> Library:
    return build_library(str(tmp_path / "library.db"), str(tmp_path / "music"))


def _with_bystander(lib: Library, tmp_path: Path) -> Library:
    """``lib`` plus one unrelated album that really exists on disk.

    Every restore writes INTO the music library, so it runs behind
    ``require_library_present`` — and a library with no album anywhere on disk
    is indistinguishable from one whose share has dropped, which is exactly what
    that guard refuses. A library with nothing in it is therefore not a neutral
    fixture for a restore; it is the unmounted-share fixture, and a test that
    used one was asserting restore behaviour through a guard that should have
    stopped it.

    The bystander is what a real library has and this one did not. Added here
    rather than inside ``_new_library`` because the listing tests that share
    that helper count albums, and a silent extra row would change what they mean.
    """
    dst = tmp_path / "music" / "Bystander" / "Album" / "01 t.flac"
    _tagged_flac(dst, artist="Bystander", album="Album", title="T", track=1)
    item = Item(album="Album", albumartist="Bystander", artist="Bystander", title="T", track=1)
    item.path = os.fsencode(str(dst))
    lib.add_album([item]).store()
    return lib


def test_list_groups_whole_folder_album(tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    _tagged_flac(
        trash / "2 Brothers - Dreams" / "01 Dreams.flac",
        artist="2 Brothers",
        album="Dreams",
        title="Dreams",
        track=1,
    )
    _tagged_flac(
        trash / "2 Brothers - Dreams" / "02 Come.flac",
        artist="2 Brothers",
        album="Dreams",
        title="Come",
        track=2,
    )
    albums = list_trashed_albums(
        trash, origins_dir=origins_for(trash), music_dir=str(tmp_path / "music")
    )
    assert len(albums) == 1
    assert albums[0].album_artist == "2 Brothers"
    assert albums[0].album == "Dreams"
    assert albums[0].track_count == 2
    assert albums[0].folder == "2 Brothers - Dreams"
    assert albums[0].format == "FLAC"


def test_list_groups_per_item_layout_and_multidisc(tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    # per-item fallback layout: trash/$albumartist/$album/...
    _tagged_flac(
        trash / "Radiohead" / "Amnesiac" / "01 A.flac",
        artist="Radiohead",
        album="Amnesiac",
        title="A",
        track=1,
    )
    # multi-disc: two CD folders, one album
    _tagged_flac(
        trash / "Adele - 25" / "CD1" / "01 a.flac", artist="Adele", album="25", title="a", track=1
    )
    _tagged_flac(
        trash / "Adele - 25" / "CD2" / "01 b.flac", artist="Adele", album="25", title="b", track=1
    )
    albums = {
        a.album: a
        for a in list_trashed_albums(
            trash, origins_dir=origins_for(trash), music_dir=str(tmp_path / "music")
        )
    }
    assert set(albums) == {"Amnesiac", "25"}
    # Per-item layout keys on the top dir under trash (the $albumartist dir
    # holding the one album) — still reachable for restore/empty.
    assert albums["Amnesiac"].folder == "Radiohead"
    assert albums["25"].folder == "Adele - 25"  # multi-disc shares one top dir
    assert albums["25"].track_count == 2


def test_trash_album_same_artist_siblings_stay_distinct(tmp_path: Path) -> None:
    # Two DIFFERENT albums by the SAME album-artist, each sent to Trash via the
    # per-item primitive (as every duplicate-resolve does). They must remain TWO
    # reachable listing entries — never collapse under one shared $albumartist top
    # dir, which the whole-folder DELETE would then wipe out wholesale (the sibling
    # the user never saw as its own row = silent data loss).
    lib = _new_library(tmp_path)
    music = tmp_path / "music"
    trash = tmp_path / "trash"

    def add_album(album: str, folder: str, titles: list[str]) -> None:
        items: list[Item] = []
        base = music / folder
        base.mkdir(parents=True, exist_ok=True)
        for i, title in enumerate(titles, start=1):
            dst = base / f"{i:02d} {title}.flac"
            shutil.copyfile(SAMPLE, dst)
            it = Item(
                album=album, albumartist="Portishead", artist="Portishead", title=title, track=i
            )
            it.path = os.fsencode(str(dst))
            it.write()  # persist tags so list_trashed_albums (Item.from_path) reads them
            items.append(it)
        lib.add_album(items).store()

    add_album("Dummy", "Portishead/Dummy", ["Mysterons", "Sour Times"])
    add_album("Third", "Portishead/Third", ["Silence", "Hunter", "Nylon Smile"])
    dummy = next(a for a in lib.albums() if a.album == "Dummy")
    third = next(a for a in lib.albums() if a.album == "Third")

    with lib.transaction():
        trash_album(lib, dummy, trash_dir=trash, origins_dir=origins_for(trash))
    with lib.transaction():
        trash_album(lib, third, trash_dir=trash, origins_dir=origins_for(trash))

    albums = list_trashed_albums(trash, origins_dir=origins_for(trash), music_dir=str(music))
    by_album = {a.album: a for a in albums}
    assert set(by_album) == {"Dummy", "Third"}
    assert len(albums) == 2
    # Each container is exactly one album's worth of audio — no cross-contamination.
    assert by_album["Dummy"].track_count == 2
    assert by_album["Third"].track_count == 3
    # Two distinct top-level entries (distinct DELETE keys), never a shared folder.
    folders = {a.folder for a in albums}
    assert len(folders) == 2


def test_list_keeps_same_tagged_siblings_distinct(tmp_path: Path) -> None:
    # Two trashed copies of the same album (the trash subsystem appends " (n)" on
    # collision) carry identical tags; they must stay TWO reachable rows, never
    # collapse onto folder="." (which the restore/empty guard would 404).
    trash = tmp_path / "trash"
    for sub in ("Dreams", "Dreams (1)"):
        _tagged_flac(
            trash / sub / "01 Dreams.flac",
            artist="2 Brothers",
            album="Dreams",
            title="Dreams",
            track=1,
        )
    albums = list_trashed_albums(
        trash, origins_dir=origins_for(trash), music_dir=str(tmp_path / "music")
    )
    assert {a.folder for a in albums} == {"Dreams", "Dreams (1)"}
    assert all(a.folder != "." for a in albums)


def test_list_missing_dir_is_empty(tmp_path: Path) -> None:
    assert (
        list_trashed_albums(
            tmp_path / "nope",
            origins_dir=tmp_path / "trash-origins",
            music_dir=str(tmp_path / "music"),
        )
        == []
    )


def test_restore_imports_as_is_and_empties_folder(tmp_path: Path) -> None:
    lib = _with_bystander(_new_library(tmp_path), tmp_path)
    trash = tmp_path / "trash"
    folder = trash / "2 Brothers - Dreams"
    _tagged_flac(
        folder / "01 Dreams.flac", artist="2 Brothers", album="Dreams", title="Dreams", track=1
    )

    result = restore_album(
        lib,
        str(folder),
        trash_dir=trash,
        origins_dir=origins_for(trash),
        protected=protected_for(lib),
    )

    assert result.restored is True
    assert result.reason == "restored"
    assert result.album_id is not None
    landed = list((tmp_path / "music" / "2 Brothers" / "Dreams").glob("*.flac"))
    assert len(landed) == 1
    assert not list(folder.rglob("*.flac"))  # moved out of Trash


def test_restore_lands_the_album_when_the_user_config_disables_autotag(tmp_path: Path) -> None:
    # beets swaps the lookup_candidates + user_query stages for import_asis when
    # `import: autotag: no` (session.py run()), and user_query is the ONLY stage
    # that calls choose_match — the sole place an outcome is stashed for the
    # album-id follow-up. Without the worker forcing autotag on, beets imports
    # the album for real (files moved, DB row created) while restore_album sees
    # zero outcomes and reports could_not_restore: a successful restore the UI
    # tells the user failed, with the files already gone from Trash.
    config["import"]["autotag"] = False
    lib = _with_bystander(_new_library(tmp_path), tmp_path)
    trash = tmp_path / "trash"
    folder = trash / "2 Brothers - Dreams"
    _tagged_flac(
        folder / "01 Dreams.flac", artist="2 Brothers", album="Dreams", title="Dreams", track=1
    )

    result = restore_album(
        lib,
        str(folder),
        trash_dir=trash,
        origins_dir=origins_for(trash),
        protected=protected_for(lib),
    )

    assert result.restored is True
    assert result.reason == "restored"
    assert result.album_id is not None
    landed = list((tmp_path / "music" / "2 Brothers" / "Dreams").glob("*.flac"))
    assert len(landed) == 1
    assert not list(folder.rglob("*.flac"))  # moved out of Trash


def test_restore_lands_an_album_whose_folder_name_is_not_valid_utf8(tmp_path: Path) -> None:
    # POSIX names are bytes: b"Old Caf\xe9" is not valid UTF-8, so it reaches the
    # app as a lone surrogate. The whole import path — beets' walk, the task
    # paths, the move — has to stay bytes-exact, or an album is strandable in
    # Trash with no way to get it back. Asserts the FILES landed, not just the
    # status field.
    lib = _with_bystander(_new_library(tmp_path), tmp_path)
    trash = tmp_path / "trash"
    trash.mkdir(parents=True)
    raw_folder = os.path.join(os.fsencode(str(trash)), b"Old Caf\xe9")
    os.makedirs(raw_folder)
    _tagged_flac_raw(
        os.path.join(raw_folder, b"01 Dreams.flac"),
        artist="2 Brothers",
        album="Dreams",
        title="Dreams",
        track=1,
    )

    result = restore_album(
        lib,
        os.fsdecode(raw_folder),
        trash_dir=trash,
        origins_dir=origins_for(trash),
        protected=protected_for(lib),
    )

    assert result.restored is True
    assert result.reason == "restored"
    assert result.album_id is not None
    landed = list((tmp_path / "music" / "2 Brothers" / "Dreams").glob("*.flac"))
    assert len(landed) == 1
    assert not os.listdir(raw_folder)  # moved out of Trash, by the real bytes name


def test_restore_duplicate_skips_and_keeps_files(tmp_path: Path) -> None:
    lib = _with_bystander(_new_library(tmp_path), tmp_path)
    # An album with the same identity is already in the library.
    existing = Item(
        album="Dreams", albumartist="2 Brothers", artist="2 Brothers", title="Dreams", track=1
    )
    existing.path = os.fsencode(
        str(tmp_path / "music" / "2 Brothers" / "Dreams" / "01 Dreams.flac")
    )
    lib.add_album([existing])

    trash = tmp_path / "trash"
    folder = trash / "2 Brothers - Dreams"
    _tagged_flac(
        folder / "01 Dreams.flac", artist="2 Brothers", album="Dreams", title="Dreams", track=1
    )

    result = restore_album(
        lib,
        str(folder),
        trash_dir=trash,
        origins_dir=origins_for(trash),
        protected=protected_for(lib),
    )

    assert result.restored is False
    assert result.reason == "already_in_library"
    assert list(folder.rglob("*.flac"))  # still in Trash, untouched


def test_a_container_recorded_before_the_files_shape_still_degrades_to_an_import(
    tmp_path: Path,
) -> None:
    """A record written by the PREVIOUS version keeps today's behaviour.

    ``trash_replaced_files`` recorded ``moved="items"`` for these art containers
    before the ``"files"`` shape existed, and such rows are on disk now. They
    still list as an import, and that import finds no media to take — so it
    answers ``could_not_restore`` with the files and the record both still
    there. A degrade, never a misread.
    """
    lib = _with_bystander(_new_library(tmp_path), tmp_path)
    trash = tmp_path / "trash"
    entry = trash / "Artist - artist art"
    entry.mkdir(parents=True)
    (entry / "artist-poster.png").write_bytes(b"\x89PNG")
    write_trash_origin(
        origins_for(trash),
        entry.name,
        origin=str(tmp_path / "music" / "Artist"),
        moved="items",
    )

    (row,) = list_trashed_albums(
        trash, origins_dir=origins_for(trash), music_dir=str(tmp_path / "music")
    )
    assert row.restore_mode == "import"

    result = restore_album(
        lib,
        str(entry),
        trash_dir=trash,
        origins_dir=origins_for(trash),
        protected=protected_for(lib),
    )

    assert (result.restored, result.reason) == (False, "could_not_restore")
    assert (entry / "artist-poster.png").read_bytes() == b"\x89PNG"
    assert read_trash_origin(origins_for(trash), entry.name) is not None


def test_resolve_trash_child_guards_traversal(tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    (trash / "Album").mkdir(parents=True)
    assert resolve_trash_child(trash, "Album") == (trash / "Album").resolve()
    with pytest.raises(ValueError):
        resolve_trash_child(trash, "../escape")
    with pytest.raises(ValueError):
        resolve_trash_child(trash, "missing")
    with pytest.raises(ValueError):
        resolve_trash_child(trash, ".")  # the Trash root itself


def test_resolve_trash_child_refuses_a_traversal_whose_target_exists(tmp_path: Path) -> None:
    """The traversal above is refused by the EXISTENCE check, not by containment.

    ``../escape`` names nothing on disk, so ``not exists(dest)`` answers it and
    the resolved ``is_relative_to`` check is never the reason — measured on this
    branch before this test existed: with that check deleted the rest of the
    suite stayed green (2998 passed, this one deselected), and
    ``resolve_trash_child(trash, "../..")`` handed back the Trash dir's
    GRANDPARENT — ``<beets dir>``'s own parent in the shipped layout.

    A sibling of the Trash dir that really exists is the input only containment
    can refuse. It is the ordinary shape too: ``<beets dir>`` holds ``trash`` and
    ``trash-origins`` side by side, so ``../trash-origins`` is a real directory
    one ``..`` away from every Trash entry the UI lists.
    """
    trash = tmp_path / "trash"
    (trash / "Album").mkdir(parents=True)
    sibling = tmp_path / "trash-origins"
    sibling.mkdir()

    with pytest.raises(ValueError):
        resolve_trash_child(trash, "../trash-origins")

    assert sibling.is_dir(), "and the refusal happened before anything touched it"


def test_resolve_trash_child_refuses_an_overlong_name(tmp_path: Path) -> None:
    # A >255-byte name component: Path.exists() RAISES OSError(ENAMETOOLONG) —
    # it only swallows ENOENT/ENOTDIR/EBADF/ELOOP. The resolver's contract is
    # "unknown child -> ValueError" (the endpoint maps that to its 404), and a
    # name the kernel cannot even stat cannot be a trashed album — so it must
    # take the same refusal path instead of surfacing as a 500.
    trash = tmp_path / "trash"
    trash.mkdir()
    with pytest.raises(ValueError):
        resolve_trash_child(trash, "x" * 300)


def test_empty_one_and_all(tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    (trash / "A").mkdir(parents=True)
    (trash / "B").mkdir(parents=True)
    assert (
        empty_one(
            str(trash / "A"),
            origins_dir=origins_for(trash),
            protected=protected_for(trash_dir=trash, origins_dir=origins_for(trash)),
        ).removed
        == 1
    )
    assert not (trash / "A").exists()
    assert (
        empty_all(
            trash,
            origins_dir=origins_for(trash),
            protected=protected_for(trash_dir=trash, origins_dir=origins_for(trash)),
        ).removed
        == 1
    )  # B remains
    assert list(trash.iterdir()) == []


def test_empty_all_finishes_what_it_can_and_names_what_it_could_not(tmp_path: Path) -> None:
    """One unremovable entry used to abort the sweep AND lose the count.

    Measured before this: three entries, one of them mode 0500, and the loop
    raised ``PermissionError`` with one entry removed, one still there, and one
    never reached — the user told nothing at all about which. Nothing was ever
    at risk (every entry is either gone or still in Trash); the count was.

    Asserts the two things the message has to carry: how many really went, and
    WHICH are still there — a bare number does not tell the user where to look.
    The removed entries' records go with them and the survivor keeps its own, or
    a later retry would restore into a folder whose origin had been forgotten.
    """
    if os.getuid() == 0:
        pytest.skip("running as root: a read-only dir does not deny writes")
    trash, origins = tmp_path / "trash", tmp_path / "origins"
    trash.mkdir()
    origins.mkdir()
    for name in ("A Album", "B Album", "C Album"):
        (trash / name).mkdir()
        (trash / name / "t.flac").write_bytes(b"\x00")
        write_trash_origin(origins, name, origin=f"/music/{name}", moved="folder")
    (trash / "B Album").chmod(0o500)

    try:
        protected = protected_for(trash_dir=trash, origins_dir=origins)
        with pytest.raises(TrashEmptyPartialError) as ei:
            empty_all(
                trash,
                origins_dir=origins,
                protected=protected,
            )
    finally:
        (trash / "B Album").chmod(0o700)  # or the tmp_path teardown cannot clean up

    assert "removed 2 of 3" in str(ei.value)
    assert "'B Album'" in str(ei.value)
    # The tail quotes an OSError's own words, so the sentence is finished here
    # (``_one_full_stop``) rather than left open or given a second stop. Written
    # against the stripped tail so BOTH failures are caught in one assert: no
    # stop at all, and the ".." an unconditional append renders.
    said = str(ei.value)
    assert said == f"{said.rstrip('.')}.", "the message must end in exactly one full stop"
    assert [p.name for p in trash.iterdir()] == ["B Album"]
    assert read_trash_origin(origins, "B Album") is not None  # the survivor keeps its origin
    assert read_trash_origin(origins, "A Album") is None
    assert read_trash_origin(origins, "C Album") is None


def test_empty_all_clears_the_store_once_trash_is_empty(tmp_path: Path) -> None:
    """The one class of leftover record no per-row action can reach.

    ``delete_trash_origin`` is owed by every route that takes an entry OUT of
    Trash, so it covers every entry MusicDrop itself removes. An entry that
    leaves by ANOTHER route — a file manager, an SMB client, ``docker volume
    rm`` — never reaches it, and its record then survived every Empty and every
    Restore for good, holding its name against a future album
    (``trash._unique_trash_dest`` reads a recorded name as occupied) and
    standing ready to be adopted by a folder that lands on that name.

    An Empty all that finishes is the one moment the answer is known for the
    whole store: nothing is in Trash, so no record describes anything. The live
    entry beside it is what keeps the assertion from being about the orphan
    alone — the sweep has to be the LAST thing, after the per-child drops, or
    the row that was really there loses its record while its folder is still in
    Trash.
    """
    trash, origins = tmp_path / "trash", tmp_path / "origins"
    trash.mkdir()
    origins.mkdir()
    (trash / "Live Album").mkdir()
    write_trash_origin(origins, "Live Album", origin="/music/Live Album", moved="folder")
    # Its entry left Trash without this app noticing, so nothing ever dropped it.
    write_trash_origin(origins, "Gone Album", origin="/music/Gone Album", moved="folder")

    assert (
        empty_all(
            trash,
            origins_dir=origins,
            protected=protected_for(trash_dir=trash, origins_dir=origins),
        ).removed
        == 1
    )

    assert list(trash.iterdir()) == []
    assert list(origins.iterdir()) == [], "an emptied Trash must leave an empty store"


def test_empty_all_keeps_every_record_when_it_removed_nothing(tmp_path: Path) -> None:
    """Emptiness alone must NOT authorise the sweep, and this is the case that says why.

    A ``trash_dir`` on a share that has dropped presents as an empty directory,
    so a sweep gated on "Trash is empty afterwards" alone would run here and
    destroy the origins of every entry still sitting on the real volume — the
    exact loss the per-child drop was written to avoid. Having REMOVED an entry
    is the evidence that the directory walked was the real one.

    Modelled at the only thing ``empty_all`` can see, an empty ``trash_dir``: it
    takes no library and asks nothing about mounts, so there is no
    ``require_library_present`` in this path to build a bystander album for. A
    user who has already emptied Trash produces the identical call, and keeping
    the records is the right answer for them too — the next Empty all that
    removes something clears them.
    """
    trash, origins = tmp_path / "trash", tmp_path / "origins"
    trash.mkdir()
    origins.mkdir()
    write_trash_origin(origins, "Real Album", origin="/music/Real Album", moved="folder")

    assert (
        empty_all(
            trash,
            origins_dir=origins,
            protected=protected_for(trash_dir=trash, origins_dir=origins),
        ).removed
        == 0
    )

    record = read_trash_origin(origins, "Real Album")
    assert record is not None, "a Trash dir that reads as empty is not proof that it is"
    assert record.origin == "/music/Real Album"


def test_empty_all_clears_a_symlinked_entry_without_following_it(tmp_path: Path) -> None:
    """One symlinked entry used to make Trash impossible to empty, permanently.

    ``is_dir()`` follows symlinks and ``shutil.rmtree`` refuses one, so the loop
    raised ``OSError`` and every retry raised it again -- and ``empty_one``
    cannot clear it either, because ``resolve_trash_child`` refuses a child that
    is a link before resolving it. Nothing hostile is needed to get
    one there: an album whose own folder is a symlink into another volume is
    trashed as a symlink, since ``shutil.move`` preserves them.

    Both halves are asserted, and the second is the one that matters more: the
    link is REMOVED, and the directory it pointed at still has its contents.
    Following it would ``rm -rf`` a directory that merely happens to be pointed
    at, which is worse than the wedge this fixes.
    """
    trash, elsewhere = tmp_path / "trash", tmp_path / "elsewhere"
    trash.mkdir()
    elsewhere.mkdir()
    (elsewhere / "keepme.txt").write_text("not Trash's to delete")
    (trash / "Real Album").mkdir()
    (trash / "Real Album" / "a.flac").write_bytes(b"\x00")
    (trash / "Symlinked Album").symlink_to(elsewhere, target_is_directory=True)

    assert (
        empty_all(
            trash,
            origins_dir=origins_for(trash),
            protected=protected_for(trash_dir=trash, origins_dir=origins_for(trash)),
        ).removed
        == 2
    )

    assert list(trash.iterdir()) == []
    assert (elsewhere / "keepme.txt").is_file()


def test_a_fifo_named_like_a_track_does_not_hang_the_listing(tmp_path: Path) -> None:
    """A non-regular entry in Trash is never opened, and the listing returns.

    Measured 2026-09-13 (security seat M-2): ``Item.from_path`` opens every name
    ``os.walk`` returns, and a FIFO blocks that open until a writer appears — one
    ``mkfifo`` wedged the request for the life of the process, holding a
    ``run_in_threadpool`` worker each time the page was reloaded (anyio's default
    limiter is 40, shared by every threadpool route). The Trash root is
    attacker-writable under the layout rule's own model.

    Run on a thread with a join deadline, because a plain call would hang this
    suite instead of failing it.
    """
    trash = tmp_path / "trash"
    _tagged_flac(trash / "Real Album" / "01 t.flac", artist="A", album="Real", title="T", track=1)
    (trash / "Wedge").mkdir(parents=True)
    os.mkfifo(trash / "Wedge" / "01.flac")
    # A LINK to the same shape, as one more entry rather than a second test: the
    # gate asks what the name resolves to, and a mutant widening it to
    # ``S_ISREG or S_ISLNK`` would hang on this row with the one above green
    # (code seat S5).
    (trash / "Linked Wedge").mkdir()
    (trash / "Linked Wedge" / "01.flac").symlink_to(trash / "Wedge" / "01.flac")
    listed: list[list[Any]] = []

    def run() -> None:
        listed.append(
            list_trashed_albums(
                trash, origins_dir=origins_for(trash), music_dir=str(tmp_path / "music")
            )
        )

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(5)

    assert not worker.is_alive(), "the listing is still blocked on the FIFO"
    assert {a.folder: a.track_count for a in listed[0]} == {
        "Real Album": 1,
        "Wedge": 0,
        "Linked Wedge": 0,
    }


@pytest.mark.parametrize("plant", ["fifo", "link-to-fifo"])
def test_a_non_regular_file_inside_an_entry_refuses_the_restore(tmp_path: Path, plant: str) -> None:
    """Restore is refused in bounded time, and the folder stays in Trash.

    Measured 2026-09-13 (security seat M-2''), at this branch's base and at
    round 1's tip alike: ``POST /api/trash/restore`` on an entry holding one
    ``mkfifo`` never returned (>6 s, blocked in ``mutagen.wave.WAVE`` inside
    beets' own ``read_item``), parking one ``run_in_threadpool`` worker per
    click. The entry lists as a 0-track row and the UI keeps Restore enabled
    there on purpose, so the operator clicks it again.

    Both plants, because the gate asks what the name RESOLVES to: the link is
    the shape a refusal on ``os.path.islink`` alone would miss. Run on a thread
    with a join deadline — a plain call would hang this suite instead of
    failing it.
    """
    lib = _with_bystander(_new_library(tmp_path), tmp_path)
    trash = tmp_path / "trash"
    entry = trash / "2 Brothers - Dreams"
    _tagged_flac(
        entry / "01 Dreams.flac", artist="2 Brothers", album="Dreams", title="Dreams", track=1
    )
    if plant == "fifo":
        os.mkfifo(entry / "02 wedge.flac")
    else:
        os.mkfifo(tmp_path / "pipe")
        (entry / "02 wedge.flac").symlink_to(tmp_path / "pipe")
    raised: list[BaseException] = []

    def run() -> None:
        try:
            restore_album(
                lib,
                str(entry),
                trash_dir=trash,
                origins_dir=origins_for(trash),
                protected=protected_for(lib),
            )
        except BaseException as exc:  # the isinstance below is the oracle
            raised.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(10)

    assert not worker.is_alive(), "the restore is still blocked on the non-regular file"
    assert isinstance(raised[0], TrashEntryUnreadableError)
    assert "'02 wedge.flac'" in str(raised[0]), str(raised[0])
    assert (entry / "01 Dreams.flac").is_file(), "nothing left Trash"
    assert not list((tmp_path / "music" / "2 Brothers").glob("*")), "nothing reached the library"


def test_a_listed_loose_file_swapped_for_a_fifo_refuses_the_restore(tmp_path: Path) -> None:
    """The entry ITSELF is asked about, not only the names under it.

    ``os.walk`` on a name that is not a directory yields nothing, so the
    pre-flight's walk answered ``None`` for a Trash entry that IS a pipe — and
    beets opens a non-directory toppath DIRECTLY rather than walking it (``if
    not os.path.isdir(syspath(self.toppath)): yield [self.toppath],
    [self.toppath]``, ``beets/importer/tasks.py:1041``). Measured 2026-09-13
    (security seat H-1 probes p1/p3, code seat CRITICAL): ``POST
    /api/trash/restore`` never returned, and one wedge held ``beets_swap_lock``
    for the life of the process — every mutating Trash route and reorganize
    answered 409 afterwards.

    The listing runs FIRST because it is the reachability claim: a loose regular
    audio file at the top of Trash lists as its own row with Restore enabled, so
    the whole listing-to-click interval is the window and no race has to be won.
    ``resolve_trash_child`` does not close it either — it refuses a link and an
    absent child, and a FIFO is neither.

    The sentence asserted is the entry-itself one: an entry that IS the pipe does
    not "hold" it, and it is not a folder either.

    Run on a thread with a join deadline; a plain call would hang this suite
    instead of failing it.
    """
    lib = _with_bystander(_new_library(tmp_path), tmp_path)
    trash = tmp_path / "trash"
    loose = trash / "loose.flac"
    _tagged_flac(loose, artist="2 Brothers", album="Dreams", title="Dreams", track=1)

    listed = list_trashed_albums(
        trash, origins_dir=origins_for(trash), music_dir=str(tmp_path / "music")
    )
    assert [a.folder for a in listed] == ["loose.flac"], "the row the operator clicks"

    loose.unlink()
    os.mkfifo(loose)
    raised: list[BaseException] = []

    def run() -> None:
        try:
            restore_album(
                lib,
                str(loose),
                trash_dir=trash,
                origins_dir=origins_for(trash),
                protected=protected_for(lib),
            )
        except BaseException as exc:  # the isinstance below is the oracle
            raised.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(10)

    assert not worker.is_alive(), "the restore is still blocked on the entry itself"
    assert isinstance(raised[0], TrashEntryUnreadableError)
    detail = str(raised[0])
    assert detail.startswith("This Trash entry is not a folder or a regular file"), detail
    assert "holds" not in detail, "the entry IS the thing; it does not hold it"
    # And the remedy names something clickable. "Remove it from Trash" did not:
    # ``_audio_free_entries`` lists an entry only if it is a link or a directory,
    # so this shape has NO row once the page is refreshed -- Empty all is the one
    # route that reaches it, and it is never disabled (security seat I-2).
    assert "Empty all" in detail, detail
    assert stat.S_ISFIFO(os.lstat(loose).st_mode), "nothing left Trash"
    assert not list((tmp_path / "music" / "2 Brothers").glob("*")), "nothing reached the library"


def test_a_loose_regular_file_entry_still_restores(tmp_path: Path) -> None:
    """The control for the ENTRY arm: what it may NOT refuse.

    The gate above asks whether the entry is a non-regular, non-directory name.
    A loose audio file at the top of Trash is the ordinary shape that arm exists
    for -- it lists as its own row with Restore enabled, which is the very
    reachability claim the test above opens with -- so the cheaper-looking
    spelling ``not entry.is_dir()`` refuses a perfectly good FLAC with "this is
    not a folder or a regular file". Measured 2026-09-14 (code seat W1,
    re-measured here): that mutant passes all 3,648 tests without this one.

    The contents arm has had its twin since it shipped
    (``test_a_dangling_link_inside_an_entry_still_restores``, just below); the
    entry arm went out with only the kill half.
    """
    lib = _with_bystander(_new_library(tmp_path), tmp_path)
    trash = tmp_path / "trash"
    loose = trash / "loose.flac"
    _tagged_flac(loose, artist="2 Brothers", album="Dreams", title="Dreams", track=1)

    result = restore_album(
        lib,
        str(loose),
        trash_dir=trash,
        origins_dir=origins_for(trash),
        protected=protected_for(lib),
    )

    assert result.restored, result
    assert not loose.exists(), "the entry never left Trash"
    assert list((tmp_path / "music" / "2 Brothers").rglob("*.flac")), "it reached the library"


def test_a_dangling_link_inside_an_entry_still_restores(tmp_path: Path) -> None:
    """The control for the gate above: what it may not refuse.

    A dangling link is what an unmounted volume looks like and the listing skips
    it without an error (the test two below this one). Opening it answers ENOENT
    at once, so it is not the gate's business — and refusing it would leave an
    album permanently unrestorable for a name that cannot block anything.
    """
    lib = _with_bystander(_new_library(tmp_path), tmp_path)
    trash = tmp_path / "trash"
    entry = trash / "2 Brothers - Dreams"
    _tagged_flac(
        entry / "01 Dreams.flac", artist="2 Brothers", album="Dreams", title="Dreams", track=1
    )
    (entry / "02 gone.flac").symlink_to(tmp_path / "nowhere" / "t.flac")

    result = restore_album(
        lib,
        str(entry),
        trash_dir=trash,
        origins_dir=origins_for(trash),
        protected=protected_for(lib),
    )

    assert (result.restored, result.reason) == (True, "restored")
    assert len(list((tmp_path / "music" / "2 Brothers" / "Dreams").glob("*.flac"))) == 1


def test_a_fifo_inside_a_symlinked_subfolder_of_an_entry_refuses_the_restore(
    tmp_path: Path,
) -> None:
    """The pre-flight follows links into subfolders because beets does.

    ``sorted_walk`` sorts a name into ``dirs`` by ``os.path.isdir``, which
    resolves links (``beets/util/__init__.py:247``), so the importer descends a
    symlinked subfolder of the entry. A pre-flight that stopped at it would hand
    beets the pipe inside. The walk is bounded by identity, not by depth: the
    link back up its own tree below is walked once.

    The refusal names the LINK, not the pipe: this link leaves the entry, and
    ``'Disc 2/01 wedge.flac'`` was a filename read back out of a directory the
    operator cannot see (security seat L-2, measured 2026-09-13 — the oracle
    test below plants the same shape against a private name). Naming ``Disc 2``
    still proves the descent, because without it there is no refusal at all:
    the sibling control below pins that a symlinked subfolder holding only
    regular files restores, so "refuse every link" cannot pass both.
    """
    lib = _with_bystander(_new_library(tmp_path), tmp_path)
    trash = tmp_path / "trash"
    entry = trash / "2 Brothers - Dreams"
    _tagged_flac(
        entry / "01 Dreams.flac", artist="2 Brothers", album="Dreams", title="Dreams", track=1
    )
    disc2 = tmp_path / "disc2"
    disc2.mkdir()
    os.mkfifo(disc2 / "01 wedge.flac")
    (entry / "Disc 2").symlink_to(disc2, target_is_directory=True)
    (disc2 / "up").symlink_to(entry, target_is_directory=True)
    raised: list[BaseException] = []

    def run() -> None:
        try:
            restore_album(
                lib,
                str(entry),
                trash_dir=trash,
                origins_dir=origins_for(trash),
                protected=protected_for(lib),
            )
        except BaseException as exc:  # the isinstance below is the oracle
            raised.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(10)

    assert not worker.is_alive(), "the walk did not finish"
    assert isinstance(raised[0], TrashEntryUnreadableError)
    assert "'Disc 2'" in str(raised[0]), str(raised[0])
    assert "wedge" not in str(raised[0]), "a name from outside the entry reached the 503"
    assert (entry / "01 Dreams.flac").is_file(), "nothing left Trash"


def test_every_symlinked_directory_is_named_by_its_link_and_a_real_one_is_not(
    tmp_path: Path,
) -> None:
    """The substitution's predicate, asked directly, because the walk cannot ask it.

    A link whose target is INSIDE the entry is an alias for a directory the walk
    reaches anyway, and the walk prunes by identity — so exactly one of the two
    spellings is descended, and which one depends on ``os.scandir`` order, i.e.
    the directory's hash order rather than a promise. Whatever this function
    answers for that shape is therefore unreachable through
    ``_unopenable_name_under``, so it is pinned here.

    The middle answer CHANGED on 2026-09-14. It used to be ``None`` for a link
    that stays inside, decided by ``realpath`` on both sides — and that question
    was resolved one statement before ``os.walk`` descended the same name, so a
    re-point in the window leaked an outside path into the 503 in 8.01 % of
    restores (security seat L-1, measured over 39,486 calls). The arm is gone:
    ``islink`` is the whole predicate, every symlinked directory is named by its
    link, and the cost is that "remove that name" can take two restores for a
    link that was inside all along.
    """
    entry = tmp_path / "Album"
    (entry / "inside").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (entry / "stays").symlink_to(entry / "inside", target_is_directory=True)
    (entry / "leaves").symlink_to(tmp_path / "outside", target_is_directory=True)

    assert _link_name_under(str(entry / "leaves"), entry) == "leaves"
    assert _link_name_under(str(entry / "stays"), entry) == "stays", (
        "where a link goes is not asked, because the answer can change after it is"
    )
    assert _link_name_under(str(entry / "inside"), entry) is None, "a real directory is not a link"


def test_a_symlinked_subfolder_of_regular_files_still_restores(tmp_path: Path) -> None:
    """The control for the gate above: what following a link may NOT cost.

    The refusal now names the link component rather than the name below it, so
    "refuse any symlinked subfolder" would pass that test while breaking every
    entry whose album folder holds a linked disc directory -- a real shape,
    since ``shutil.move`` preserves links on the way into Trash. The pipe is
    what refuses, not the link.
    """
    lib = _with_bystander(_new_library(tmp_path), tmp_path)
    trash = tmp_path / "trash"
    entry = trash / "2 Brothers - Dreams"
    _tagged_flac(
        entry / "01 Dreams.flac", artist="2 Brothers", album="Dreams", title="Dreams", track=1
    )
    disc2 = tmp_path / "disc2"
    _tagged_flac(disc2 / "02 Dreams.flac", artist="2 Brothers", album="Dreams", title="B", track=2)
    (entry / "Disc 2").symlink_to(disc2, target_is_directory=True)

    result = restore_album(
        lib,
        str(entry),
        trash_dir=trash,
        origins_dir=origins_for(trash),
        protected=protected_for(lib),
    )

    assert result.restored, result
    landed = list((tmp_path / "music" / "2 Brothers").glob("**/*.flac"))
    assert landed, "nothing reached the library"


def test_a_link_out_of_an_entry_cannot_read_back_a_name_behind_it(tmp_path: Path) -> None:
    """The 503 may name only a path the operator can see INSIDE the entry.

    ``_unopenable_name_under`` walks ``followlinks=True`` and its answer goes
    verbatim into the 503 that ``SettingsTrashPage`` renders as the row's whole
    text. Measured 2026-09-13 (security seat L-2): with
    ``<trash>/Album/peek -> /any/dir``, one Restore click came back
    ``"This Trash entry holds 'peek/private-name'"`` -- the name of the first
    non-regular file in a directory outside Trash, iterable by re-pointing the
    link. Someone who can only write into ``/music`` is this branch's own threat
    model, and ``shutil.move`` carries their link into Trash.

    ``private-name`` is a FIFO so the walk refuses on it: that is what makes the
    leak reachable at all, and it keeps the shape identical to the oracle.

    Run on a thread with a join deadline; the refusal is what stops the pipe
    reaching beets, so a regression here hangs rather than fails.
    """
    lib = _with_bystander(_new_library(tmp_path), tmp_path)
    trash = tmp_path / "trash"
    entry = trash / "2 Brothers - Dreams"
    _tagged_flac(
        entry / "01 Dreams.flac", artist="2 Brothers", album="Dreams", title="Dreams", track=1
    )
    outside = tmp_path / "not-trash"
    outside.mkdir()
    os.mkfifo(outside / "private-name")
    (entry / "peek").symlink_to(outside, target_is_directory=True)
    raised: list[BaseException] = []

    def run() -> None:
        try:
            restore_album(
                lib,
                str(entry),
                trash_dir=trash,
                origins_dir=origins_for(trash),
                protected=protected_for(lib),
            )
        except BaseException as exc:  # the isinstance below is the oracle
            raised.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(10)

    assert not worker.is_alive(), "the restore is still blocked on the pipe behind the link"
    assert isinstance(raised[0], TrashEntryUnreadableError)
    detail = str(raised[0])
    assert "private-name" not in detail, detail
    assert "'peek'" in detail, detail
    assert (entry / "01 Dreams.flac").is_file(), "nothing left Trash"


def test_a_link_to_an_audio_file_in_the_trash_still_lists_its_tags(tmp_path: Path) -> None:
    """The control for the gate above: what it may not skip.

    ``os.walk`` lists a link among ``files`` and ``Item.from_path`` follows it, so
    a hand-placed link to a media FILE arrives as a row with real tags (the
    module's own note, measured 2026-09-02). The gate therefore asks what the
    name RESOLVES to, not what the link itself is.
    """
    trash = tmp_path / "trash"
    elsewhere = tmp_path / "elsewhere"
    _tagged_flac(elsewhere / "01 t.flac", artist="Linked", album="Album", title="T", track=1)
    trash.mkdir()
    (trash / "linked.flac").symlink_to(elsewhere / "01 t.flac")

    albums = list_trashed_albums(
        trash, origins_dir=origins_for(trash), music_dir=str(tmp_path / "music")
    )

    assert [(a.folder, a.album_artist, a.track_count) for a in albums] == [
        ("linked.flac", "Linked", 1)
    ]


def test_a_dangling_link_among_the_files_is_skipped_without_an_error(tmp_path: Path) -> None:
    """The other half of following the link: there is nothing at the end of it.

    An unmounted volume is what this looks like. The entry is simply absent from
    every group, and the real album beside it still lists.
    """
    trash = tmp_path / "trash"
    _tagged_flac(trash / "Real Album" / "01 t.flac", artist="A", album="Real", title="T", track=1)
    (trash / "Real Album" / "gone.flac").symlink_to(tmp_path / "nowhere" / "t.flac")

    albums = list_trashed_albums(
        trash, origins_dir=origins_for(trash), music_dir=str(tmp_path / "music")
    )

    assert [(a.folder, a.track_count) for a in albums] == [("Real Album", 1)]


def test_empty_one_removes_a_loose_file(tmp_path: Path) -> None:
    # A loose audio file directly under trash_dir lists with folder=<filename>;
    # emptying it must unlink the file, not 500 on rmtree (NotADirectoryError).
    trash = tmp_path / "trash"
    trash.mkdir()
    (trash / "loose.flac").write_bytes(b"x")
    assert (
        empty_one(
            str(trash / "loose.flac"),
            origins_dir=origins_for(trash),
            protected=protected_for(trash_dir=trash, origins_dir=origins_for(trash)),
        ).removed
        == 1
    )
    assert not (trash / "loose.flac").exists()
