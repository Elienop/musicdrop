"""The Trash-name allocator against a folder name at the filesystem's own limit.

``_unique_trash_dest`` settles a collision by LENGTHENING the name — " (1)",
" (2)"… — which is the one thing a name already at ``NAME_MAX`` cannot survive,
and ``Path.exists()`` answers that with ``OSError(36)`` rather than ``False``
(``pathlib._IGNORED_ERRNOS`` is ENOENT/ENOTDIR/EBADF/ELOOP; errno 36 is not in
it). Most tests here are an END-TO-END delete through a real public mover rather
than a unit call on the allocator: what the defect cost was a 500 on the delete
route with nothing moved and no retry that could ever succeed, and a husk the
orphan sweep skipped in silence on every run.

ONE test here is a unit call and nothing else --
:func:`test_a_name_the_KERNEL_refuses_reads_as_free_and_not_as_a_500`, whose
input is a filesystem this suite does not mount (a ``NAME_MAX`` below our own
constant), so the errno is forced from the predicate. Everything else reaches the
allocator through a real mover, including the one that OPENS on unit calls:
:func:`test_fit_name_leaves_a_name_that_already_fits_alone` asserts the
arithmetic directly and then drives ``trash_folder`` twice, so an over-eager
shortener is caught on disk as well as in the function.

Two ways into the collision loop, and they are worth separating because only one
of them is new. An EXISTING Trash entry at the name is the old way; an ORPHANED
RECORD — the entry removed outside MusicDrop, its record kept, which the sibling
store's design accepts as litter — is the way this branch added, and it fires
where nothing is in the way at all.

And one route that never enters the loop at all: a Trash dir nested until
``<trash>/<255-byte name>`` is longer than PATH_MAX while every component is
still inside NAME_MAX. Nothing is in the way, the shortener has nothing to take
off, and the candidate cannot be looked up regardless — which is the fixture
that reaches both spellings of the occupancy test with no injection at all.
What it can separate them BY is narrow, and its own docstring says what was
measured.

The 255 here is a LITERAL on purpose. Deriving the inputs from ``_NAME_MAX``
would make these tests move with the constant and prove only that the module
agrees with itself; the constant is instead pinned in both directions once, in
:func:`test_the_name_bound_is_the_filesystems_and_not_just_ours`.
"""

from __future__ import annotations

import errno
import os
import traceback
from pathlib import Path

import pytest
from beets.library import Album, Item, Library

from app.beets import trash as trash_module
from app.beets.trash import (
    _fit_name,
    _trash_container_name,
    _unique_trash_dest,
    trash_album,
    trash_folder,
)
from app.beets.trash_manage import restore_album
from app.beets.trash_origins import _NAME_MAX, read_trash_origin, write_trash_origin
from app.reorganize_jobs.registry import ReorganizeRegistry
from app.reorganize_jobs.runner import _sweep_orphans
from tests.conftest import build_library, make_test_handle, origins_for, protected_for

#: A folder name exactly at NAME_MAX. " (1)" cannot be appended to it.
LONGEST = "H" * 255

#: FULLWIDTH LATIN CAPITAL LETTER A -- three UTF-8 bytes for one codepoint.
#: Spelled by codepoint rather than typed, the way ``test_plex_mapping`` does,
#: so the source stays unambiguous-unicode clean and the fixture cannot drift.
_FULLWIDTH_A = chr(0xFF21)


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    """The Trash dir and its SIBLING origin store, always built as the pair."""
    trash = tmp_path / "trash"
    return trash, origins_for(trash)


def _husk(tmp_path: Path, name: str) -> Path:
    """An audio-free art/booklet folder — the shape ``trash_folder`` moves."""
    husk = tmp_path / "music" / name
    husk.mkdir(parents=True)
    (husk / "cover.jpg").write_bytes(b"\x00")
    return husk


def _stub_album(lib: Library, *, artist: str, album: str, at: str) -> Album:
    """One album whose single file really sits at ``music/<at>/01 t.flac``.

    Stub bytes, not tagged audio: nothing here reads a tag, and every mover under
    test works off the item's stored PATH.
    """
    music = Path(os.fsdecode(lib.directory))
    dst = music / at / "01 t.flac"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(b"\x00")
    item = Item(album=album, albumartist=artist, artist=artist, title="T", track=1)
    item.path = os.fsencode(str(dst))
    added: Album = lib.add_album([item])
    added.store()
    return added


def _library_with_bystander(tmp_path: Path, *, folder: str) -> Library:
    """The album under test at ``music/<folder>``, plus one unrelated album.

    The bystander is not decoration. Trashing the only album on disk leaves the
    music root empty, which IS the dropped-share fixture — ``require_library_root``
    and ``require_library_present`` refuse it — so a one-album library would fail
    these deletes and restores for a reason no real library has.
    """
    lib = build_library(str(tmp_path / "library.db"), str(tmp_path / "music"))
    _stub_album(lib, artist="Portishead", album="Dummy", at=folder)
    _stub_album(lib, artist="Radiohead", album="Amnesiac", at="Radiohead/Amnesiac")
    return lib


def _dummy(lib: Library) -> Album:
    """The album under test, never the bystander."""
    album: Album = next(a for a in lib.albums() if a.album == "Dummy")
    return album


def test_the_name_bound_is_the_filesystems_and_not_just_ours(tmp_path: Path) -> None:
    """Pinned in BOTH directions, because either alone proves nothing.

    The literal catches the constant being lowered below what this app assumes,
    and the ``pathconf`` floor catches it being raised above what the filesystem
    running the suite will actually accept — the second is what turns the tests
    below from "255 is a number we chose" into "255 is the line the kernel
    draws". The same pair guards ``trash_origins._NAME_MAX``'s record-key budget.
    """
    assert _NAME_MAX == 255
    assert _NAME_MAX <= os.pathconf(str(tmp_path), "PC_NAME_MAX")
    # And the fixture really is AT the limit: one byte more is unwritable.
    (tmp_path / LONGEST).mkdir()
    with pytest.raises(OSError):
        (tmp_path / (LONGEST + "x")).mkdir()


def test_a_husk_at_the_name_limit_with_an_ORPHANED_RECORD_still_reaches_trash(
    tmp_path: Path,
) -> None:
    """The way in this branch added, and the one with nothing in the way.

    The Trash entry was removed outside MusicDrop (a file manager, an SMB
    client) and its record stayed behind. The allocator treats a recorded name as
    occupied — that is what stops a later album inheriting a stale origin — so it
    reaches for " (1)" although the Trash dir is empty, and at 255 bytes that
    used to raise ENAMETOOLONG straight out of the delete.

    The husk is the case with the most to lose: an audio-free folder cannot be
    re-imported, so the origin record is its ONLY exit from Trash.
    """
    trash, origins = _dirs(tmp_path)
    origins.mkdir(parents=True)
    write_trash_origin(origins, LONGEST, origin="/music/gone", moved="folder")
    husk = _husk(tmp_path, LONGEST)

    dest = trash_folder(
        husk,
        trash_dir=trash,
        origins_dir=origins,
        protected=protected_for(trash_dir=trash, origins_dir=origins),
    )

    assert dest.is_dir()
    assert (dest / "cover.jpg").is_file()
    assert not husk.exists(), "the delete must actually have moved something"
    assert len(os.fsencode(dest.name)) <= 255, "the entry name must fit the filesystem"
    assert dest.name != LONGEST, "the recorded name was occupied, so it must not be reused"
    # The record is written under the name actually USED, not the one asked for.
    record = read_trash_origin(origins, dest.name)
    assert record is not None
    assert record.origin == str(husk)
    # ...and the litter it stepped around is untouched, still naming its own origin.
    orphan = read_trash_origin(origins, LONGEST)
    assert orphan is not None
    assert orphan.origin == "/music/gone"


def test_a_husk_at_the_name_limit_moves_back_to_where_it_came_from(tmp_path: Path) -> None:
    """The point of surviving the allocator: the restore still lands exactly.

    A shortened entry name is only a container label; what a restore reads is the
    RECORD. So the folder goes back under its full 255-byte name even though the
    Trash entry it came out of was never called that.
    """
    lib = build_library(str(tmp_path / "library.db"), str(tmp_path / "music"))
    _stub_album(lib, artist="Bystander", album="Album", at="Bystander/Album")
    trash, origins = _dirs(tmp_path)
    origins.mkdir(parents=True)
    write_trash_origin(origins, LONGEST, origin="/music/gone", moved="folder")
    husk = _husk(tmp_path, LONGEST)
    dest = trash_folder(
        husk,
        trash_dir=trash,
        origins_dir=origins,
        protected=protected_for(trash_dir=trash, origins_dir=origins),
    )

    result = restore_album(
        lib, str(dest), trash_dir=trash, origins_dir=origins, protected=protected_for(lib)
    )

    assert result.restored is True
    assert (husk / "cover.jpg").is_file(), "the husk must be back at its full-length name"
    assert not dest.exists()
    assert read_trash_origin(origins, dest.name) is None, "the landed entry's record must go"


def test_a_folder_at_the_name_limit_moves_past_an_EXISTING_trash_entry(
    tmp_path: Path,
) -> None:
    """The other half of "taken": an ENTRY in the way, not an orphaned record.

    A Trash entry already holds the name — the ordinary "deleted this, changed my
    mind, deleted it again" shape — so the allocator must lengthen, and at 255
    bytes it could not. The test above covers the record half; this one is the
    filesystem half, and both go through the same ``while`` loop.

    Asked through ``trash_folder``: the whole-folder ALBUM mover this used to
    drive is gone, and ``trash_folder`` is the surviving caller that hands the
    allocator a FOLDER's own name (``delete_album`` builds its container name
    from tags, which ``test_a_container_name_built_from_TAGS...`` covers).
    """
    trash, origins = _dirs(tmp_path)
    (trash / LONGEST).mkdir(parents=True)
    (trash / LONGEST / "older.jpg").write_bytes(b"\x00")
    husk = _husk(tmp_path, LONGEST)

    dest = trash_folder(
        husk,
        trash_dir=trash,
        origins_dir=origins,
        protected=protected_for(trash_dir=trash, origins_dir=origins),
    )

    assert dest.is_dir()
    assert (dest / "cover.jpg").is_file()
    assert not husk.exists()
    assert len(os.fsencode(dest.name)) <= 255
    assert dest.name != LONGEST, "the entry in the way means the name must not be reused"
    assert (trash / LONGEST / "older.jpg").is_file(), "the entry in the way must be untouched"
    record = read_trash_origin(origins, dest.name)
    assert record is not None
    assert record.origin == str(husk)


def test_the_orphan_sweep_does_not_skip_a_husk_at_the_name_limit(tmp_path: Path) -> None:
    """The sweep's ``except OSError`` was written for one unmovable folder.

    ENAMETOOLONG out of the allocator landed in it, so the husk was skipped in
    silence — every run, forever, with nothing in the job's counters to say a
    folder had been passed over. Asserted through the registry's own count rather
    than the filesystem alone, because "swept 0" is exactly what the bug looked
    like from outside.
    """
    trash, origins = _dirs(tmp_path)
    origins.mkdir(parents=True)
    write_trash_origin(origins, LONGEST, origin="/music/gone", moved="folder")
    husk = _husk(tmp_path, LONGEST)
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")

    # A real handle: the sweep re-runs the containment check on the pair it was
    # handed, and that check reads the music root, the beets dir and the DB path
    # off the handle. ``beets`` is a sibling of ``music`` because the check
    # refuses a beets data dir that nests with the library.
    beets_dir = tmp_path / "beets"
    beets_dir.mkdir(parents=True, exist_ok=True)
    handle = make_test_handle(
        build_library(str(beets_dir / "library.db"), str(tmp_path / "music")), beets_dir
    )
    stopped = _sweep_orphans(
        reg,
        handle,
        scope="library",
        music_dir=tmp_path / "music",
        trash_dir=trash,
        trash_origins_dir=origins,
        vacated=[],
        ignore_dirs=(),
        protected_dirs=(),
    )

    assert stopped is False
    assert reg.state().orphans_trashed == 1, "the husk was skipped, not swept"
    assert not husk.exists()


def test_a_multibyte_name_is_shortened_on_a_CHARACTER_boundary(tmp_path: Path) -> None:
    """85 fullwidth characters are 255 bytes, and ``len()`` says 85.

    Two properties in one fixture. The budget is counted in BYTES, or a name
    ``len()`` calls short sails past the kernel's limit; and the cut lands
    between characters, or the entry would be named a string that is not the one
    keying its own record — ``os.fsdecode`` would carry the severed tail as lone
    surrogates and the name would no longer decode.
    """
    trash, origins = _dirs(tmp_path)
    origins.mkdir(parents=True)
    name = _FULLWIDTH_A * 85
    assert len(os.fsencode(name)) == 255
    write_trash_origin(origins, name, origin="/music/gone", moved="folder")
    husk = _husk(tmp_path, name)

    dest = trash_folder(
        husk,
        trash_dir=trash,
        origins_dir=origins,
        protected=protected_for(trash_dir=trash, origins_dir=origins),
    )

    assert len(os.fsencode(dest.name)) <= 255
    # Round-trips as strict UTF-8: no character was cut in half.
    assert os.fsencode(dest.name).decode("utf-8") == dest.name
    record = read_trash_origin(origins, dest.name)
    assert record is not None
    assert record.origin == str(husk)


def test_a_non_utf8_name_at_the_limit_keeps_its_bytes(tmp_path: Path) -> None:
    """An undecodable folder name is carried as lone surrogates, one per byte.

    So shortening by codepoints is still shortening by bytes here, and the bytes
    that survive are the ORIGINAL ones — a name the kernel gave us goes back to
    the kernel unchanged apart from its tail.
    """
    trash, origins = _dirs(tmp_path)
    origins.mkdir(parents=True)
    raw = b"\xff" * 255
    name = os.fsdecode(raw)
    write_trash_origin(origins, name, origin="/music/gone", moved="folder")
    husk = _husk(tmp_path, name)

    dest = trash_folder(
        husk,
        trash_dir=trash,
        origins_dir=origins,
        protected=protected_for(trash_dir=trash, origins_dir=origins),
    )

    encoded = os.fsencode(dest.name)
    assert len(encoded) <= 255
    assert encoded.startswith(b"\xff" * 251), "the surviving bytes must be the original ones"
    record = read_trash_origin(origins, dest.name)
    assert record is not None
    assert record.origin == str(husk)


def test_a_container_name_built_from_TAGS_can_be_over_the_limit_on_its_own(
    tmp_path: Path,
) -> None:
    """The one name here that no filesystem ever vetted.

    beets truncates a path component it generates to 200 bytes
    (``beets.util.MAX_FILENAME_LENGTH``), so a beets-named FOLDER never arrives
    at the allocator over the line. ``_trash_container_name`` does not truncate at
    all: it is ``"<albumartist> - <album>"`` straight off the album's tags, so the
    per-item mover can hand the allocator a name that is too long before any
    collision suffix exists — no orphaned record and no existing entry needed.
    """
    lib = _library_with_bystander(tmp_path, folder="Portishead/Dummy")
    album = _dummy(lib)
    album.albumartist = "A" * 300
    album.store()
    trash, origins = _dirs(tmp_path)
    assert len(os.fsencode(_trash_container_name(album))) > 255

    with lib.transaction():
        trash_album(lib, album, trash_dir=trash, origins_dir=origins)

    (container,) = list(trash.iterdir())
    assert len(os.fsencode(container.name)) <= 255
    record = read_trash_origin(origins, container.name)
    assert record is not None
    assert record.origin == str(tmp_path / "music" / "Portishead" / "Dummy")
    assert record.moved == "items"


def test_fit_name_leaves_a_name_that_already_fits_alone(tmp_path: Path) -> None:
    """The ordinary case, which every other test here would pass without.

    A shortener that trimmed a byte off every name would rename every Trash entry
    in the library and nothing above would notice — the assertions above are all
    "<= 255". This is the one that says the untouched case is untouched.

    AT the budget, not merely under it. A short name proves nothing about the
    arithmetic: ``name[: budget - 1]`` leaves an 18-character name whole and only
    shows itself where the slice can actually bite, so the 255-byte name is the
    input that separates "fits" from "fits after a byte was taken off it" — and
    end to end, with an empty store and an empty Trash, that byte would be the
    difference between the entry keeping its own name and silently being renamed
    on its way in.
    """
    assert _fit_name("Portishead - Dummy", 255) == "Portishead - Dummy"
    assert _fit_name(_FULLWIDTH_A * 2, 255) == _FULLWIDTH_A * 2
    assert _fit_name(LONGEST, 255) == LONGEST, "a name exactly at the budget is not shortened"
    trash, origins = _dirs(tmp_path)
    husk = _husk(tmp_path, "Portishead - Dummy")
    longest_husk = _husk(tmp_path, LONGEST)

    dest = trash_folder(
        husk,
        trash_dir=trash,
        origins_dir=origins,
        protected=protected_for(trash_dir=trash, origins_dir=origins),
    )
    longest_dest = trash_folder(
        longest_husk,
        trash_dir=trash,
        origins_dir=origins,
        protected=protected_for(trash_dir=trash, origins_dir=origins),
    )

    assert dest.name == "Portishead - Dummy"
    assert longest_dest.name == LONGEST, "nothing was in the way, so nothing may be trimmed"


def _trash_dir_over_path_max(tmp_path: Path) -> Path:
    """A Trash dir nested until ``<trash>/<LONGEST>`` is longer than PATH_MAX.

    Every component stays inside NAME_MAX and the Trash dir itself stays inside
    PATH_MAX, so every ``mkdir`` on the way succeeds: the only path in the
    fixture the kernel refuses is the candidate the allocator is about to test.
    Built by measuring against ``os.pathconf`` rather than against a literal,
    because what has to be true is that the KERNEL refuses it.
    """
    path_max = os.pathconf(str(tmp_path), "PC_PATH_MAX")
    deep = tmp_path
    while len(str(deep / "trash" / LONGEST)) <= path_max:
        deep = deep / ("d" * 200)
    deep.mkdir(parents=True)
    trash = deep / "trash"
    assert len(str(trash)) < path_max, "the Trash dir itself must be a legal path"
    assert len(str(trash / LONGEST)) > path_max
    return trash


def test_a_trash_path_over_PATH_MAX_fails_at_the_MOVE_and_not_at_the_allocator(
    tmp_path: Path,
) -> None:
    """The occupancy test's errno-36-absorbing half, reached with nothing injected.

    ``_NAME_MAX`` bounds one COMPONENT; PATH_MAX bounds the whole path — so a
    name the shortener rightly leaves alone is still unlookupable once the Trash
    dir is deep enough, and no suffix, no orphaned record and no entry in the way
    are needed to get there.

    TWO things separate the two spellings, and the FRAME is the one this test
    asserts on. Run twice from the same root, once with
    :func:`app.fsutil.exists` and once with ``dest.exists()``, the delete ends
    identically on everything a caller sees: ``OSError(36)``, the same
    ``.filename`` (the whole candidate path, in both), the same ``str(exc)``, a
    byte-identical 500 body, the husk still in ``/music`` and an empty Trash.
    What differs:

    * which frame the traceback blames — ``shutil.move`` in ``trash_folder``
      against the ``while`` in ``_unique_trash_dest``. That is what the
      assertions below read, because whoever reads the traceback is who the
      difference is for;
    * one log record, which this test does not read. Measured on this fixture
      (2026-09-02): the guarded run emits exactly one WARNING, from
      ``app.beets.trash_origins`` — ``origin_recorded``'s "could not tell" — and
      the unguarded run emits none. :mod:`app.fsutil` is not what talks; it has
      no logger at all. It is the ``or`` in ``_unique_trash_dest``: the guarded
      ``exists`` answers False instead of raising, so the second predicate runs
      and meets the same errno on the record's own path.
    """
    trash = _trash_dir_over_path_max(tmp_path)
    origins = origins_for(trash)
    husk = _husk(tmp_path, LONGEST)
    # The fixture really is at the hazard rather than merely deep: the unguarded
    # predicate raises on this candidate, which is what the guarded one absorbs.
    with pytest.raises(OSError) as raw:
        (trash / LONGEST).exists()
    assert raw.value.errno == errno.ENAMETOOLONG

    protected = protected_for(trash_dir=trash, origins_dir=origins)
    with pytest.raises(OSError) as caught:
        trash_folder(
            husk,
            trash_dir=trash,
            origins_dir=origins,
            protected=protected,
        )

    assert caught.value.errno == errno.ENAMETOOLONG
    ours = [
        frame
        for frame in traceback.extract_tb(caught.value.__traceback__)
        if frame.filename == trash_module.__file__
    ]
    assert ours, "the failure must pass through the module under test"
    last = ours[-1]
    where = f"the allocator must hand this failure to the move; it came out of {last.name}"
    assert last.name == "trash_folder", f"{where}:{last.lineno} instead"
    assert "shutil.move" in (last.line or ""), f"{where} at {last.line!r} instead"
    assert (husk / "cover.jpg").is_file(), "the delete must not have moved anything"
    assert list(trash.iterdir()) == [], "nothing may be left behind in Trash"


def test_a_name_the_KERNEL_refuses_reads_as_free_and_not_as_a_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The smaller-``NAME_MAX`` half, which this suite can only inject.

    ``_NAME_MAX`` is this app's guess at the limit, and it is wrong wherever the
    filesystem's own is smaller — eCryptfs stops at 143 bytes. There the
    occupancy test is handed a candidate the shortener already believes fits, and
    the kernel still answers ENAMETOOLONG, which ``Path.exists()`` raises rather
    than absorbs, straight out of the delete as a 500 with nothing moved.

    Injected rather than staged, because this suite mounts no filesystems: the
    errno is forced from the predicate itself, the way ``test_fsutil`` and
    eleven other modules force theirs. This pins the allocator's own boundary —
    it RETURNS a candidate instead of raising — and nothing past it; what the
    move then does on a filesystem whose ``NAME_MAX`` really is smaller is not
    staged anywhere here. The OTHER way the constant is wrong — a Trash path
    near PATH_MAX — needs no injection and is staged for real in
    :func:`test_a_trash_path_over_PATH_MAX_fails_at_the_MOVE_and_not_at_the_allocator`,
    which reaches :func:`app.fsutil.exists` without a monkeypatch and pins the
    frame it moves the failure to.
    """

    def boom(_self: Path) -> bool:
        raise OSError(errno.ENAMETOOLONG, "forced failure")

    monkeypatch.setattr(Path, "exists", boom)
    trash, origins = _dirs(tmp_path)

    dest = _unique_trash_dest(trash, origins, "Portishead - Dummy")

    assert dest == trash / "Portishead - Dummy"
