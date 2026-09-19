"""Tests for the reversible delete (move-to-Trash) of albums and artists."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from beets.library import Album, Library
from fastapi import HTTPException

from app.beets import delete as delete_mod
from app.beets import trash as trash_mod
from app.beets.delete import (
    AlbumNotFoundError,
    delete_album,
    delete_album_op,
    delete_artist,
    delete_artist_op,
)
from app.beets.library import LibraryRootUnavailableError, _require_id
from app.beets.protected import ProtectedTrees
from app.beets.trash import album_folder
from app.beets.trash_origins import read_trash_origin, require_usable_store
from app.config import Settings
from app.models.delete import DeleteResult
from tests.conftest import (
    beets_dir_for,
    build_library,
    make_test_handle,
    origins_for,
    protected_for,
)


def _delete(lib: Library, album_id: int, trash: Path) -> DeleteResult:
    """``delete_album`` with the checked pair a request would build."""
    return delete_album(
        lib,
        album_id,
        trash_dir=trash,
        origins_dir=origins_for(trash),
        protected=protected_for(lib, trash_dir=trash, origins_dir=origins_for(trash)),
    )


def test_delete_album_trashes_the_albums_own_files_and_drops(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """Owner ruling ``decisions.md`` 58: per file, not per folder.

    The folder move is what made the released case-insensitive loss reachable —
    two albums sharing one real directory under two spellings, both taken by one
    ``shutil.move``. ``Album.move`` asks each item where IT lives, so only this
    album's files leave; ``test_delete_album_on_a_casefold_fs_leaves_no_dangling_rows``
    is the pin on the loss itself.
    """
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    names = sorted(os.path.basename(os.fsdecode(i.path)) for i in album.items())

    result = _delete(duplicates_lib, album_id, trash)

    assert result.trashed_albums == 1
    assert str(trash) in result.trash_path
    assert duplicates_lib.get_album(album_id) is None  # dropped from the library
    # Every one of its files is in Trash, and only its own: the container is
    # named for the album and holds exactly the tracks the album had.
    (container,) = list(trash.iterdir())
    assert container.name == "Daft Punk - Discovery"
    assert sorted(p.name for p in container.rglob("*.mp3")) == names


def test_delete_album_carries_its_lyric_sidecars_into_trash(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The ``.lrc``/``.txt`` files are MusicDrop's own, so nothing else moves them.

    ``Album.move`` carries what beets TRACKS — the items and ``album.artpath``.
    A sidecar left behind sits in a now audio-empty folder that the reorganize
    orphan sweep trashes as a second entry, which splits one deleted album's
    lyrics across two Trash rows. ``app.beets.sidecars`` holds the one naming
    rule, the same helper edit and reorganize already use.
    """
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    folder = Path(album_folder(duplicates_lib, list(album.items())))
    (folder / "01 Track 1.lrc").write_text("[00:01.00] x", encoding="utf-8")
    (folder / "02 Track 2.txt").write_text("plain words", encoding="utf-8")

    result = _delete(duplicates_lib, album_id, trash)

    landed = Path(result.trash_path)
    assert (landed / "01 Track 1.lrc").read_text(encoding="utf-8") == "[00:01.00] x"
    assert (landed / "02 Track 2.txt").read_text(encoding="utf-8") == "plain words"
    # Beside their own track, which is the whole contract: Plex reads a sidecar
    # by the audio file's stem.
    assert (landed / "01 Track 1.mp3").is_file()
    # The folder goes too: with the sidecars carried there is nothing left in
    # it, and beets' own ``prune_dirs`` is asked again after the carry.
    assert not folder.exists()
    assert not folder.parent.exists()  # the artist level as well, as beets does


def test_a_sidecar_that_will_not_move_does_not_fail_the_delete(
    duplicates_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audio has ALREADY moved by then, so no sidecar fault may rewrite that.

    ``move_sidecars`` never raises — it logs every skip — and the carry runs
    after the primitive returned, so the two properties this pins are that the
    delete still succeeds and that the sidecar is left where it is rather than
    half-moved.

    The fault is injected by suffix so only the sidecar move fails: patching
    ``shutil.move`` outright would also hit the movers in ``trash.py``.
    """
    import shutil as shutil_mod

    real_move = shutil_mod.move

    def _refuses_sidecars(src: str, dst: str, *args: object, **kwargs: object) -> str:
        if src.endswith((".lrc", ".txt")):
            raise PermissionError(13, "Permission denied")
        moved: str = real_move(src, dst, *args, **kwargs)  # type: ignore[arg-type]  # passthrough
        return moved

    monkeypatch.setattr("app.beets.sidecars.shutil.move", _refuses_sidecars)
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    folder = Path(album_folder(duplicates_lib, list(album.items())))
    (folder / "01 Track 1.lrc").write_text("[00:01.00] x", encoding="utf-8")

    result = _delete(duplicates_lib, album_id, trash)

    assert result.trashed_albums == 1
    assert duplicates_lib.get_album(album_id) is None  # the delete really happened
    landed = Path(result.trash_path)
    assert (landed / "01 Track 1.mp3").is_file()  # the audio went
    assert not (landed / "01 Track 1.lrc").exists()  # the sidecar did not
    assert (folder / "01 Track 1.lrc").is_file()  # ...and is still readable where it was


def test_a_prune_that_raises_does_not_fail_the_delete(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The re-prune runs after the files moved, so it may not undo that. MEASURED.

    A real fault, not a spied one: ``prune_dirs`` wraps only its ``rmtree``
    (beets ``util/__init__.py``), so its ``os.listdir`` of an unreadable ancestor
    propagates. The ARTIST folder is mode 0o311 — traversable and writable, not
    listable — which lets beets' own prune inside ``Album.move`` break at the
    album folder (the ``.lrc`` is still in it) and lets the carry's re-prune
    rmtree that folder and then hit ``PermissionError`` one level up.

    This drives the ALBUM route only. An artist fan-out stopping mid-way with one
    album's delete already done is the worse shape and is NOT pinned here.
    """
    if os.geteuid() == 0:
        pytest.skip("root ignores the directory mode this fault needs")

    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    folder = Path(album_folder(duplicates_lib, list(album.items())))
    (folder / "01 Track 1.lrc").write_text("[00:01.00] x", encoding="utf-8")
    artist_dir = folder.parent
    artist_dir.chmod(0o311)  # -wx: traverse and write, but do not list
    try:
        result = _delete(duplicates_lib, album_id, trash)
    finally:
        artist_dir.chmod(0o755)

    assert result.trashed_albums == 1
    assert duplicates_lib.get_album(album_id) is None
    landed = Path(result.trash_path)
    assert (landed / "01 Track 1.lrc").is_file(), "the carry itself still happened"
    assert not folder.exists(), "and the prune got as far as the album folder"
    assert artist_dir.is_dir(), "the level it could not read is left alone"


def test_the_re_prune_uses_the_users_clutter_list_and_not_the_function_default(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """``Thumbs.DB`` is clutter to beets' CONFIG and not to ``prune_dirs``' default.

    ``prune_dirs``' signature default is ``(".DS_Store", "Thumbs.db")`` and the
    config default is ``["Thumbs.DB", ".DS_Store"]`` — one letter apart, and
    case-sensitive on Linux. So a folder left holding only a ``Thumbs.DB`` is
    pruned by the argument beets itself passes and kept by the default, and the
    user's own ``clutter:`` list is not consulted at all.

    Measured: without the ``clutter=`` argument in ``carry_sidecars`` the whole
    suite stayed green, so this is the only reader that line has.
    """
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    folder = Path(album_folder(duplicates_lib, list(album.items())))
    (folder / "01 Track 1.lrc").write_text("[00:01.00] x", encoding="utf-8")
    (folder / "Thumbs.DB").write_bytes(b"\x00")  # the config spelling, not the default's

    _delete(duplicates_lib, album_id, trash)

    assert not folder.exists(), "clutter-only is empty enough to prune"


def test_a_delete_leaves_a_sidecar_another_albums_track_claims(tmp_path: Path) -> None:
    """One ``.lrc``, two tracks at the same stem — it is both albums' lyrics.

    ``01 T1.flac`` and ``01 T1.mp3`` in one folder share ``01 T1.lrc``: measured,
    deleting the first album carried the second album's lyrics into Trash, and
    the row that still needs them was left with none. The rule is the one import
    Replace already follows — a sidecar a REMAINING item row can claim stays.

    ``test_delete_album_carries_its_lyric_sidecars_into_trash`` is the control:
    with no other claimant the sidecar travels, so this cannot pass by never
    carrying anything.
    """
    from beets.library import Item

    music = tmp_path / "music"
    lib = build_library(str(beets_dir_for(tmp_path) / "library.db"), str(music))
    folder = music / "Sharey" / "Both"
    folder.mkdir(parents=True)

    def add(*, album: str, name: str) -> Album:
        track = folder / name
        track.write_bytes(b"\x00")
        item = Item(album=album, albumartist="Sharey", artist="Sharey", title="T1", track=1)
        item.path = os.fsencode(str(track))
        made: Album = lib.add_album([item])
        made.store()
        return made

    doomed = add(album="A Doomed", name="01 T1.flac")
    add(album="B Keeper", name="01 T1.mp3")
    shared = folder / "01 T1.lrc"
    shared.write_text("[00:01.00] both of ours", encoding="utf-8")

    trash = tmp_path / "trash"
    _delete(lib, _require_id(doomed.id), trash)

    assert not list(trash.rglob("*.lrc")), "the other album's lyrics did not travel"
    assert shared.read_text(encoding="utf-8") == "[00:01.00] both of ours"
    assert (folder / "01 T1.mp3").is_file(), "and neither did its track"
    assert list(trash.rglob("*.flac")), "the deleted album's own file did move"


def test_a_sidecar_claim_is_seen_for_tracks_in_the_music_root(tmp_path: Path) -> None:
    """A flat library (``paths: default: $title``) is a documented layout. REGRESSION.

    ``relpath(music, music)`` is ``"."``, so the relative prefix was ``./`` and
    matched no stored row: measured, the neighbour's ``01 T1.lrc`` was carried
    into Trash from the music root while the same shape one folder down was
    left alone. The root's prefix is EMPTY, which brings every row back; the
    answer stays about this directory because a stem is an absolute path, so a
    row one folder down produces a different one.
    """
    from beets.library import Item

    music = tmp_path / "music"
    music.mkdir()
    lib = build_library(
        str(beets_dir_for(tmp_path) / "library.db"), str(music), path_format="$title"
    )

    def add(*, album: str, name: str) -> Album:
        track = music / name
        track.write_bytes(b"\x00")
        item = Item(album=album, albumartist="Sharey", artist="Sharey", title="T1", track=1)
        item.path = os.fsencode(str(track))
        made: Album = lib.add_album([item])
        made.store()
        return made

    doomed = add(album="A Doomed", name="01 T1.flac")
    add(album="B Keeper", name="01 T1.mp3")
    shared = music / "01 T1.lrc"
    shared.write_text("[00:01.00] both of ours", encoding="utf-8")

    trash = tmp_path / "trash"
    _delete(lib, _require_id(doomed.id), trash)

    assert not list(trash.rglob("*.lrc")), "the neighbour's lyrics did not travel"
    assert shared.read_text(encoding="utf-8") == "[00:01.00] both of ours"
    assert list(trash.rglob("*.flac")), "the deleted album's own file did move"


@pytest.mark.parametrize("neighbour", ["01 T1.1.mp3", "01 T1.5 (remix).mp3"])
def test_a_beets_collision_divert_does_not_claim_the_other_albums_sidecar(
    tmp_path: Path, neighbour: str
) -> None:
    """The claim is a STEM comparison, not a prefix one. MEASURED both ways.

    ``01 T1.1.mp3`` is what beets itself writes for a Keep-both in the same
    format, and ``01 T1.5 (remix).mp3`` is an ordinary name; both start with
    ``01 T1.`` and neither owns ``01 T1.lrc``. A prefix test read them as
    claimants and left the deleted album's own lyrics behind for nobody.

    ``test_a_delete_leaves_a_sidecar_another_albums_track_claims`` is the other
    direction, where the neighbour really is at the same stem.
    """
    from beets.library import Item

    music = tmp_path / "music"
    lib = build_library(str(beets_dir_for(tmp_path) / "library.db"), str(music))
    folder = music / "Sharey" / "Both"
    folder.mkdir(parents=True)

    def add(*, album: str, name: str) -> Album:
        track = folder / name
        track.write_bytes(b"\x00")
        item = Item(album=album, albumartist="Sharey", artist="Sharey", title="T1", track=1)
        item.path = os.fsencode(str(track))
        made: Album = lib.add_album([item])
        made.store()
        return made

    doomed = add(album="A Doomed", name="01 T1.mp3")
    add(album="B Neighbour", name=neighbour)
    (folder / "01 T1.lrc").write_text("[00:01.00] the doomed album's", encoding="utf-8")

    trash = tmp_path / "trash"
    _delete(lib, _require_id(doomed.id), trash)

    assert not (folder / "01 T1.lrc").exists(), "nobody left behind owns it"
    landed = [p.name for p in trash.rglob("*.lrc")]
    assert landed == ["01 T1.lrc"], "it went to Trash with its own track"
    assert (folder / neighbour).is_file(), "and the neighbour is untouched"


def test_a_sidecar_claim_is_seen_through_the_absolute_row_spelling(tmp_path: Path) -> None:
    """Rows stored ABSOLUTE — an album outside the music dir — claim too.

    beets stores paths relative to ``lib.directory`` in the ordinary case and
    absolute for outside/legacy rows, so the claim query asks both spellings.
    Measured: with only the relative one, this shape carried the other album's
    lyrics away and the whole suite stayed green.
    """
    from beets.library import Item

    music = tmp_path / "music"
    music.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    lib = build_library(str(beets_dir_for(tmp_path) / "library.db"), str(music))
    # One album inside the library keeps ``require_library_root`` satisfied.
    resident = music / "Resident" / "Album"
    resident.mkdir(parents=True)
    (resident / "01 Here.mp3").write_bytes(b"\x00")
    here = Item(album="Resident", albumartist="Resident", artist="Resident", title="H", track=1)
    here.path = os.fsencode(str(resident / "01 Here.mp3"))
    lib.add_album([here]).store()

    def add(*, album: str, name: str) -> Album:
        track = outside / name
        track.write_bytes(b"\x00")
        item = Item(album=album, albumartist="Sharey", artist="Sharey", title="T1", track=1)
        item.path = os.fsencode(str(track))
        made: Album = lib.add_album([item])
        made.store()
        return made

    doomed = add(album="A Doomed", name="01 T1.flac")
    add(album="B Keeper", name="01 T1.mp3")
    shared = outside / "01 T1.lrc"
    shared.write_text("[00:01.00] both of ours", encoding="utf-8")

    trash = tmp_path / "trash"
    _delete(lib, _require_id(doomed.id), trash)

    assert shared.is_file(), "the neighbour's lyrics stayed"
    assert not list(trash.rglob("*.lrc"))


def test_delete_album_takes_the_tracked_cover_and_leaves_a_strangers_files(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The cover beets tracks as ``album.artpath`` goes; a booklet or a loose
    cover is not the album's to move, so it stays — and so does the folder
    holding it.

    Two files of the same KIND, one tracked and one not, in one folder: measured,
    ``Album.move``'s ``move_art`` carries ``artpath`` and nothing else, so what
    decides is the beets row rather than the extension.

    The counter-case is in the same test rather than a separate one, because
    "the folder stays" only means something beside "an emptied folder does not":
    ``prune_dirs`` rmtree's every ancestor its move emptied, up to
    ``lib.directory`` (measured — deleting a lone album took its artist folder
    with it). This is the arm that pins the BREAK in that walk — a folder holding
    something that is not the album's stops it — and
    ``test_delete_album_carries_its_lyric_sidecars_into_trash`` pins the other
    side, where the prune ``delete.py`` asks for after the carry takes both
    levels.
    """
    trash = tmp_path / "trash"
    keeper = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    keeper_id = _require_id(keeper.id)
    kept_folder = Path(album_folder(duplicates_lib, list(keeper.items())))
    (kept_folder / "booklet.pdf").write_bytes(b"%PDF-1.4")
    (kept_folder / "front.jpg").write_bytes(b"\xff\xd8\xff")  # a cover beets does NOT track
    tracked_art = kept_folder / "cover.jpg"
    tracked_art.write_bytes(b"\xff\xd8\xffcover")
    keeper.artpath = os.fsencode(str(tracked_art))
    keeper.store()
    bare = next(a for a in duplicates_lib.albums() if a.albumartist == "Boards of Canada")
    bare_id = _require_id(bare.id)
    bare_folder = Path(album_folder(duplicates_lib, list(bare.items())))

    result = _delete(duplicates_lib, keeper_id, trash)
    _delete(duplicates_lib, bare_id, trash)

    assert (Path(result.trash_path) / "cover.jpg").read_bytes() == b"\xff\xd8\xffcover"
    assert sorted(p.name for p in kept_folder.iterdir()) == ["booklet.pdf", "front.jpg"]
    assert not bare_folder.exists()  # beets pruned the one it emptied


def test_delete_album_ghost_files_already_gone(duplicates_lib: Library, tmp_path: Path) -> None:
    """Files deleted on disk outside MusicDrop -> drop the ghost rows, don't 500.

    Reproduces the live user case: an artist folder removed over SMB leaves the
    beets DB rows behind. The front-door delete must clean them, not fail on the
    missing source files.

    ``trash.exists()`` is no longer the assertion it was: the per-item mover
    ``mkdir``s the Trash before it moves and its ghost arm ``rmdir``s only the
    container it made, so what this pins is that no ENTRY was left in Trash.
    """
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    folder = album_folder(duplicates_lib, list(album.items()))
    shutil.rmtree(folder)  # ghost: DB rows remain, the files are gone

    result = _delete(duplicates_lib, album_id, trash)

    assert result.trashed_albums == 1
    assert duplicates_lib.get_album(album_id) is None  # ghost rows dropped
    assert list(trash.iterdir()) == []  # nothing relocated — there was nothing to move
    assert not list(origins_for(trash).glob("*.json"))  # and no record describing nothing


def test_delete_album_unknown_id_raises(duplicates_lib: Library, tmp_path: Path) -> None:
    protected = protected_for(
        duplicates_lib, trash_dir=tmp_path / "trash", origins_dir=tmp_path / "trash-origins"
    )
    with pytest.raises(AlbumNotFoundError):
        delete_album(
            duplicates_lib,
            999_999,
            trash_dir=tmp_path / "trash",
            origins_dir=tmp_path / "trash-origins",
            protected=protected,
        )


def test_delete_artist_trashes_all_their_albums(duplicates_lib: Library, tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    before = [a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk"]
    assert before, "fixture should have at least one Daft Punk album"

    result = delete_artist(
        duplicates_lib,
        "Daft Punk",
        trash_dir=trash,
        origins_dir=origins_for(trash),
        protected=protected_for(duplicates_lib, trash_dir=trash, origins_dir=origins_for(trash)),
    )

    assert result.trashed_albums == len(before)
    assert not [a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk"]


def test_delete_artist_unknown_is_noop(duplicates_lib: Library, tmp_path: Path) -> None:
    result = delete_artist(
        duplicates_lib,
        "Nobody At All",
        trash_dir=tmp_path / "trash",
        origins_dir=tmp_path / "trash-origins",
        protected=protected_for(
            duplicates_lib, trash_dir=tmp_path / "trash", origins_dir=tmp_path / "trash-origins"
        ),
    )
    assert result.trashed_albums == 0


def test_delete_album_op_409_during_backfill() -> None:
    """A running library job blocks delete with 409 — before touching the lib."""
    from app.lyrics_jobs.registry import reset_lyrics_backfill

    reset_lyrics_backfill().start(writes_enabled=True)

    class _App:
        class state:
            beets_library = None

    class _Req:
        app = _App()

    try:
        req = _Req()
        # Calling the async op only CREATES the coroutine — nothing runs (and
        # nothing can raise) until asyncio.run drives it inside the block.
        op = delete_album_op(req, 1)  # type: ignore[arg-type]
        with pytest.raises(HTTPException) as ei:
            asyncio.run(op)
        assert ei.value.status_code == 409
    finally:
        reset_lyrics_backfill()


# ----- The unmounted-share guard, from the front door -----
#
# Both delete entry points ride ``trash_album``, whose ghost branch reads a
# missing source file as "deleted outside MusicDrop". With the library root
# itself gone that reading is wrong for every album at once, so the primitive's
# root guard has to fire before the first row drop — and the drop it prevents
# would otherwise be permanent, because a beets ``Transaction`` commits on the
# way out even while unwinding an exception (dbcore/db.py:924-941).


def test_delete_album_root_unavailable_keeps_rows(duplicates_lib: Library, tmp_path: Path) -> None:
    """No DeleteResult(trashed_albums=1) lie: the op raises and the album stays."""
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    shutil.rmtree(os.fsdecode(duplicates_lib.directory))

    origins = origins_for(trash)
    protected = protected_for(duplicates_lib, trash_dir=trash, origins_dir=origins)
    with pytest.raises(LibraryRootUnavailableError):
        delete_album(
            duplicates_lib,
            album_id,
            trash_dir=trash,
            origins_dir=origins,
            protected=protected,
        )

    assert duplicates_lib.get_album(album_id) is not None  # still queryable
    assert not trash.exists()


def test_delete_artist_root_unavailable_drops_nothing(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The fan-out aborts on the FIRST album, before any row is dropped.

    Radiohead holds two albums in the fixture; a guard that ran anywhere after
    the first ``album.remove`` would leave one row committed and unrecoverable.
    """
    trash = tmp_path / "trash"
    before = [_require_id(a.id) for a in duplicates_lib.albums() if a.albumartist == "Radiohead"]
    assert len(before) == 2, "fixture should hold two Radiohead albums"
    total_before = len(list(duplicates_lib.albums()))
    shutil.rmtree(os.fsdecode(duplicates_lib.directory))

    origins = origins_for(trash)
    protected = protected_for(duplicates_lib, trash_dir=trash, origins_dir=origins)
    with pytest.raises(LibraryRootUnavailableError):
        delete_artist(
            duplicates_lib,
            "Radiohead",
            trash_dir=trash,
            origins_dir=origins,
            protected=protected,
        )

    assert [_require_id(a.id) for a in duplicates_lib.albums() if a.albumartist == "Radiohead"] == (
        before
    )
    assert len(list(duplicates_lib.albums())) == total_before
    assert not trash.exists()


def test_delete_album_op_503_root_unavailable(duplicates_lib: Library, tmp_path: Path) -> None:
    """The HTTP mapping: 503 + the flat honest sentence, not the structured 500.

    The blanket ``except`` above would answer with the 500's recovery line, which
    sends the reader to check a Trash folder this operation never created (and
    for most of this branch's life promised the files were already in it), so the
    root-unavailable arm has to sit in front of it. The 503 says the one thing
    that is known instead. The stub request is enough because ``_swap_lock``
    creates its lock lazily on whatever ``app.state`` it is handed
    (config_editor.py:624-630) and ``_settings`` falls back to the module
    singleton when ``state.settings`` is absent (:632-645).
    """
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    handle = make_test_handle(duplicates_lib, beets_dir_for(tmp_path))
    shutil.rmtree(os.fsdecode(duplicates_lib.directory))

    class _App:
        state = SimpleNamespace(beets_library=handle)

    class _Req:
        app = _App()

    req = _Req()
    coro = delete_album_op(req, album_id)  # type: ignore[arg-type]  # duck-typed stub
    with pytest.raises(HTTPException) as ei:
        asyncio.run(coro)

    assert ei.value.status_code == 503
    assert ei.value.detail == "Library folder unavailable. Is the music share mounted?"
    assert duplicates_lib.get_album(album_id) is not None


def test_delete_artist_op_503_root_unavailable(duplicates_lib: Library, tmp_path: Path) -> None:
    """The artist op maps the same cause the same way — its own arm, its own pin.

    Without this the artist half of the mapping is unpinned: the route-status
    census reads the RAISE and only flags a status raised-but-undeclared, so
    deleting the ``except`` here would leave a declared 503 nothing produces and
    hand the fan-out back the blanket 500, whose recovery line can only tell the
    user to go and look in Trash.
    """
    handle = make_test_handle(duplicates_lib, beets_dir_for(tmp_path))
    before = len(list(duplicates_lib.albums()))
    shutil.rmtree(os.fsdecode(duplicates_lib.directory))

    class _App:
        state = SimpleNamespace(beets_library=handle)

    class _Req:
        app = _App()

    req = _Req()
    coro = delete_artist_op(req, "Radiohead")  # type: ignore[arg-type]  # duck-typed stub
    with pytest.raises(HTTPException) as ei:
        asyncio.run(coro)

    assert ei.value.status_code == 503
    assert ei.value.detail == "Library folder unavailable. Is the music share mounted?"
    assert len(list(duplicates_lib.albums())) == before


# ----- the SECOND setup fault a delete refuses: an unusable origin store ------
#
# Owner ruling, ``decisions.md`` 28. Same tier and same shape as the root guard
# above, for the same reason: the check runs before anything moves or is
# dropped, so the one fact known on every path is the one the 503 states.


def _store_fault_req(
    lib: Library, tmp_path: Path, trash: Path, *, origins: Path | None = None
) -> object:
    """A stub request whose settings point the ops at ``trash``'s sibling store.

    ``origins`` overrides that sibling, which a Trash INSIDE the music library
    needs: the layout rule refuses an origin store under the music root, and it
    runs first, so the sibling default would answer a different refusal than the
    test is about.
    """
    handle = make_test_handle(lib, beets_dir_for(tmp_path))

    class _App:
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(
                trash_dir=str(trash),
                trash_origins_dir=str(origins if origins is not None else origins_for(trash)),
            ),
        )

    class _Req:
        app = _App()

    return _Req()


@pytest.mark.parametrize("artist", [None, "Radiohead"], ids=["album", "artist"])
@pytest.mark.parametrize(
    ("shape", "says"),
    [
        ("file", "is not a usable folder"),
        ("unsearchable", "cannot be read"),
        ("unreadable", "cannot be read"),
    ],
)
def test_delete_op_503_when_the_origin_store_cannot_be_used(
    duplicates_lib: Library, tmp_path: Path, artist: str | None, shape: str, says: str
) -> None:
    """Both ops, both measured shapes: a 503 that names the store and leaks no path.

    The two shapes are not a duplication. ``file`` — a regular FILE where the
    store belongs — is the one nothing could see before this guard, because
    ``Path.exists()`` absorbs the ENOTDIR its children raise, and it is
    ROOT-PROOF: it denies for uid 0 exactly as it does for uid 1000.
    ``unsearchable`` — the directory at mode 0600 — is the ruling's own headline
    shape (a bad ``PUID``/``PGID``, a restored backup) and is the one that must
    skip under root, where a mode bit denies nothing and the arm would report
    green having exercised no fault.

    Both ends of the message are asserted, because they pull in opposite
    directions: the sentence has to NAME the store (or the reader cannot act on
    it) while carrying no absolute path (the presence refusal beside it leaks
    none either — ``test_refusal_message_leaks_no_path``), and only asserting
    one of the two lets the other regress.

    ``unreadable`` — mode 0300, ``-wx`` — is the third, and it is the only shape
    the ``scandir`` probe answers by itself: measured at 0300, the ``mkdir``
    succeeds, the ``stat`` of the never-there key answers ENOENT (healthy) and
    the ``mkstemp`` write succeeds, so with the ``scandir`` dropped the whole
    guard passes and the delete runs. It is the mode a store gets when the Empty
    all sweep can no longer list it, which is the half of the probe the other
    two rows do not reach. Skips under root for the same reason as the row
    above.

    ``says`` pins WHICH of the three probes answered, and it is not decoration:
    an unsearchable store fails the write probe too, so without it the search
    probe is a line no test can kill (measured — dropping it left 59 tests
    green, with the refusal still raised and only its sentence changed from
    "cannot be read" to "cannot be written"). The two sentences send an operator
    at different permission bits.
    """
    if shape != "file" and os.getuid() == 0:
        pytest.skip("running as root: a mode bit on a directory denies nothing")
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    folder = album_folder(duplicates_lib, list(album.items()))
    total_before = len(list(duplicates_lib.albums()))
    if shape == "file":
        origins.write_bytes(b"not a directory")
    else:
        origins.mkdir()
        (origins / "Someone Elses.json").write_text("{}", encoding="utf-8")
        # 0600, not 0000: the ``unsearchable`` shape LISTS fine and only fails a
        # lookup of a child, which is the fault ``origin_recorded`` meets and the
        # only one the search probe is there for. At 0000 the ``scandir`` probe
        # answers first, and the ``stat`` becomes a line no test can kill
        # (measured). 0300 is the mirror: it cannot be LISTED and everything
        # else about it works.
        origins.chmod(0o600 if shape == "unsearchable" else 0o300)

    req = _store_fault_req(duplicates_lib, tmp_path, trash)
    op = (
        delete_artist_op(req, artist)  # type: ignore[arg-type]  # stub req
        if artist is not None
        else delete_album_op(req, album_id)  # type: ignore[arg-type]  # ditto
    )
    try:
        with pytest.raises(HTTPException) as ei:
            asyncio.run(op)
    finally:
        if shape != "file":
            origins.chmod(0o700)

    assert ei.value.status_code == 503
    detail = ei.value.detail
    assert isinstance(detail, str), "flat, like the root guard's — not the structured 500"
    assert "trash-origins" in detail, "the reader has to be told WHICH folder to fix"
    assert says in detail, "and which of the three questions the store failed"
    assert str(origins) not in detail, "the store's absolute path must not be in it"
    assert str(tmp_path) not in detail, "...nor any prefix of it"
    # The promise sits BETWEEN the cause and the instruction — "...: Permission
    # denied. Nothing has been deleted. Fix its permissions or its mount, then
    # retry." — so the reader is reassured before being told what to do.
    assert detail.endswith(
        ". Nothing has been deleted. Fix its permissions or its mount, then retry."
    ), detail
    # ...and the disk and the library agree with the sentence. The artist arm
    # is Radiohead, which holds TWO albums in the fixture, so the count is what
    # says the whole fan-out was refused rather than only its first album.
    assert len(list(duplicates_lib.albums())) == total_before
    assert duplicates_lib.get_album(album_id) is not None
    assert os.path.isdir(folder), "the album's files are still where the library says"
    # The Trash ROOT exists — the request's own IDENTITY check creates it
    # (``checked_protected_trees``; the layout walk creates nothing) — and
    # nothing was allocated in it.
    assert list(trash.iterdir()) == [], "no Trash name was allocated"
    if shape == "file":
        assert origins.read_bytes() == b"not a directory", "the store is as it was"
    else:
        assert sorted(p.name for p in origins.iterdir()) == ["Someone Elses.json"]


def test_a_fanout_that_already_moved_one_album_does_not_promise_nothing_was_deleted(
    duplicates_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store going unusable BETWEEN two albums answers 500 without the promise.

    "Nothing has been deleted" used to live in the store refusal's own sentence
    (``trash_origins._STORE_FIX``), which every mover relays. The fan-out asks
    the store once PER ALBUM, so album 2's refusal was relayed into a body that
    said, forty words apart, that one album had been moved to Trash and that
    nothing had been deleted — in the one field the browser renders. The promise
    now belongs to the two 503 arms, which fire only before anything is created,
    moved or dropped, and this is the run that must not have it.

    The seam is placed by wrapping the guard, but the refusal itself is REAL: the
    wrapper replaces the store with a regular FILE between the two albums and
    then calls the guard, so the sentence the 500 relays is the store's own —
    which is the only way this test can see where that sentence puts the promise.
    The FILE shape rather than a chmod because it denies for uid 0 too, so the
    test does not self-skip for a maintainer running this suite inside the
    shipped image, which declares no ``USER`` (``Dockerfile``).
    """
    trash = tmp_path / "trash"
    calls: list[Path] = []
    real = require_usable_store  # the same object ``trash`` imported, before the patch

    def _breaks_the_store_before_the_second_album(origins_dir: Path) -> None:
        calls.append(origins_dir)
        if len(calls) > 1:
            shutil.rmtree(origins_dir)
            origins_dir.write_bytes(b"not a directory")
        real(origins_dir)

    monkeypatch.setattr(
        trash_mod, "require_usable_store", _breaks_the_store_before_the_second_album
    )
    req = _store_fault_req(duplicates_lib, tmp_path, trash)

    op = delete_artist_op(req, "Radiohead")  # type: ignore[arg-type]  # stub req
    with pytest.raises(HTTPException) as ei:
        asyncio.run(op)

    assert len(calls) == 2, "one ask per album, and the fan-out stopped on the second"
    assert ei.value.status_code == 500, "the 503 tier closed the moment an album was dropped"
    detail = ei.value.detail
    assert isinstance(detail, dict), "the structured body, not the 503's flat sentence"
    message = detail["message"]
    assert "Nothing has been deleted" not in message, "one album HAS been"
    assert "trash-origins" in message, "the cause is still relayed, in the store's own words"
    assert "is not a usable folder" in message, "...naming which question the store failed"
    assert "1 of 2 albums had been moved to Trash" in message, "...and how far it got"
    # ...and the disk agrees with the half the message does claim.
    assert len(list(trash.iterdir())) == 1, "the first album really is in Trash"
    remaining = [a.album for a in duplicates_lib.albums() if a.albumartist == "Radiohead"]
    assert len(remaining) == 1, "the second album kept its rows"


def test_delete_album_op_503_masked_drop_says_what_to_do(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The wire answer for a drop the ROOT check cannot see.

    A ``.stfolder`` on the local mountpoint keeps ``require_library_root``
    passing, so without the stronger predicate this request would answer
    ``200 trashed_albums=1`` having moved nothing and dropped the rows. It must
    be a 503 whose sentence tells the user what to check — and which names no
    filesystem path, because the detail is rendered straight into the dialog.
    """
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    handle = make_test_handle(duplicates_lib, beets_dir_for(tmp_path))
    root = Path(os.fsdecode(duplicates_lib.directory))
    shutil.rmtree(root)
    root.mkdir(parents=True)
    (root / ".stfolder").mkdir()

    class _App:
        state = SimpleNamespace(beets_library=handle)

    class _Req:
        app = _App()

    req = _Req()
    coro = delete_album_op(req, album_id)  # type: ignore[arg-type]  # duck-typed stub
    with pytest.raises(HTTPException) as ei:
        asyncio.run(coro)

    assert ei.value.status_code == 503
    assert ei.value.detail == (
        "Library folder is present but none of the music files the library names"
        " are in it. Either the music share is not mounted, or those files have"
        " been removed outside MusicDrop."
    )
    assert duplicates_lib.get_album(album_id) is not None  # rows kept
    assert str(root) not in str(ei.value.detail)


def test_delete_artist_root_gone_raises_before_the_transaction(
    duplicates_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ENTRY check is what makes the artist 503's "nothing was dropped" true.

    Relying on the primitive alone would leave that claim conditional: the
    primitive only checks the root once it is already inside the transaction,
    per album. Pinned by stubbing the primitive out entirely — with nothing else
    in the fan-out able to raise, a raise here can only have come from the
    check that runs before the loop.
    """
    calls: list[int] = []

    def _never(*args: object, **kwargs: object) -> str:
        calls.append(1)
        return ""

    monkeypatch.setattr(delete_mod, "_trash_one", _never)
    before = len(list(duplicates_lib.albums()))
    shutil.rmtree(os.fsdecode(duplicates_lib.directory))

    protected = protected_for(
        duplicates_lib, trash_dir=tmp_path / "trash", origins_dir=tmp_path / "trash-origins"
    )
    with pytest.raises(LibraryRootUnavailableError):
        delete_artist(
            duplicates_lib,
            "Radiohead",
            trash_dir=tmp_path / "trash",
            origins_dir=tmp_path / "trash-origins",
            protected=protected,
        )

    assert calls == []  # the fan-out never started
    assert len(list(duplicates_lib.albums())) == before


def test_delete_artist_op_reports_partial_progress_for_ANY_cause(
    duplicates_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same two tiers, for a cause that is not the unmounted share.

    The test below pins this for ``LibraryRootUnavailableError``, and until now
    that was the ONLY cause that got it: a permission error, a full disk or a DB
    fault mid-fan-out reached the user as a bare message with no count, while
    beets had already committed every album before it. Same state, strictly
    worse reporting, and nothing said which.

    ``PermissionError`` here stands for the whole class -- what matters is that
    it is not the one exception type the arm above already names.
    """
    trash = tmp_path / "trash"
    handle = make_test_handle(duplicates_lib, beets_dir_for(tmp_path))
    real = delete_mod._trash_one
    calls = {"n": 0}

    def _fails_on_the_second(
        lib: Library,
        album: object,
        *,
        trash_dir: Path,
        origins_dir: Path,
        protected: ProtectedTrees,
    ) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            raise PermissionError(13, "Permission denied")
        return str(
            real(lib, album, trash_dir=trash_dir, origins_dir=origins_dir, protected=protected)
        )

    monkeypatch.setattr(delete_mod, "_trash_one", _fails_on_the_second)

    class _App:
        state = SimpleNamespace(beets_library=handle, settings=Settings(trash_dir=str(trash)))

    class _Req:
        app = _App()

    req = _Req()
    op = delete_artist_op(req, "Radiohead")  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as ei:
        asyncio.run(op)

    assert ei.value.status_code == 500
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert "1 of 2" in detail["message"]  # the count survives, which it did not before
    assert "Permission denied" in detail["message"]  # and the real cause is still named


def test_delete_artist_op_mid_flight_drop_reports_partial_progress(
    duplicates_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A share that drops MID fan-out must not answer with the 503.

    The 503 (and its OpenAPI description) says nothing was moved or dropped.
    Once an album has been through the primitive that is false and unfixable —
    beets commits on the way out of the transaction even while unwinding the
    exception — so this case has to surface as the structured 500 whose message
    states how far the fan-out got.
    """
    trash = tmp_path / "trash"
    handle = make_test_handle(duplicates_lib, beets_dir_for(tmp_path))
    real = delete_mod._trash_one
    calls = {"n": 0}

    def _drops_on_the_second(
        lib: Library,
        album: object,
        *,
        trash_dir: Path,
        origins_dir: Path,
        protected: ProtectedTrees,
    ) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            raise LibraryRootUnavailableError(
                "Library folder unavailable. Is the music share mounted?"
            )
        return str(
            real(lib, album, trash_dir=trash_dir, origins_dir=origins_dir, protected=protected)
        )

    monkeypatch.setattr(delete_mod, "_trash_one", _drops_on_the_second)

    class _App:
        # An explicit Settings pins the Trash location inside tmp_path: this is
        # the one op test that really moves files, and _settings would otherwise
        # fall back to the module singleton (a dev .env could point anywhere).
        state = SimpleNamespace(beets_library=handle, settings=Settings(trash_dir=str(trash)))

    class _Req:
        app = _App()

    req = _Req()
    coro = delete_artist_op(req, "Radiohead")  # type: ignore[arg-type]  # duck-typed stub
    with pytest.raises(HTTPException) as ei:
        asyncio.run(coro)

    assert ei.value.status_code == 500  # NOT the nothing-was-dropped 503
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert "1 of 2" in detail["message"]  # names how far it got
    assert "music share" in detail["message"]  # and why it stopped
    # The library agrees with the message: one album trashed, one untouched.
    assert len([a for a in duplicates_lib.albums() if a.albumartist == "Radiohead"]) == 1
    assert trash.is_dir()


def test_delete_album_op_records_an_origin_in_the_store_the_listing_reads(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The op resolves BOTH dirs, and only an end-to-end check sees the second.

    Every unit test below the op passes ``origins_dir`` explicitly, so an op that
    resolved it wrong — as the Trash dir, say, which would put the records inside
    the entry namespace the listing walks — is invisible to all of them and
    silently degrades every deleted album to a row with no origin shown at all.

    ``restore_mode`` is ``"import"`` and not ``"move_back"``: the per-item mover
    records ``moved="items"``, which is what a delete writes from now on. The
    move-back mode is still produced by the reorganize orphan sweep
    (``trash.trash_folder``) and by every album a released version deleted, so
    the value is live — it is simply not this route's any more.
    """
    from app.beets.trash_manage import list_trashed_albums

    trash = tmp_path / "trash"
    origins = tmp_path / "trash-origins"
    handle = make_test_handle(duplicates_lib, beets_dir_for(tmp_path))
    album = next(iter(duplicates_lib.albums()))
    album_id = _require_id(album.id)
    album_root = os.path.dirname(os.fsdecode(next(iter(album.items())).path))

    class _App:
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_dir=str(trash), trash_origins_dir=str(origins)),
        )

    class _Req:
        app = _App()

    asyncio.run(delete_album_op(_Req(), album_id))  # type: ignore[arg-type]  # duck-typed stub

    assert list(origins.glob("*.json")), "the op resolved no origin store"
    assert not list(trash.glob("*.json")), "records must not land in the entry namespace"
    rows = list_trashed_albums(
        trash, origins_dir=origins, music_dir=os.fsdecode(duplicates_lib.directory)
    )
    (row,) = [r for r in rows if r.origin == album_root]
    assert row.restore_mode == "import"


# --- the partial-failure message may only claim what really happened ---------
# The fan-out's 500 is read by someone whose delete half-finished, and it used
# to tell them two things it could not know: that "the music share became
# unavailable" (the guard that refuses says it is EITHER that or an artist whose
# folders were removed outside MusicDrop, and cannot tell which), and that N
# albums are waiting in Trash (the primitive drops the rows of a ghost or an
# empty album having moved nothing at all).


_GHOST_ARTIST = "Ghosty"

# The recovery line for every failure but the two ``_recovery`` names (a fan-out
# that really moved files, and ``TrashMoveIncompleteError``). Spelled once,
# asserted whole at each of the six states that reach it; the sentence states no
# disk fact, which is what lets one stand in all six, so each of those tests
# carries its own disk asserts beside this equality.
_RETRY_BEFORE_EMPTY = (
    "A delete that stops part-way can leave some or all of the files in Trash."
    " Retry before emptying Trash: emptying now can destroy the only copy."
)


def _ghost_artist_library(tmp_path: Path, *, real_album: bool, bystander: bool) -> Library:
    """A library whose ``Ghosty`` albums are ROWS ONLY — their folders are gone.

    Built here rather than from ``duplicates_lib`` because the shape under test
    is a specific one: albums whose folders were removed outside MusicDrop while
    the share is perfectly fine.

    Two knobs, and each is what makes one scenario DETERMINISTIC rather than a
    coin flip on ``require_library_present``'s random sample:

    * ``real_album`` adds one Ghosty album that is really on disk and sorts
      FIRST (beets orders ``lib.albums()`` by ``albumartist+ album+``), so the
      fan-out moves it and then meets a library whose every remaining album is a
      ghost — the refusal is then certain, and it lands with progress already
      made;
    * ``bystander`` adds one album by another artist that is really on disk, so
      the refusal never happens at all: the sample size is 5 and this library
      holds 5 albums or fewer, which makes every draw exhaustive and therefore
      always a hit.

    An unrelated folder of real files sits in the music root either way. It is
    what makes "the share is mounted" a fact of the fixture rather than an
    assumption: the guard samples only albums the LIBRARY knows about, so this
    folder cannot make it pass, and any test can stat it at the end.
    """
    from beets.library import Item

    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def add(*, artist: str, album: str, on_disk: bool) -> None:
        base = music / artist / album
        base.mkdir(parents=True, exist_ok=True)
        f = base / "01 Track.mp3"
        f.write_bytes(b"\x00")
        item = Item(album=album, albumartist=artist, artist=artist, title="Track", track=1)
        item.path = os.fsencode(str(f))
        lib.add_album([item]).store()
        if not on_disk:
            shutil.rmtree(base)  # removed outside MusicDrop; the rows survive

    if real_album:
        # Sorts before every "Ghost N" under the same albumartist.
        add(artist=_GHOST_ARTIST, album="A Real Album", on_disk=True)
    for n in range(1, 5):
        add(artist=_GHOST_ARTIST, album=f"Ghost {n}", on_disk=False)
    if bystander:
        add(artist="Bystander", album="Still Here", on_disk=True)

    proof = music / "Not In The Library" / "sleeve.jpg"
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_bytes(b"\x00")
    return lib


def _artist_op_500(
    lib: Library, tmp_path: Path, trash: Path, artist: str = _GHOST_ARTIST
) -> HTTPException:
    """Run ``delete_artist_op`` for an artist and return the 500 it raises."""
    handle = make_test_handle(lib, beets_dir_for(tmp_path))

    class _App:
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_dir=str(trash), trash_origins_dir=str(origins_for(trash))),
        )

    class _Req:
        app = _App()

    req = _Req()
    op = delete_artist_op(req, artist)  # type: ignore[arg-type]  # stub req
    with pytest.raises(HTTPException) as ei:
        asyncio.run(op)
    assert ei.value.status_code == 500
    return ei.value


def test_delete_artist_partial_does_not_diagnose_an_unmounted_share(tmp_path: Path) -> None:
    """A refusal this fan-out CANNOT explain must be relayed, not diagnosed.

    ``require_library_present`` refuses when none of the albums it sampled is on
    disk, and its own message names two causes because it cannot tell them
    apart. This artist's folders were removed outside MusicDrop and the share is
    fine — provable here, since the fixture's unrelated folder is readable
    throughout — so "the music share became unavailable" was a false statement
    of fact printed to the one user who would act on it.
    """
    lib = _ghost_artist_library(tmp_path, real_album=True, bystander=False)
    trash = tmp_path / "trash"
    ghosty = [a.album for a in lib.albums() if a.albumartist == _GHOST_ARTIST]
    assert ghosty[0] == "A Real Album", "the fan-out must reach the real album first"
    assert len(ghosty) == 5

    detail = _artist_op_500(lib, tmp_path, trash).detail

    assert isinstance(detail, dict)
    message = detail["message"]
    assert "1 of 5" in message  # how far it got, unchanged
    assert "became unavailable" not in message  # the cause it was never told
    # ...and the words the error DID use, both of its causes intact.
    assert "Either the music share is not mounted, or those files have been" in message
    # The share really was mounted the whole time: this file never stopped being
    # readable, so a message blaming the mount would have been false, not unlucky.
    assert (tmp_path / "music" / "Not In The Library" / "sleeve.jpg").is_file()
    # One album's files really are in Trash, so that half of the promise stands.
    # The per-item layout: a container named for the album, the files re-filed
    # under it by beets' path template rather than as a copy of the source tree.
    assert list((trash / "Ghosty - A Real Album").rglob("*.mp3"))
    assert detail["recovery"] == "Files are recoverable in the Trash folder. Retry."


def test_delete_artist_partial_counts_moves_not_row_drops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ghost rows are dropped, not trashed — so the message may not say "moved".

    Every album here is a ghost, so the fan-out cleans rows and relocates
    nothing; the bystander is what makes the presence guard pass every time
    rather than at random. The old message offered "3 of 4 albums had been moved
    to Trash" and the 500 offered to find them there, for a Trash folder that
    does not exist.
    """
    lib = _ghost_artist_library(tmp_path, real_album=False, bystander=True)
    trash = tmp_path / "trash"
    real = delete_mod._trash_one
    calls = {"n": 0}

    def _fails_on_the_fourth(
        lib: Library,
        album: object,
        *,
        trash_dir: Path,
        origins_dir: Path,
        protected: ProtectedTrees,
    ) -> str:
        calls["n"] += 1
        if calls["n"] == 4:
            raise PermissionError(13, "Permission denied")
        return str(
            real(lib, album, trash_dir=trash_dir, origins_dir=origins_dir, protected=protected)
        )

    monkeypatch.setattr(delete_mod, "_trash_one", _fails_on_the_fourth)

    detail = _artist_op_500(lib, tmp_path, trash).detail

    assert isinstance(detail, dict)
    message = detail["message"]
    assert "3 of 4" in message  # three albums' rows are gone, and it says so
    assert "moved to Trash" not in message  # but not one file was
    assert "Permission denied" in message  # the real cause still names itself
    # The physical fact the wording has to match: the Trash folder holds
    # nothing, so a line telling the user to look in it points at nothing.
    assert list(trash.iterdir()) == []
    assert detail["recovery"] == _RETRY_BEFORE_EMPTY
    assert len([a for a in lib.albums() if a.albumartist == _GHOST_ARTIST]) == 1


def _two_album_artist_library(tmp_path: Path, *, first_on_disk: bool) -> Library:
    """``Twosome``'s two albums, the second of them always really on disk.

    Drives BOTH shapes of the partial message from one construction, since the
    clause under test is the same in both: with ``first_on_disk`` the fan-out
    really moves album one (``moved`` = 1, the "had been moved to Trash" shape);
    without it album one is a ghost row whose folder was removed outside
    MusicDrop (``moved`` = 0, the "no files left to move" shape).

    ``A First`` sorts before ``B Second`` under the same albumartist, so the
    fan-out's order is the fixture's order and the album it stops on is known. A
    bystander album on disk keeps ``require_library_present`` a certainty rather
    than a draw: three albums against a sample of five is an exhaustive draw.
    """
    from beets.library import Item

    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def add(*, artist: str, album: str, name: str, on_disk: bool) -> None:
        base = music / artist / album
        base.mkdir(parents=True, exist_ok=True)
        f = base / name
        f.write_bytes(b"\x00")
        item = Item(album=album, albumartist=artist, artist=artist, title="Track", track=1)
        item.path = os.fsencode(str(f))
        lib.add_album([item]).store()
        if not on_disk:
            shutil.rmtree(base)  # removed outside MusicDrop; the row survives

    add(artist="Twosome", album="A First", name="01 First.mp3", on_disk=first_on_disk)
    add(artist="Twosome", album="B Second", name="01 Second.mp3", on_disk=True)
    add(artist="Bystander", album="Still Here", name="01 t.mp3", on_disk=True)
    return lib


@pytest.mark.parametrize(
    ("first_on_disk", "expected"),
    [
        (True, "stopped after 1 of 2 albums had been moved to Trash"),
        (False, "stopped after dropping 1 of 2 albums that had no files left to move"),
    ],
)
def test_delete_artist_partial_speaks_only_of_the_albums_it_never_reached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_on_disk: bool, expected: str
) -> None:
    """Both partial messages ended "the rest are untouched". The rest includes this one.

    "The rest" takes in the album the fan-out stopped ON, and that album can be
    the most touched of all. Neither counter can see it — both count returns
    from the primitive — so the message says what it does know: the albums it
    never reached.

    What this test pins beside the wording is the state that makes "untouched"
    false, and owner ruling ``decisions.md`` 58 makes it the WORST of the two it
    could be: there is no move-back any more, so the album this stopped on has
    every one of its files inside the Trash container while the library still
    lists it. The album before it stays deleted with its files in Trash — beets
    commits on the way out of the transaction even while unwinding, so nothing
    already done can be taken back, and a message claiming otherwise would be the
    same falsehood in the other direction.

    The fault is a patched ``Album.remove`` on the SECOND album, not a plugin
    listener: a listener fires after beets deleted the album row
    (``beets/library/models.py:391`` before ``:394``), so it could not support
    the "still in the library" assert below. Both message shapes, because the
    clause was the same sentence in both and a fix to one of them is not a fix.
    """
    lib = _two_album_artist_library(tmp_path, first_on_disk=first_on_disk)
    trash = tmp_path / "trash"
    second = next(a for a in lib.albums() if a.album == "B Second")
    second_id = _require_id(second.id)
    second_root = Path(album_folder(lib, list(second.items())))
    first_id = _require_id(next(a for a in lib.albums() if a.album == "A First").id)
    calls = {"n": 0}
    real_remove = type(second).remove

    def _boom_on_the_second(self: Album, delete: bool = False, with_items: bool = True) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk I/O error, forced by the fixture")
        real_remove(self, delete, with_items)

    monkeypatch.setattr(type(second), "remove", _boom_on_the_second)

    detail = _artist_op_500(lib, tmp_path, trash, "Twosome").detail

    assert isinstance(detail, dict)
    message = detail["message"]
    assert expected in message  # how far it got, unchanged
    assert "the rest are untouched" not in message
    assert "the albums it never reached are untouched" in message
    # The album it stopped on, and why "untouched" would be a lie about it: its
    # file is under the Trash container, gone from its own folder, and the
    # library still lists the album.
    assert not (second_root / "01 Second.mp3").exists()
    # ``rglob("*.mp3")``, not the source file name: ``Album.move`` re-files
    # each item by PATH TEMPLATE under the container, so the name can change.
    assert list((trash / "Twosome - B Second").rglob("*.mp3"))
    assert lib.get_album(second_id) is not None
    # ...and the album BEFORE it is still gone, which nothing can change.
    assert lib.get_album(first_id) is None
    if first_on_disk:
        assert list((trash / "Twosome - A First").rglob("*.mp3"))
        assert "Trash" in detail["recovery"], "that one really is recoverable from there"


def test_delete_album_500_does_not_promise_trash_for_files_that_did_not_move(
    duplicates_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``TrashMoveIncompleteError`` is raised BECAUSE nothing moved.

    Its own message ends "The library rows were kept", and the 500 wrapping it
    answered "Files are recoverable in the Trash folder" — a contradiction
    inside one body, about files still sitting in the music library.
    """
    from app.beets.trash import TrashMoveIncompleteError

    def _refuses(*args: object, **kwargs: object) -> str:
        raise TrashMoveIncompleteError("'X' did not move to Trash. The library rows were kept.")

    monkeypatch.setattr(delete_mod, "_trash_one", _refuses)
    handle = make_test_handle(duplicates_lib, beets_dir_for(tmp_path))
    album_id = _require_id(next(iter(duplicates_lib.albums())).id)

    class _App:
        state = SimpleNamespace(beets_library=handle, settings=Settings())

    class _Req:
        app = _App()

    req = _Req()
    op = delete_album_op(req, album_id)  # type: ignore[arg-type]  # stub req
    with pytest.raises(HTTPException) as ei:
        asyncio.run(op)

    assert ei.value.status_code == 500
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert "recoverable in the Trash folder" not in detail["recovery"]
    assert detail["recovery"] == (
        "The files were not moved and the library still has the album. Retry."
    )


def test_delete_artist_500_on_the_FIRST_album_does_not_promise_trash(
    duplicates_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fan-out that fails on album 1 of 2 has moved nothing — and must say so.

    The tests above cover the fan-out that got PAST its first album: it raises
    ``ArtistDeletePartialError``, which carries the count the recovery line is
    chosen from. A failure on the FIRST album never reaches that helper —
    ``mutated == 0`` re-raises the cause bare so the 503 tier stays available —
    so the 500 wrapping it took the blanket "Files are recoverable in the Trash
    folder" while not one file had moved and Trash held nothing at all. The
    same wrong sentence as the ghost fan-out's, down the one path nothing
    enumerated.

    Pinned on the physical fact as well as the wording: the Trash dir — which
    ``_checked_store`` creates before any album is touched — holds nothing, and
    both albums are still in the library.
    """
    trash = tmp_path / "trash"
    handle = make_test_handle(duplicates_lib, beets_dir_for(tmp_path))

    def _fails_on_the_first(
        lib: Library,
        album: object,
        *,
        trash_dir: Path,
        origins_dir: Path,
        protected: ProtectedTrees,
    ) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(delete_mod, "_trash_one", _fails_on_the_first)

    class _App:
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_dir=str(trash), trash_origins_dir=str(origins_for(trash))),
        )

    class _Req:
        app = _App()

    before = [a for a in duplicates_lib.albums() if a.albumartist == "Radiohead"]
    assert len(before) == 2, "'the first album' only means something with more than one"

    req = _Req()
    op = delete_artist_op(req, "Radiohead")  # type: ignore[arg-type]  # stub req
    with pytest.raises(HTTPException) as ei:
        asyncio.run(op)

    assert ei.value.status_code == 500
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert "Permission denied" in detail["message"]  # the cause is still relayed
    assert "recoverable in the Trash folder" not in detail["recovery"]
    assert detail["recovery"] == _RETRY_BEFORE_EMPTY
    # The disk agrees with the sentence: the Trash folder holds nothing.
    assert list(trash.iterdir()) == []
    assert len([a for a in duplicates_lib.albums() if a.albumartist == "Radiohead"]) == 2


def _shared_folder_two_track_library(tmp_path: Path) -> Library:
    """An album of TWO tracks in a folder it shares, so a move can stop HALF done.

    ``trash_album`` moves item by item and beets stores each item's new path as
    it goes (``Album.move`` -> ``item.move(..., store=True)``,
    ``beets/library/models.py:489-495``), so a fault on the second item leaves
    the first under Trash with its row already pointing there and the album still
    in the library.

    * ``A Two`` and ``B Live`` are both ``Sharey``'s and share
      ``music/Sharey/Both``. That no longer decides which mover runs — there is
      only one — but it keeps the shape honest: a folder with a stranger's tracks
      in it is what the per-item move exists for, and it is what stops the source
      folder being pruned out from under the assertions below;
    * ``A Two`` sorts first under the same albumartist, so the artist fan-out
      meets it as its FIRST album — the tier where ``mutated == 0``;
    * a bystander album on disk keeps ``require_library_present`` a certainty
      rather than a draw: three albums against a sample of five is exhaustive.
    """
    from beets.library import Item

    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def add(*, artist: str, album: str, at: Path, names: list[str]) -> None:
        at.mkdir(parents=True, exist_ok=True)
        items = []
        for n, name in enumerate(names, 1):
            f = at / name
            f.write_bytes(b"\x00")
            item = Item(album=album, albumartist=artist, artist=artist, title=f"T{n}", track=n)
            item.path = os.fsencode(str(f))
            items.append(item)
        lib.add_album(items).store()

    both = music / "Sharey" / "Both"
    add(artist="Sharey", album="A Two", at=both, names=["01 A.mp3", "02 A.mp3"])
    add(artist="Sharey", album="B Live", at=both, names=["03 B.mp3"])
    add(
        artist="Bystander",
        album="Still Here",
        at=music / "Bystander" / "Album",
        names=["01 t.mp3"],
    )
    return lib


def test_delete_album_500_does_not_read_the_answer_out_of_a_half_moved_album(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Something IS in Trash here, and none of the old sentence's reading holds.

    The recovery line used to answer the user's question for them: "if the
    album's folder is there it can be restored from there; if it is not, nothing
    moved and there is nothing to restore". This is the state where the first
    half is as wrong as the second. What reached Trash is the CONTAINER named
    for the album, holding the one item that made it — not the album's folder;
    no origin record was written, because ``trash_album`` writes one only after
    the move; and the album is still in the library with that item's row
    pointing inside Trash.

    ``Item.move`` is patched rather than the primitive, so the half-moved state
    is built by the real mover: item one really is relocated and stored before
    item two raises.
    """
    from beets.library import Item
    from beets.util import MoveOperation

    lib = _shared_folder_two_track_library(tmp_path)
    trash = tmp_path / "trash"
    handle = make_test_handle(lib, beets_dir_for(tmp_path))
    album_id = _require_id(next(a for a in lib.albums() if a.album == "A Two").id)
    real_move = Item.move
    calls = {"n": 0}

    def _fails_on_the_second(
        item: Item,
        operation: MoveOperation = MoveOperation.MOVE,
        basedir: bytes | None = None,
        with_album: bool = True,
        store: bool = True,
    ) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise PermissionError(13, "Permission denied")
        real_move(item, operation, basedir=basedir, with_album=with_album, store=store)

    monkeypatch.setattr(Item, "move", _fails_on_the_second)

    class _App:
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_dir=str(trash), trash_origins_dir=str(origins_for(trash))),
        )

    class _Req:
        app = _App()

    req = _Req()
    op = delete_album_op(req, album_id)  # type: ignore[arg-type]  # stub req
    with pytest.raises(HTTPException) as ei:
        asyncio.run(op)

    assert ei.value.status_code == 500
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert "Permission denied" in detail["message"]  # the cause is relayed
    assert detail["recovery"] == _RETRY_BEFORE_EMPTY
    # The direction, beside the state below: the sentence may point AT Trash and
    # may not tell this user what finding something there means.
    assert "can be restored" not in detail["recovery"]
    assert "nothing to restore" not in detail["recovery"]
    # The state itself. One file made it, under the container rather than under
    # anything named like the album's folder; the row for it points into Trash;
    # the album never left the library; and nothing recorded where it came from.
    container = trash / "Sharey - A Two"
    assert len(list(container.rglob("*.mp3"))) == 1
    album = lib.get_album(album_id)
    assert album is not None  # the rows were kept
    rows = [os.fsdecode(i.path) for i in album.items()]
    assert [r for r in rows if r.startswith(f"{container}{os.sep}")] != []
    assert not list(origins_for(trash).glob("*.json"))
    # ...and the album that only ever shared the folder is untouched.
    assert (tmp_path / "music" / "Sharey" / "Both" / "03 B.mp3").is_file()


@pytest.mark.parametrize("stored", ["NULL", "X''"])
def test_an_album_with_a_null_path_row_refuses_before_anything_moves(
    tmp_path: Path, stored: str
) -> None:
    """A row with no usable ``path`` is refused by name, not relayed as Python text.

    Both spellings, because they are two different aliens. NULL needs a
    hand-edited DB; the EMPTY one beets writes itself for
    ``lib.add(Item(title=...))``, and a predicate reading ``is None`` let it
    through — measured: ``500 "Delete failed: [Errno 2] No such file or
    directory: \'\'"``, the container made, a temp file left and the good track
    already moved out of the library. Earlier, NULL alone reached the wire twice:
    the twin\'s ``PathQuery`` match raised ``AttributeError``, and with that
    guarded the prune walk raised ``TypeError``.

    Two things are pinned: the body is the app\'s sentences on both fields, and
    the state is untouched — rows kept, the good file where it was, Trash empty.
    """
    from beets.library import Item

    music = tmp_path / "music"
    folder = music / "Art" / "Alb"
    folder.mkdir(parents=True)
    good = folder / "01 T1.mp3"
    good.write_bytes(b"\x00")
    lib = build_library(str(beets_dir_for(tmp_path) / "library.db"), str(music))
    one = Item(album="Alb", albumartist="Art", artist="Art", title="T1", track=1)
    one.path = os.fsencode(str(good))
    two = Item(album="Alb", albumartist="Art", artist="Art", title="T2", track=2)
    two.path = os.fsencode(str(folder / "02 T2.mp3"))
    album = lib.add_album([one, two])
    album.store()
    album_id = _require_id(album.id)
    with lib.transaction() as tx:
        tx.mutate(f"UPDATE items SET path = {stored} WHERE track = 2", ())
    trash = tmp_path / "trash"
    handle = make_test_handle(lib, beets_dir_for(tmp_path))

    class _App:
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_dir=str(trash), trash_origins_dir=str(origins_for(trash))),
        )

    class _Req:
        app = _App()

    req = _Req()
    # Created outside the block: nothing runs until asyncio.run drives it.
    op = delete_album_op(req, album_id)  # type: ignore[arg-type]  # stub req
    with pytest.raises(HTTPException) as ei:
        asyncio.run(op)

    assert ei.value.status_code == 500
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert detail["message"] == (
        "Delete failed: A track of this album has no file path in the library."
    )
    assert detail["recovery"] == "Nothing was moved. Fix the row in beets, then retry."
    assert "NoneType" not in detail["message"], "no interpreter text on the wire"
    # Nothing moved, nothing dropped, and no container was made.
    assert lib.get_album(album_id) is not None
    assert good.is_file()
    assert sorted(p.name for p in trash.rglob("*")) == []


def _shared_folder_ghost_library(tmp_path: Path) -> Library:
    """Two albums of one artist in ONE folder, the first of them files-less.

    The shape that reaches ``trash_album``'s ghost arm — the only branch that
    hands ``_reached_trash`` a path in the MUSIC dir rather than at or under
    Trash:

    * ``A Ghost`` and ``B Live`` are both ``Sharey``'s and both live in
      ``music/Sharey/Both``, so ``B Live``'s file keeps the folder from being
      pruned once ``A Ghost``'s row is gone;
    * ``A Ghost``'s file is removed while ``B Live``'s stays, so the folder is
      still a directory (the missing-folder branch is skipped) and yet nothing
      of ``A Ghost``'s can move;
    * a bystander album on disk keeps ``require_library_present`` a certainty —
      three albums against a sample of five is an exhaustive draw, and two of
      them are really there.

    ``A Ghost`` sorts before ``B Live`` under the same albumartist, so the
    fan-out meets it first.
    """
    from beets.library import Item

    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def add(*, artist: str, album: str, at: Path, name: str) -> Path:
        at.mkdir(parents=True, exist_ok=True)
        f = at / name
        f.write_bytes(b"\x00")
        item = Item(album=album, albumartist=artist, artist=artist, title="Track", track=1)
        item.path = os.fsencode(str(f))
        lib.add_album([item]).store()
        return f

    both = music / "Sharey" / "Both"
    gone = add(artist="Sharey", album="A Ghost", at=both, name="01 Ghost.mp3")
    add(artist="Sharey", album="B Live", at=both, name="02 Live.mp3")
    add(artist="Bystander", album="Still Here", at=music / "Bystander" / "Album", name="01 t.mp3")
    gone.unlink()  # removed outside MusicDrop; the row and the shared folder survive
    return lib


def test_delete_artist_does_not_count_a_shared_folder_ghost_as_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path OUTSIDE Trash is not a Trash entry, however unlike ``trash_dir`` it looks.

    ``_reached_trash`` asks two things of the primitive's answer: that it is not
    ``trash_dir`` itself, and that it is under it. The first alone is what the
    no-item-rows arm needs — it returns ``str(trash_dir)``. This is the case only
    the second catches: ``trash_album``'s own ghost arm returns the album's
    folder in the MUSIC dir, which is neither ``trash_dir`` nor inside it.

    Counted as a move, the fan-out's 500 would say one album "had been moved to
    Trash" and send its user to a Trash folder holding nothing at all — the
    exact promise the two counters exist to keep apart.
    """
    lib = _shared_folder_ghost_library(tmp_path)
    trash = tmp_path / "trash"
    handle = make_test_handle(lib, beets_dir_for(tmp_path))
    real = delete_mod._trash_one
    calls = {"n": 0}

    def _fails_on_the_second(
        lib: Library,
        album: object,
        *,
        trash_dir: Path,
        origins_dir: Path,
        protected: ProtectedTrees,
    ) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            raise PermissionError(13, "Permission denied")
        return str(
            real(lib, album, trash_dir=trash_dir, origins_dir=origins_dir, protected=protected)
        )

    monkeypatch.setattr(delete_mod, "_trash_one", _fails_on_the_second)

    class _App:
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_dir=str(trash), trash_origins_dir=str(origins_for(trash))),
        )

    class _Req:
        app = _App()

    req = _Req()
    op = delete_artist_op(req, "Sharey")  # type: ignore[arg-type]  # stub req
    with pytest.raises(HTTPException) as ei:
        asyncio.run(op)

    assert ei.value.status_code == 500
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert "1 of 2" in detail["message"]  # one album's rows went, and it says so
    assert "moved to Trash" not in detail["message"]  # but nothing was moved
    assert detail["recovery"] == _RETRY_BEFORE_EMPTY
    # The premise of the whole test, asserted rather than assumed: the ghost went
    # through the SHARED-folder fallback, whose sibling is still sitting in the
    # folder it was never allowed to move wholesale — and Trash is empty.
    assert sorted(p.name for p in (tmp_path / "music" / "Sharey" / "Both").iterdir()) == [
        "02 Live.mp3"
    ]
    assert list(trash.iterdir()) == []
    assert [a.album for a in lib.albums() if a.albumartist == "Sharey"] == ["B Live"]


@pytest.mark.parametrize(
    ("path", "artist"),
    [("/api/albums/{album_id}", None), ("/api/artists", "Radiohead")],
)
def test_the_delete_routes_500_description_does_not_deny_its_own_body(
    duplicates_lib: Library,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    artist: str | None,
) -> None:
    """The schema's 500 may not promise what a real 500 body denies.

    Both routes DECLARED "its files are recoverable in the Trash folder" flatly,
    for a status whose structured body carries the recovery line that decides
    exactly that — so the generated client's types documented one answer while
    the wire sent the other. The tests above pin the body; this joins it to the
    contract, so the two cannot drift apart again.

    Two halves, and neither is evidence on its own: the LIVE spec's description
    (a wrong one passes every behavioural test in this file) against a REAL 500
    provoked with nothing moved (a wrong body passes any assertion about the
    spec). ``tests/test_openapi_spec_guard.py`` is not evidence about either —
    it only fires on a dump that was not regenerated.
    """
    from app.main import app

    def _refuses(*args: object, **kwargs: object) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(delete_mod, "_trash_one", _refuses)
    trash = tmp_path / "trash"
    handle = make_test_handle(duplicates_lib, beets_dir_for(tmp_path))

    class _App:
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_dir=str(trash), trash_origins_dir=str(origins_for(trash))),
        )

    class _Req:
        app = _App()

    op = (
        delete_artist_op(_Req(), artist)  # type: ignore[arg-type]  # stub req
        if artist is not None
        else delete_album_op(_Req(), _require_id(next(iter(duplicates_lib.albums())).id))  # type: ignore[arg-type]  # ditto
    )
    with pytest.raises(HTTPException) as ei:
        asyncio.run(op)
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert detail["recovery"] == _RETRY_BEFORE_EMPTY
    assert list(trash.iterdir()) == []  # nothing moved, and the body promises nothing

    description = app.openapi()["paths"][path]["delete"]["responses"]["500"]["description"]
    assert "recoverable in the Trash folder" not in description, (
        f"{path}'s 500 description promises Trash recovery for a status this route"
        f" answers with {detail['recovery']!r}"
    )
    # The negative alone is happy with a paraphrase that drops the condition —
    # "the body carries the cause and a recovery hint" passes it and tells the
    # reader of the contract nothing about WHEN Trash gets named. The condition
    # is the claim, so assert it directly.
    #
    # The verb is part of the claim. "points at the Trash folder only when" was
    # false against this very body: ``_RETRY_BEFORE_EMPTY`` names the Trash folder for
    # the moved-nothing case asserted above. What is conditional is the PROMISE,
    # not the mention — ``_recovery``'s docstring draws the line as promise vs
    # check — so both descriptions say "promises recovery from", and this asserts
    # the whole phrase rather than the bare condition.
    assert "promises recovery from the Trash folder only when" in description, (
        f"{path}'s 500 description must keep the condition on its Trash promise;"
        f" it reads {description!r}"
    )


@pytest.mark.parametrize(
    ("path", "artist", "promise"),
    [
        ("/api/albums/{album_id}", None, "The album is still in the library"),
        ("/api/artists", "Sharey", "None of the artist's albums has been dropped"),
    ],
)
def test_the_delete_routes_503_description_does_not_deny_a_share_that_dropped_mid_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    artist: str | None,
    promise: str,
) -> None:
    """Both 503s DECLARED "Nothing reached the Trash folder". This is a 503 that did.

    The guard is not only a pre-check: ``trash.py``'s post-condition re-checks
    the root BEFORE it asks whether anything landed, so a share that drops
    part-way through the per-item move raises ``LibraryRootUnavailableError``
    with items already under the Trash container — and both ops answer that with
    the 503, ahead of the blanket 500. The artist route's own extra clause ("a
    share that drops part-way through the fan-out is reported as the 500
    instead") is wrong for the same run: the fan-out has dropped nothing yet, so
    ``mutated == 0`` re-raises the cause bare and it lands here too.

    What every 503 does share is the library: no rows are gone. That is what the
    descriptions now claim, and this test holds both halves together — the LIVE
    spec against a REAL 503 with files under Trash and the album still in the
    library. Neither half is evidence on its own, and
    ``tests/test_openapi_spec_guard.py`` is evidence about neither: it only
    fires on a dump that was not regenerated.

    The share is dropped by the real mover, between the first item and the
    second, so nothing here decides where the raise comes from.
    """
    from beets.library import Item
    from beets.util import MoveOperation

    from app.main import app

    lib = _shared_folder_two_track_library(tmp_path)
    music = tmp_path / "music"
    trash = tmp_path / "trash"
    handle = make_test_handle(lib, beets_dir_for(tmp_path))
    album_id = _require_id(next(a for a in lib.albums() if a.album == "A Two").id)
    real_move = Item.move
    calls = {"n": 0}

    def _drops_the_share_after_the_first(
        item: Item,
        operation: MoveOperation = MoveOperation.MOVE,
        basedir: bytes | None = None,
        with_album: bool = True,
        store: bool = True,
    ) -> None:
        real_move(item, operation, basedir=basedir, with_album=with_album, store=store)
        calls["n"] += 1
        if calls["n"] == 1:
            shutil.rmtree(music)  # the share goes between item one and item two

    monkeypatch.setattr(Item, "move", _drops_the_share_after_the_first)

    class _App:
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_dir=str(trash), trash_origins_dir=str(origins_for(trash))),
        )

    class _Req:
        app = _App()

    op = (
        delete_artist_op(_Req(), artist)  # type: ignore[arg-type]  # stub req
        if artist is not None
        else delete_album_op(_Req(), album_id)  # type: ignore[arg-type]  # ditto
    )
    with pytest.raises(HTTPException) as ei:
        asyncio.run(op)

    assert ei.value.status_code == 503
    # The root guard's own flat sentence, so this is the arm the descriptions
    # below describe and not some other 503.
    assert ei.value.detail == "Library folder unavailable. Is the music share mounted?"
    # The state the old sentence denied: one item is under the Trash container.
    assert len(list((trash / "Sharey - A Two").rglob("*.mp3"))) == 1
    # ...and the state both descriptions may still promise: the rows are kept.
    assert lib.get_album(album_id) is not None

    description = app.openapi()["paths"][path]["delete"]["responses"]["503"]["description"]
    assert "Nothing reached the Trash folder" not in description, (
        f"{path}'s 503 description denies a Trash entry this same status leaves behind;"
        f" it reads {description!r}"
    )
    # The negative alone is happy with a description that says nothing about
    # either side. Both halves of the replacement are the claim, so assert them:
    # what is known (no rows are gone) and what is not (go and look).
    assert promise in description
    assert "check there before retrying" in description


def test_delete_op_503_when_the_library_looks_unmounted_before_the_trash_is_made(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The op's store check refuses to put a Trash on a bare mountpoint.

    Measured 2026-09-12 (security seat H-1): the creation ran ahead of every
    presence guard, so a destructive request on a dropped share left a directory
    in the music root that defeated the cheap mounted-check permanently, and the
    next disk sync dropped every row. The op reached
    ``require_library_root`` only AFTER this helper, so the refusal is new here.
    """
    music = Path(duplicates_lib.directory.decode())
    for child in music.iterdir():  # the share drops; the mountpoint stays
        shutil.rmtree(child)
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    req = _store_fault_req(
        duplicates_lib, tmp_path, music / "a" / ".trash", origins=tmp_path / "origins"
    )
    coro = delete_album_op(req, album_id)  # type: ignore[arg-type]  # duck-typed stub

    with pytest.raises(HTTPException) as ei:
        asyncio.run(coro)

    assert ei.value.status_code == 503
    detail = ei.value.detail
    assert isinstance(detail, str)
    assert "Is the music share mounted?" in detail
    assert detail.endswith("Nothing has been deleted.")
    assert list(music.iterdir()) == [], "nothing created on the bare mountpoint"
    assert duplicates_lib.get_album(album_id) is not None


@pytest.mark.skipif(os.getuid() == 0, reason="root writes a read-only directory anyway")
def test_delete_op_503_when_the_trash_dir_cannot_be_created(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The op's own store check CREATES the Trash, so its refusal is a 503 here.

    ``checked_protected_trees`` creates the directory one line before it takes
    the identity the mover compares (``store_layout._ensure_trash_root``); it
    used to be called outside this helper's ``except StoreLayoutError`` arm, so
    an uncreatable Trash answered 500 with no "nothing has been deleted".
    """
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    req = _store_fault_req(duplicates_lib, tmp_path, locked / "trash")
    # Created outside the block (nothing runs until asyncio.run drives it), so
    # the raises body holds the one call that can throw.
    coro = delete_album_op(req, album_id)  # type: ignore[arg-type]  # duck-typed stub

    try:
        with pytest.raises(HTTPException) as ei:
            asyncio.run(coro)
    finally:
        locked.chmod(0o700)

    assert ei.value.status_code == 503
    detail = ei.value.detail
    assert isinstance(detail, str)
    assert "could not be created" in detail
    assert detail.endswith("Nothing has been deleted.")
    assert duplicates_lib.get_album(album_id) is not None


# --- an album with NO item rows -----------------------------------------------


def _artist_with_an_itemless_album(tmp_path: Path) -> Library:
    """``Twosome``'s two albums, the first of them a row with no items at all.

    beets prunes an album when its last item goes, so this shape is reached only
    by removing the items WITHOUT the album (``with_album=False``) — or, in the
    field, by whatever left the row behind. ``A Empty`` sorts first under the
    same albumartist, so the fan-out meets it before the real one.
    """
    from beets.library import Item

    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def add(*, album: str, name: str) -> Album:
        base = music / "Twosome" / album
        base.mkdir(parents=True, exist_ok=True)
        f = base / name
        f.write_bytes(b"\x00")
        item = Item(album=album, albumartist="Twosome", artist="Twosome", title="Track", track=1)
        item.path = os.fsencode(str(f))
        added: Album = lib.add_album([item])
        return added

    empty = add(album="A Empty", name="01 Gone.mp3")
    add(album="B Real", name="01 Here.mp3")
    with lib.transaction():
        for item in list(empty.items()):
            item.remove(delete=False, with_album=False)
    reread = lib.get_album(_require_id(empty.id))
    assert reread is not None
    assert list(reread.items()) == []
    return lib


def test_an_album_with_no_item_rows_leaves_nothing_in_trash(tmp_path: Path) -> None:
    """The primitive would ``mkdir`` a container for it; the front door does not.

    Measured before the arm existed: an item-less album left an empty container
    (``trash entries: ['E - Empty'] | origins: []``) that the Trash page lists as
    a 0-track row with no origin, and whose path
    :func:`~app.beets.delete._reached_trash` reads as "moved". The arm now lives
    in ``trash.trash_album`` rather than in ``delete._trash_one``, so the
    primitive's other two callers get it too; it answers ``trash_dir`` itself.
    """
    lib = _artist_with_an_itemless_album(tmp_path)
    trash = tmp_path / "trash"
    empty_id = _require_id(next(a for a in lib.albums() if a.album == "A Empty").id)

    result = _delete(lib, empty_id, trash)

    assert result.trashed_albums == 1
    assert lib.get_album(empty_id) is None  # the row is gone
    assert result.trash_path == str(trash)
    assert not trash.exists(), "no container, so the Trash is not even created"


def test_the_fanout_does_not_count_an_itemless_album_as_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_reached_trash`` must read the no-item-rows answer as "not moved".

    Counted as a move, the fan-out's 500 would say one album "had been moved to
    Trash" and send its user to look for files that never left — the promise the
    two counters exist to keep apart. Pinned through the message rather than on
    the counter, because the message is what the user reads.
    """
    lib = _artist_with_an_itemless_album(tmp_path)
    trash = tmp_path / "trash"
    real = delete_mod._trash_one
    calls = {"n": 0}

    def _fails_on_the_second(
        lib: Library,
        album: object,
        *,
        trash_dir: Path,
        origins_dir: Path,
        protected: ProtectedTrees,
    ) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            raise PermissionError(13, "Permission denied")
        return str(
            real(lib, album, trash_dir=trash_dir, origins_dir=origins_dir, protected=protected)
        )

    monkeypatch.setattr(delete_mod, "_trash_one", _fails_on_the_second)

    detail = _artist_op_500(lib, tmp_path, trash, "Twosome").detail

    assert isinstance(detail, dict)
    message = detail["message"]
    assert "dropping 1 of 2 albums that had no files left to move" in message
    assert "moved to Trash" not in message
    assert detail["recovery"] == _RETRY_BEFORE_EMPTY
    assert list(trash.iterdir()) == []  # and the disk agrees: nothing reached it


# --- the retry after a row drop that raised -----------------------------------


@pytest.mark.parametrize("trash_inside_the_library", [False, True])
def test_a_retry_after_a_failed_row_drop_leaves_ONE_trash_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trash_inside_the_library: bool
) -> None:
    """Retry is what the recovery line tells the user to do, so it has to land.

    ``Album.remove`` raising leaves the files in the container and the album
    still listed — beets sends ``album_removed`` to plugins with no try/except,
    and there is no undo (owner ruling ``decisions.md`` 58). Measured before the
    retry arm, a second delete moved the files AGAIN: two entries, the origin
    that names the real folder on the empty husk, and the entry holding the files
    claiming it came from ``<trash>/Art - Alb/Art/Alb``.

    Both Trash layouts, because they differ on what is left behind: with the
    Trash INSIDE the library the husk was pruned away and its record orphaned in
    the store — a burnt name in the allocator that only ``empty_all``'s sweep
    clears.
    """
    from beets.library import Item

    music = tmp_path / "music"
    lib = build_library(str(beets_dir_for(tmp_path) / "library.db"), str(music))
    folder = music / "Art" / "Alb"
    folder.mkdir(parents=True)
    items = []
    for i in (1, 2):
        track = folder / f"{i:02d} T{i}.mp3"
        track.write_bytes(b"\x00")
        item = Item(album="Alb", albumartist="Art", artist="Art", title=f"T{i}", track=i)
        item.path = os.fsencode(str(track))
        items.append(item)
    album = lib.add_album(items)
    album.store()
    album_id = _require_id(album.id)

    trash = (music / "Trash") if trash_inside_the_library else (tmp_path / "trash")
    origins = origins_for(trash)

    real_remove = type(album).remove
    calls: list[int] = []

    def _raises_once(self: Album, delete: bool = False, with_items: bool = True) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("a plugin listener said no")
        real_remove(self, delete=delete, with_items=with_items)

    monkeypatch.setattr(type(album), "remove", _raises_once)

    with pytest.raises(RuntimeError, match="a plugin listener said no"):
        _delete(lib, album_id, trash)
    assert lib.get_album(album_id) is not None, "the album is still listed — the state to recover"

    result = _delete(lib, album_id, trash)

    assert lib.get_album(album_id) is None
    entries = sorted(p.name for p in trash.iterdir())
    assert entries == ["Art - Alb"], "one entry, not a second container"
    assert sorted(p.name for p in trash.rglob("*.mp3")) == ["01 T1.mp3", "02 T2.mp3"]
    records = sorted(p.name for p in origins.glob("*.json"))
    assert records == ["Art - Alb.json"]
    record = read_trash_origin(origins, "Art - Alb")
    assert record is not None
    assert record.origin == str(folder), "the origin names the album's real folder"
    assert not record.origin.startswith(str(trash)), "and never a path inside Trash"
    # Strictly INSIDE, not the root: the fan-out reads this answer to decide
    # whether the album counted as moved, and ``str(trash_dir)`` means "nothing
    # reached Trash" (``_reached_trash``).
    answer = Path(result.trash_path)
    assert answer.is_relative_to(trash), "the answer is inside Trash"
    assert answer != trash, "the answer is the entry, not the root"


def test_a_retry_after_a_part_way_move_finishes_the_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A MIXED album is not the retry arm's, and the retry moves what is left.

    A two-disc album whose second disc cannot be read: item 1 lands in Trash and
    item 2 raises, so the rows point at both ends. Measured — with the arm gated
    on ``any`` instead of ``all`` the retry dropped the rows and left item 2's
    file in the library, untracked, and 200 tests stayed green.

    The origin record is the other half. Taken over ALL rows, the retry's
    ``source_root`` is the commonpath of Trash and music — ``/`` on the shipped
    layout — which says nothing about where the album came from. Rows already
    inside Trash are skipped instead (``trash._album_root``'s ``not_in``), so the
    record names the folder the unmoved row is still sitting in. Gating the
    record on "inside ``directory:``" would have been the smaller change and is
    WRONG — an album stored outside the music folder is supported three ways and
    keeps its record (``test_an_album_outside_the_music_folder_still_reaches_trash``,
    which that gate failed).

    What is NOT asserted is a file count in Trash. beets' own move copies before
    it unlinks, so the failed first attempt leaves a stray copy of item 2 inside
    the first container — beets', not this module's, and the album is whole in
    the second container either way.
    """
    if os.geteuid() == 0:
        pytest.skip("root ignores the directory mode this fault needs")

    from beets.library import Item

    music = tmp_path / "music"
    lib = build_library(str(beets_dir_for(tmp_path) / "library.db"), str(music))
    root = music / "Art" / "Alb"
    discs = [root / "Disc 1", root / "Disc 2"]
    items = []
    for i, at in enumerate(discs, 1):
        at.mkdir(parents=True)
        track = at / f"{i:02d} T{i}.mp3"
        track.write_bytes(b"\x00")
        item = Item(album="Alb", albumartist="Art", artist="Art", title=f"T{i}", track=i, disc=i)
        item.path = os.fsencode(str(track))
        items.append(item)
    album = lib.add_album(items)
    album.store()
    album_id = _require_id(album.id)
    trash = tmp_path / "trash"
    origins = origins_for(trash)

    discs[1].chmod(0o555)  # item 2 cannot be moved out of it
    try:
        with pytest.raises(Exception, match=r"(?i)permission|denied|move"):
            _delete(lib, album_id, trash)
        rows = sorted(os.fsdecode(i.path) for i in _require_album(lib, album_id).items())
        assert any(r.startswith(str(trash)) for r in rows), "item 1 really moved"
        assert any(r.startswith(str(music)) for r in rows), "and item 2 really did not"
    finally:
        discs[1].chmod(0o755)

    _delete(lib, album_id, trash)

    assert lib.get_album(album_id) is None
    assert not list(music.rglob("*.mp3")), "nothing untracked stayed in the library"
    landed = {p.name for p in trash.rglob("*.mp3")}
    assert landed == {"01 T1.mp3", "02 T2.mp3"}, "both tracks are in Trash"
    origins_written = [read_trash_origin(origins, p.stem) for p in origins.glob("*.json")]
    written = sorted(r.origin for r in origins_written if r is not None)
    # One record, from the retry: the first attempt raised before recording. It
    # names the disc the unmoved row was still in — a real folder of the album's,
    # where the commonpath over all rows would have been the parent of Trash and
    # music. Display only, so "a folder it came from" is the whole bar.
    assert written == [str(discs[1])]


def test_a_trash_old_sibling_is_not_read_as_being_in_trash(tmp_path: Path) -> None:
    """The retry arm's prefix carries a trailing separator, and that is why.

    Rows under ``<trash>-old`` share every byte of ``<trash>`` up to its last
    character. Without the separator they read as "already in Trash", the arm
    drops the rows without moving anything, and the files sit in a sibling
    folder with nothing naming them. Measured: the whole file stayed green with
    the separator removed.
    """
    from beets.library import Item

    music = tmp_path / "music"
    music.mkdir()
    lib = build_library(str(beets_dir_for(tmp_path) / "library.db"), str(music))
    # Inside the music folder, which is a supported layout and the one where a
    # sibling is reachable at all: the library root guard wants files under it.
    trash = music / ".trash"
    sibling = music / ".trash-old"
    folder = sibling / "Art - Alb"
    folder.mkdir(parents=True)
    track = folder / "01 T1.mp3"
    track.write_bytes(b"\x00")
    item = Item(album="Alb", albumartist="Art", artist="Art", title="T1", track=1)
    item.path = os.fsencode(str(track))
    album = lib.add_album([item])
    album.store()

    result = _delete(lib, _require_id(album.id), trash)

    assert Path(result.trash_path).is_relative_to(trash), "it really moved into Trash"
    assert not track.exists(), "and left the sibling folder"
    assert [p.name for p in trash.rglob("*.mp3")] == ["01 T1.mp3"]


def test_a_symlink_inside_trash_does_not_take_the_retry_arm(tmp_path: Path) -> None:
    """The arm asks ``lstat``, like every other type test in this module. MEASURED.

    A row naming a symlink INSIDE Trash whose target is a live library file read
    as "already in Trash" under ``os.path.isfile``: the rows were dropped and the
    real file stayed in the library, untracked. Contrived, and nothing was
    destroyed — but the module's own posture (``sidecars.move_sidecars``) is that
    the link is not the file.
    """
    from beets.library import Item

    music = tmp_path / "music"
    folder = music / "Art" / "Alb"
    folder.mkdir(parents=True)
    lib = build_library(str(beets_dir_for(tmp_path) / "library.db"), str(music))
    real = folder / "01 T1.mp3"
    real.write_bytes(b"\x00")
    trash = tmp_path / "trash"
    (trash / "Art - Alb").mkdir(parents=True)
    link = trash / "Art - Alb" / "01 T1.mp3"
    link.symlink_to(real)
    item = Item(album="Alb", albumartist="Art", artist="Art", title="T1", track=1)
    item.path = os.fsencode(str(link))
    album = lib.add_album([item])
    album.store()
    album_id = _require_id(album.id)

    result = _delete(lib, album_id, trash)

    assert lib.get_album(album_id) is None
    # The arm did NOT fire, so the row was relocated into a container of its own
    # rather than being read as "already there" and dropped where it lay.
    assert not os.path.lexists(link), "the row's own entry was moved"
    assert Path(result.trash_path).is_relative_to(trash / "Art - Alb (1)")
    assert real.is_file(), "and the live library file it pointed at was never touched"


def _require_album(lib: Library, album_id: int) -> Album:
    album = lib.get_album(album_id)
    assert album is not None
    return album


def test_rows_inside_trash_with_their_files_gone_still_meet_the_root_guard(
    tmp_path: Path,
) -> None:
    """A ghost is not a retry, and only the files on disk tell them apart.

    The retry arm skips ``require_library_root`` deliberately: the files are in
    Trash, so whether the music share is mounted says nothing about dropping
    those rows. An album whose rows name Trash and whose files are NOT there is
    the other thing — and "every file is missing" is what an unmounted share
    looks like, which the guard's own message says it cannot distinguish from
    files removed outside MusicDrop. So it must reach the guard and refuse, with
    the rows kept.
    """
    from beets.library import Item

    music = tmp_path / "music"
    trash = music / "Trash"
    entry = trash / "Art - Alb"
    entry.mkdir(parents=True)
    lib = build_library(str(beets_dir_for(tmp_path) / "library.db"), str(music))
    track = entry / "01 T1.mp3"
    track.write_bytes(b"\x00")
    item = Item(album="Alb", albumartist="Art", artist="Art", title="T1", track=1)
    item.path = os.fsencode(str(track))
    album = lib.add_album([item])
    album.store()
    album_id = _require_id(album.id)
    shutil.rmtree(music)  # the share went, so every row's file is "missing"

    with pytest.raises(LibraryRootUnavailableError):
        _delete(lib, album_id, trash)

    assert lib.get_album(album_id) is not None, "nothing may be dropped on this path"


def test_a_null_path_row_beside_rows_already_in_trash_refuses_by_name(tmp_path: Path) -> None:
    """The one shape that REACHES the retry arm's ``it.path`` guard.

    ``all()`` short-circuits, so an album with a NULL row and any row outside
    Trash never asks about the NULL one. Here every other row IS in Trash, the
    arm walks all of them, and the guard is what stops beets' predicate being
    handed a ``None`` — unguarded it raised ``AttributeError: 'NoneType' object
    has no attribute 'startswith'`` out of the route. Guarded, the album takes
    the ordinary move and meets the named refusal there.
    """
    from beets.library import Item

    from app.beets.trash import TrashRowUnreadableError

    music = tmp_path / "music"
    trash = music / "Trash"
    entry = trash / "Art - Alb"
    entry.mkdir(parents=True)
    lib = build_library(str(beets_dir_for(tmp_path) / "library.db"), str(music))
    track = entry / "01 T1.mp3"
    track.write_bytes(b"\x00")
    one = Item(album="Alb", albumartist="Art", artist="Art", title="T1", track=1)
    one.path = os.fsencode(str(track))
    two = Item(album="Alb", albumartist="Art", artist="Art", title="T2", track=2)
    two.path = os.fsencode(str(entry / "02 T2.mp3"))
    album = lib.add_album([one, two])
    album.store()
    album_id = _require_id(album.id)
    with lib.transaction() as tx:
        tx.mutate("UPDATE items SET path = NULL WHERE track = 2", ())

    with pytest.raises(TrashRowUnreadableError):
        _delete(lib, album_id, trash)

    assert lib.get_album(album_id) is not None, "the rows are kept"
    assert track.is_file(), "and the file that was already in Trash stayed"


# --- the guards every row-dropping arm has to run first -----------------------


def test_every_arm_that_drops_a_row_asks_the_origin_store_first() -> None:
    """``_NOTHING_DELETED``'s promise, checked STRUCTURALLY rather than by outcome.

    The 503 arms append "Nothing has been deleted." to the origin-store refusal,
    and that is only true while no arm can drop a row ahead of the guard. Two
    functions reach a row drop, so both are read here: ``trash_album``'s body
    must OPEN with its two guards, ahead of its ``mkdir``, of its no-item-rows
    arm and of the ghost arm that drops rows having relocated nothing; and
    ``_trash_one``'s retry arm must ask before its own ``album.remove``.

    Structural because no outcome test can see the ORDER: adding a second guard
    to ``delete_artist`` left the whole suite green (measured), and a guard moved
    below the mutation it protects still refuses — just too late.
    """
    import ast
    import inspect
    import textwrap

    from app.beets import trash as trash_module

    def _body(func: object) -> list[ast.stmt]:
        """The function's statements, minus its docstring.

        Only the docstring: a bare ``require_library_root(lib)`` is an
        ``ast.Expr`` too, so dropping every ``Expr`` drops the very calls this
        test is about (measured — it read the guards as absent and passed the
        wrong assertion).
        """
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))  # type: ignore[arg-type]  # a function
        fn = tree.body[0]
        assert isinstance(fn, ast.FunctionDef)
        body = fn.body
        if ast.get_docstring(fn) is not None:
            body = body[1:]
        return body

    def _calls(node: ast.stmt) -> list[str]:
        return [
            c.func.id
            for c in ast.walk(node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        ]

    opening = _body(trash_module.trash_album)[:2]
    assert [_calls(n) for n in opening] == [["require_library_root"], ["require_usable_store"]]

    # ``trash_album``'s SECOND row-dropping arm, the item-less one, sits inside
    # the same body those two guards open — read as an arm that drops a row and
    # is not the last statement, so a move of it above them fails here.
    inner = _body(trash_module.trash_album)[2:]
    itemless = next(n for n in inner if isinstance(n, ast.If))
    assert any("remove" in ast.dump(n) for n in itemless.body), "the item-less arm drops a row"

    # The retry arm: the guard call must precede the remove INSIDE it.
    arm = next(n for n in _body(delete_mod._trash_one) if isinstance(n, ast.If))
    assert _calls(arm.body[0]) == ["require_usable_store"]
    assert any("remove" in ast.dump(n) for n in arm.body[1:]), "the arm must drop the row"


# --- the released case-insensitive loss ---------------------------------------


def test_delete_album_on_a_casefold_fs_leaves_no_dangling_rows(tmp_path: Path) -> None:
    """Two albums, ONE real folder, two spellings — the released loss. MEASURED.

    The released whole-folder mover's shared-folder check (``_folder_is_shared``,
    deleted with it) decided "shared" by byte-prefix over the row strings, so
    rows spelled ``Art/ALB/…`` did not match the root ``Art/Alb`` and one
    ``shutil.move`` took the single real directory with both albums' tracks in
    it (measured on released code: ``dangling_rows = 2``). Per-file Delete asks
    each item where it lives, which makes the loss unreachable rather than
    guarded.

    For ROWS. ``TWIN ART EXISTS`` below is a CHARACTERIZATION and not a pin on a
    fix: one cover file tracked by both twins travels with whichever album is
    deleted, because ``Album.move`` carries ``album.artpath`` and both rows name
    the same file. Measured the same for the released mover, so the per-file
    change neither closed it nor opened it.

    Needs a casefolding tmpfs inside a mount namespace, so it runs as a probe.
    SKIPPED where the box has neither; ``MUSICDROP_REQUIRE_CASEFOLD=1`` turns
    that skip into a failure, so a box that is supposed to have one cannot go
    quietly green.
    """
    from tests._mountns import run_probe, unshare_works

    required = os.environ.get("MUSICDROP_REQUIRE_CASEFOLD") == "1"
    if not unshare_works():
        if required:
            pytest.fail("MUSICDROP_REQUIRE_CASEFOLD=1 but this box has no mount namespace")
        pytest.skip("no unprivileged mount namespace on this box")

    work = tmp_path / "work"
    work.mkdir()
    lines = run_probe("casefold_delete", work)

    if "CASE-INSENSITIVE True" not in lines:
        if required:
            pytest.fail(f"MUSICDROP_REQUIRE_CASEFOLD=1 but no casefold tmpfs: {lines}")
        pytest.skip(f"no casefolding tmpfs on this box: {lines}")

    # The fixture really is the two-spellings-one-folder state, not a control.
    assert "SPELLINGS 2" in lines, lines
    assert "ONE REAL FOLDER True" in lines, lines
    # The loss itself: every row the library still has resolves to a real file.
    assert "DANGLING ROWS 0" in lines, lines
    assert "ROWS LEFT 2" in lines, lines
    assert "TWIN ALBUM LISTED True" in lines, lines
    assert "TARGET IN TRASH 2" in lines, lines
    # Characterization, see the docstring: the shared cover is really shared…
    assert "ONE REAL COVER True" in lines, lines
    # …and it went with the deleted twin, leaving the surviving row's artpath
    # naming a file that is not there.
    assert "TWIN ART EXISTS False" in lines, lines


# --- what Restore does with the sidecars Delete put in the container ----------


def test_restoring_a_deleted_album_strands_its_sidecars_in_trash(tmp_path: Path) -> None:
    """A MEASUREMENT of today's Restore, not a design decision. Reported.

    Delete now puts the ``.lrc`` beside its track inside the Trash container.
    Restore for a ``moved="items"`` entry is a re-IMPORT of that container, and
    beets imports audio: it moves the tracks into the library and leaves
    everything else where it was. So the lyrics do not come back with the album,
    and because ``restore_album`` deletes the record once it answers
    ``restored``, what is left in the listing is a row with no origin to point
    at. Both halves are asserted so a future change to either is visible here
    rather than in the field.

    ``config["threaded"] = False`` and a plugin-free library: this runs a real
    beets import, and the ``client`` fixture's ``musicbrainz`` plugin would
    spend the run waiting on network lookups.
    """
    import shutil as shutil_mod

    from beets import config
    from beets.library import Item

    from app.beets.trash_manage import list_trashed_albums, restore_album

    config["threaded"] = False  # reset by the autouse _clear_beets_globals fixture
    sample = Path(__file__).parent / "fixtures" / "silent.flac"
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def tagged(dst: Path, *, artist: str, album: str, title: str) -> Item:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil_mod.copyfile(sample, dst)
        item = Item(album=album, albumartist=artist, artist=artist, title=title, track=1)
        item.path = os.fsencode(str(dst))
        item.write()  # the tags must be ON DISK or the re-import files it under __/00
        return item

    # A bystander really on disk, so the restore does not run behind
    # require_library_present's dropped-share refusal.
    bystander = tagged(music / "By" / "Album" / "01 t.flac", artist="By", album="Album", title="T")
    lib.add_album([bystander]).store()
    target = tagged(
        music / "Solo" / "One" / "01 Song.flac", artist="Solo", album="One", title="Song"
    )
    album_id = _require_id(lib.add_album([target]).id)
    lib.get_album(album_id).store()  # type: ignore[union-attr]  # just added
    (music / "Solo" / "One" / "01 Song.lrc").write_text("[00:01.00] la", encoding="utf-8")

    trash = tmp_path / "trash"
    origins = origins_for(trash)
    result = _delete(lib, album_id, trash)
    landed = Path(result.trash_path)
    assert (landed / "01 Song.lrc").is_file(), "the premise: Delete carried the sidecar"
    (entry,) = list(trash.iterdir())

    answer = restore_album(
        lib,
        str(entry),
        trash_dir=trash,
        origins_dir=origins,
        protected=protected_for(lib, trash_dir=trash, origins_dir=origins),
    )

    assert answer.restored is True
    assert answer.reason == "restored"
    # The tracks come back, under the path template.
    assert list(music.rglob("01 Song.flac")), "the audio was re-imported"
    # The sidecar did NOT: it is still in the Trash container, and the row that
    # container leaves behind has lost the record that said where it came from.
    assert list(entry.rglob("*.lrc")) == [next(entry.rglob("01 Song.lrc"))]
    assert not list(music.rglob("*.lrc"))
    rows = [r for r in list_trashed_albums(trash, origins_dir=origins, music_dir=str(music))]
    (row,) = [r for r in rows if r.folder == entry.name]
    assert row.track_count == 0, "a 0-track row is what is left"
    assert row.origin is None, "and the record was consumed by the restore"
