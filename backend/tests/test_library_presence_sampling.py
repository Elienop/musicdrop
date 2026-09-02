"""``require_library_present`` must RE-ROLL BOTH of its samples, never fix them.

The sampler has two queries. ``_SAMPLE_ALBUM_PATHS_SQL`` draws albums, and the
comment above it says why it draws them with ``ORDER BY RANDOM()``: a FIXED
sample makes a false refusal permanent — the same few long-deleted albums are
re-checked forever — while a re-rolled sample lets a healthy-but-stale library
recover on the next attempt. ``_SAMPLE_ITEM_PATHS_SQL`` is the fallback for a
library that groups nothing, and it needs the same property for the same reason:
a singleton-only library (``beet import -s``, or a folder of loose tracks) is
answered ENTIRELY by that second query.

Neither was pinned. Dropping ``ORDER BY RANDOM()`` from either one left the whole
suite green, and the libraries they break are not exotic: rows sit at whatever
rowids they were added at, and the oldest are exactly the ones most likely to
have been cleaned up by hand. With a plain ``LIMIT`` the sampler asks about those
same rows on every call, so a perfectly mounted share answers "holds none of the
library's albums" forever and the user cannot delete or restore a single row.

The fallback also decides which rows are asked about at all. It does not narrow
to ``album_id IS NULL``, so an item whose ``album_id`` points at a deleted album
row is sampled like any other instead of producing an empty sample that the
empty-sample arm waves through.

Two more properties of the same function, each unpinned until the tests at the
bottom of this file:

* Which query runs FIRST. The album query is the primary and the item query its
  fallback, and swapping them left the suite green — yet they are not
  interchangeable. The album arm returns one file per album (its ``MIN(path)``
  track); the item arm returns one row per TRACK, so on a multi-track library it
  asks about fewer real subjects than the sampler's own docstring claims, and it
  puts an ``ORDER BY RANDOM()`` scan of the big ``items`` table on every presence
  check (the module's comment on ``_SAMPLE_ALBUM_PATHS_SQL`` says avoiding
  exactly that scan is why the album query is the primary).
* The empty-path skip. ``if not raw`` weakened to ``if raw is None`` also left
  the suite green, and an empty-string ``path`` row then resolves to the library
  ROOT, spending one of the five slots on a path that is not a file and can
  never be one. It used to be worse than a wasted slot: while the sampler took
  ``os.path.dirname`` of each path the root was itself the subject, and a root
  exists whenever ``require_library_root`` has just passed, so one such row
  answered "the music is there" for a share that was gone.

What the sampler asks about is the FILE, never the folder holding it. Under
``dirname`` the answer depended on the path template's depth: a flat
``paths.default`` (``$title``) files every track in the music root, so every
sample collapsed onto the root and a dropped share with a stray entry on its
mountpoint was ACCEPTED. The last test in this file is that regression.

``tests/test_library_presence.py`` pins what the predicate ANSWERS; this file
pins the properties of HOW it asks that a caller can feel.
"""

from __future__ import annotations

import math
import os
import shutil
from pathlib import Path

import pytest
from beets.library import Item, Library

from app.beets.library import (
    _PRESENCE_SAMPLE_SIZE,
    LibraryRootUnavailableError,
    _sampled_library_files,
    require_library_present,
    require_library_root,
)
from tests.conftest import build_library

#: Total rows in each fixture library. Only the false-failure rate sets it:
#: exactly ``_PRESENCE_SAMPLE_SIZE`` of them are ghosts, so a call refuses only
#: when the uniform draw picks all of them, with probability
#: ``1 / C(_TOTAL_ROWS, _PRESENCE_SAMPLE_SIZE)`` — 1 in 2.5 billion here.
#:
#: The tests assert that bound rather than trusting this comment, and the
#: direction it guards is LOWERING ``_PRESENCE_SAMPLE_SIZE`` (or shrinking
#: ``_TOTAL_ROWS``), not raising it: ``1 / C(N, K)`` FALLS as K grows, so a
#: bigger sample only makes the all-ghost draw rarer. Measured against the
#: assert: K 5 -> 4 fails it, K 5 -> 8 passes. K raised past ``N / 2`` would turn
#: the curve back around, but N here is 40x K.
_TOTAL_ROWS = 200


def _stale_library(tmp_path: Path) -> Library:
    """A HEALTHY library whose lowest-rowid albums are all long gone from disk.

    The ghosts are added first, so they own the lowest rowids and are precisely
    what an unordered ``LIMIT`` returns. Their folders are never created rather
    than created and deleted: the DB row pointing at a folder that is not there
    is the whole state, and that is what "removed outside MusicDrop" leaves.

    Everything else really exists, so the share is plainly mounted — this is not
    a dropped-share fixture and ``require_library_present`` must accept it.
    """
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def add(*, artist: str, folder: Path, on_disk: bool) -> None:
        track = folder / "01 Track.mp3"
        if on_disk:
            folder.mkdir(parents=True)
            track.write_bytes(b"\x00")  # placeholder bytes; tests never read audio
        item = Item(album="Album", albumartist=artist, artist=artist, title="Track", track=1)
        item.path = os.fsencode(str(track))
        lib.add_album([item]).store()

    for i in range(_PRESENCE_SAMPLE_SIZE):
        add(artist=f"Ghost {i:03d}", folder=music / f"Ghost {i:03d}" / "Album", on_disk=False)
    for i in range(_TOTAL_ROWS - _PRESENCE_SAMPLE_SIZE):
        add(artist=f"Real {i:03d}", folder=music / f"Real {i:03d}" / "Album", on_disk=True)
    return lib


def _singleton_library(tmp_path: Path) -> Library:
    """The same stale library with no album rows at all — only loose tracks.

    What ``beet import -s`` leaves, and what a folder of loose files imports as.
    Every row has a NULL ``album_id``, so ``_SAMPLE_ALBUM_PATHS_SQL`` groups
    nothing and the fallback query answers this library on its own. The ghosts
    are added first for the same reason as above: they own the lowest rowids and
    are exactly what a bare ``LIMIT`` keeps re-drawing.
    """
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def add(*, name: str, on_disk: bool) -> None:
        folder = music / "Singles" / name
        track = folder / "01 Track.mp3"
        if on_disk:
            folder.mkdir(parents=True)
            track.write_bytes(b"\x00")  # placeholder bytes; tests never read audio
        item = Item(artist=name, title="Track", track=1)
        item.path = os.fsencode(str(track))
        lib.add(item)

    for i in range(_PRESENCE_SAMPLE_SIZE):
        add(name=f"Ghost {i:03d}", on_disk=False)
    for i in range(_TOTAL_ROWS - _PRESENCE_SAMPLE_SIZE):
        add(name=f"Real {i:03d}", on_disk=True)
    with lib.transaction() as tx:
        assert tx.query("SELECT COUNT(*) FROM albums")[0][0] == 0
    return lib


def _dangling_album_id_library(tmp_path: Path, *, on_disk: bool) -> Library:
    """Rows that name folders but belong to an album row that no longer exists.

    A half-wiped ``library.db``: the ``albums`` table lost its rows while
    ``items`` kept theirs. The album query grouped nothing (no album row to draw)
    and the old fallback's ``WHERE album_id IS NULL`` matched nothing either, so
    the sampler returned an empty list and the empty-sample arm accepted — for a
    library that names 200 folders and can say whether they are there.

    The music root always holds a ``.stfolder``, so ``require_library_root``
    passes and what is measured is the SAMPLER's answer, not the root check's.
    That stray entry on the local mountpoint is the exact hole
    ``require_library_present`` exists to cover.
    """
    music = tmp_path / "music"
    music.mkdir()
    (music / ".stfolder").write_bytes(b"")
    lib = build_library(str(tmp_path / "library.db"), str(music))

    for i in range(_TOTAL_ROWS):
        folder = music / f"Artist {i:03d}" / "Album"
        track = folder / "01 Track.mp3"
        if on_disk:
            folder.mkdir(parents=True)
            track.write_bytes(b"\x00")
        item = Item(album="Album", albumartist=f"Artist {i:03d}", title="Track", track=1)
        item.path = os.fsencode(str(track))
        lib.add(item)
    with lib.transaction() as tx:
        # A row id no album row has: the state left by the albums table going
        # while the items table stayed.
        tx.mutate("UPDATE items SET album_id = ?", (4242,))
        assert tx.query("SELECT COUNT(*) FROM albums")[0][0] == 0
        assert tx.query("SELECT COUNT(*) FROM items WHERE album_id IS NULL")[0][0] == 0
    return lib


def _what_an_unordered_limit_would_draw(lib: Library) -> list[str]:
    """The files the sampler would keep re-checking with ``ORDER BY RANDOM()`` gone.

    Spelled out in SQL rather than derived from the fixture's insertion order,
    so it measures what SQLite actually returns for a bare ``LIMIT`` instead of
    assuming rowid order. The album query is tried first, exactly as the sampler
    tries it, so one helper covers both fixtures: on a singleton-only library it
    groups nothing and falls through to the fallback's bare form.
    """
    with lib.transaction() as tx:
        rows = tx.query(
            "SELECT MIN(path) FROM items"
            " WHERE album_id IN (SELECT id FROM albums LIMIT ?)"
            " GROUP BY album_id",
            (_PRESENCE_SAMPLE_SIZE,),
        )
        if not rows:
            rows = tx.query("SELECT path FROM items LIMIT ?", (_PRESENCE_SAMPLE_SIZE,))
    files = [os.fsdecode(row[0]) for row in rows]
    assert len(files) == _PRESENCE_SAMPLE_SIZE, files
    return files


def test_a_stale_library_is_not_refused_by_its_oldest_ghosts(tmp_path: Path) -> None:
    """The invariant: a mounted library with old ghost rows stays deletable.

    With ``ORDER BY RANDOM()`` the draw reaches the albums that are really there
    and the first hit accepts. Without it the same five ghosts come back on
    every call and the user is locked out of their own library — permanently,
    since nothing about the library itself changes to un-stick it.

    The premise is asserted here rather than in a test of its own because the
    two are only meaningful together: if these ghosts were NOT the rows an
    unordered ``LIMIT`` returns, the call below would be green with the
    randomisation removed and would be pinning nothing at all.
    """
    lib = _stale_library(tmp_path)
    assert 1 / math.comb(_TOTAL_ROWS, _PRESENCE_SAMPLE_SIZE) < 1e-9, (
        "the fixture must be big enough that an all-ghost draw is not a real flake"
    )
    fixed = _what_an_unordered_limit_would_draw(lib)
    assert not any(os.path.isfile(path) for path in fixed), fixed

    require_library_present(lib)  # must not raise


def test_two_draws_from_the_same_library_are_not_the_same_sample(tmp_path: Path) -> None:
    """The mechanism behind that recovery: consecutive calls see different albums.

    Two identical draws are possible but not credible at this size (1 in 2.5
    billion, the bound asserted above), so a repeat here means the sample is
    fixed rather than re-rolled.
    """
    lib = _stale_library(tmp_path)

    first = _sampled_library_files(lib, _PRESENCE_SAMPLE_SIZE)
    second = _sampled_library_files(lib, _PRESENCE_SAMPLE_SIZE)

    assert len(first) == _PRESENCE_SAMPLE_SIZE
    assert set(first) != set(second)


def test_a_stale_singleton_library_is_not_refused_by_its_oldest_ghosts(tmp_path: Path) -> None:
    """The same invariant on the fallback query, which had a bare ``LIMIT``.

    A singleton-only library never reaches the album query, so the fixed sample
    lived on unnoticed there. Measured on this fixture before the fallback was
    re-rolled: refused 20 of 20 attempts, ONE distinct draw over 5 calls, and the
    message blamed the mount ("Is the music share mounted?") about a share
    holding 195 of the 200 folders. ``require_library_present`` gates the delete
    path and ``restore_album``'s entry, so that user could neither delete nor
    restore a single row, permanently.
    """
    lib = _singleton_library(tmp_path)
    assert 1 / math.comb(_TOTAL_ROWS, _PRESENCE_SAMPLE_SIZE) < 1e-9, (
        "the fixture must be big enough that an all-ghost draw is not a real flake"
    )
    fixed = _what_an_unordered_limit_would_draw(lib)
    assert not any(os.path.isfile(path) for path in fixed), fixed

    require_library_present(lib)  # must not raise


def test_two_draws_from_a_singleton_library_are_not_the_same_sample(tmp_path: Path) -> None:
    """The mechanism, on the fallback arm: consecutive calls see different tracks.

    Same bound as above — a repeat at this size means the fallback's sample is
    fixed rather than re-rolled.
    """
    lib = _singleton_library(tmp_path)

    first = _sampled_library_files(lib, _PRESENCE_SAMPLE_SIZE)
    second = _sampled_library_files(lib, _PRESENCE_SAMPLE_SIZE)

    assert len(first) == _PRESENCE_SAMPLE_SIZE
    assert set(first) != set(second)


def test_a_dangling_album_id_is_sampled_rather_than_waved_through(tmp_path: Path) -> None:
    """A half-wiped DB on a dropped share must not answer "the music is there".

    The library names 200 folders and none of them exist, which is what a dropped
    share looks like — but the mountpoint still holds a ``.stfolder``, so the
    root check passes and only the sample can catch it. While the fallback asked
    for ``album_id IS NULL`` these rows matched neither query, the sample came
    back empty, and the empty-sample arm accepted: the caller then dropped rows
    for a library whose files are all still on the unmounted share.
    """
    lib = _dangling_album_id_library(tmp_path, on_disk=False)

    assert _sampled_library_files(lib, _PRESENCE_SAMPLE_SIZE), (
        "the sampler must reach rows whose album_id points nowhere"
    )
    with pytest.raises(LibraryRootUnavailableError) as excinfo:
        require_library_present(lib)
    assert "none of the music files the library names" in str(excinfo.value)


def test_a_mounted_dangling_album_id_library_is_still_accepted(tmp_path: Path) -> None:
    """And the other direction, so the fix is not "refuse whenever albums are gone".

    Byte-identical fixture with the folders present. A damaged ``albums`` table
    is not a dropped share and must not be reported as one — the sampler asks
    about the paths it has and the first hit accepts, exactly as for a library
    whose rows are all in order.
    """
    lib = _dangling_album_id_library(tmp_path, on_disk=True)

    require_library_present(lib)  # must not raise


#: Albums in the two fixtures below, and tracks in each of them.
#:
#: Deliberately FEWER albums than ``_PRESENCE_SAMPLE_SIZE`` and MORE items than
#: it, which is what makes both fixtures deterministic and what tells the two
#: query arms apart:
#:
#: * fewer albums than the ``LIMIT`` means the album draw is exhaustive, so
#:   ``ORDER BY RANDOM()`` cannot change WHICH albums come back. There is no
#:   false-failure probability to bound here the way ``_TOTAL_ROWS`` bounds one
#:   for the re-roll tests above — measured over 200 consecutive draws, the
#:   album arm returned the same 3 folders every time;
#: * more items than the ``LIMIT`` means the item arm always fills its 5 rows.
#:   Measured over the same 200 draws with the arms swapped: 5 rows naming 2 or
#:   3 distinct folders, never 3 rows. That gap is the assertion.
_GROUPED_ALBUMS = _PRESENCE_SAMPLE_SIZE - 2
_TRACKS_PER_ALBUM = 4


def _multi_track_library(tmp_path: Path) -> tuple[Library, list[str]]:
    """A healthy library of a few multi-track albums, and the file per album the
    sampler must name.

    Every file is on disk, so this is not a dropped-share fixture and the
    presence check accepts it; what is measured is the SHAPE of the sample, not
    the answer. The expected file is each album's ``MIN(path)`` — the lowest
    path, which the ``NN Track.mp3`` naming makes the first track.
    """
    assert _GROUPED_ALBUMS >= 1, "the album arm needs at least one album to group"
    assert _GROUPED_ALBUMS < _PRESENCE_SAMPLE_SIZE, "the album draw must be exhaustive"
    assert _GROUPED_ALBUMS * _TRACKS_PER_ALBUM > _PRESENCE_SAMPLE_SIZE, (
        "the item arm must be able to fill its LIMIT, or the two arms agree by accident"
    )
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    lowest_per_album: list[str] = []
    for a in range(_GROUPED_ALBUMS):
        folder = music / f"Artist {a:03d}" / "Album"
        folder.mkdir(parents=True)
        lowest_per_album.append(str(folder / "01 Track.mp3"))
        items = []
        for t in range(_TRACKS_PER_ALBUM):
            track = folder / f"{t + 1:02d} Track.mp3"
            track.write_bytes(b"\x00")  # placeholder bytes; tests never read audio
            item = Item(
                album="Album",
                albumartist=f"Artist {a:03d}",
                artist=f"Artist {a:03d}",
                title=f"Track {t + 1}",
                track=t + 1,
            )
            item.path = os.fsencode(str(track))
            items.append(item)
        lib.add_album(items).store()
    return lib, lowest_per_album


def _library_with_an_empty_path_row(tmp_path: Path) -> tuple[Library, Path]:
    """A dropped share whose DB holds one row with an EMPTY ``path``.

    Only an external ``UPDATE`` writes that (beets always stores the file it
    imported), which is why it is built with one — the comment on the
    empty-sample arm of ``require_library_present`` names the same cause.

    None of the album folders exist: that IS the state this fixture is about, a
    share that went away while ``library.db`` stayed. The mountpoint keeps a
    ``.stfolder`` so ``require_library_root`` passes and the sampler is the only
    thing that can catch it — the exact hole ``require_library_present`` exists
    to cover. Fewer albums than ``_PRESENCE_SAMPLE_SIZE``, so the draw is
    exhaustive and the empty row is in EVERY sample; nothing here is a coin flip.
    """
    music = tmp_path / "music"
    music.mkdir()
    (music / ".stfolder").write_bytes(b"")
    lib = build_library(str(tmp_path / "library.db"), str(music))

    for a in range(_GROUPED_ALBUMS):
        item = Item(
            album="Album",
            albumartist=f"Artist {a:03d}",
            artist=f"Artist {a:03d}",
            title="Track",
            track=1,
        )
        item.path = os.fsencode(str(music / f"Artist {a:03d}" / "Album" / "01 Track.mp3"))
        lib.add_album([item]).store()
    with lib.transaction() as tx:
        lowest = tx.query("SELECT id FROM items ORDER BY id LIMIT 1")[0][0]
        tx.mutate("UPDATE items SET path = '' WHERE id = ?", (lowest,))
        stored = tx.query("SELECT path FROM items WHERE id = ?", (lowest,))[0][0]
    assert stored == "", repr(stored)  # SQLite is dynamically typed: a str, not b""
    return lib, music


def test_the_sampler_asks_one_file_per_album_and_not_one_per_track(tmp_path: Path) -> None:
    """The album query is the PRIMARY; the item query is only its fallback.

    Swapping them is not an equivalent change. This library groups into
    ``_GROUPED_ALBUMS`` albums of ``_TRACKS_PER_ALBUM`` tracks each, so the
    shipped order returns exactly one file per album — the cardinality
    ``_sampled_library_files``' own docstring promises ("one per album where the
    library groups into albums ... one per item on the fallback arm"). With the
    arms swapped the item query never comes back empty, so it answers every
    presence check: ``_PRESENCE_SAMPLE_SIZE`` rows drawn from the tracks, more
    rows than this library has albums and 2 or 3 real subjects behind them — and
    an unindexed ``ORDER BY RANDOM()`` over the big ``items`` table on a healthy
    library that the album query answers from the small one.

    Asserting the exact paths, not just their count, is what also pins WHICH
    file stands for an album: its ``MIN(path)``, so the sampled slot is stable
    across draws rather than a different track each time.
    """
    lib, lowest_per_album = _multi_track_library(tmp_path)
    with lib.transaction() as tx:
        assert tx.query("SELECT COUNT(*) FROM albums")[0][0] == _GROUPED_ALBUMS
        assert tx.query("SELECT COUNT(*) FROM items")[0][0] > _PRESENCE_SAMPLE_SIZE

    files = _sampled_library_files(lib, _PRESENCE_SAMPLE_SIZE)

    assert sorted(files) == sorted(lowest_per_album)
    require_library_present(lib)  # must not raise: every file is on disk


def test_an_empty_path_row_is_skipped_and_not_read_as_the_library_root(tmp_path: Path) -> None:
    """A row naming no file must not spend a sample slot, or stand for the root.

    ``os.path.join(lib.directory, "")`` is the library root with a trailing
    separator, so an empty ``path`` that reaches the loop's body puts the root
    itself into the sample. Under the file check that costs a slot rather than
    the answer (the root is a directory, so ``os.path.isfile`` says no) — but it
    was the answer while the sampler took ``os.path.dirname``: the root is what
    ``require_library_root`` has just confirmed exists, so the first-hit accept
    reported "the music is there" for a library whose every album folder was
    gone. Measured then: weakening the skip to ``raw is None`` put the root in
    50 of 50 samples and accepted a dropped share.

    Both halves are asserted because either alone is weak — the count catches
    the wasted slot, and ``normpath`` is what makes "the root is not in the
    sample" catch it too (the joined form carries a trailing separator, so a
    bare string comparison passes vacuously).
    """
    lib, music = _library_with_an_empty_path_row(tmp_path)

    files = _sampled_library_files(lib, _PRESENCE_SAMPLE_SIZE)

    assert not [f for f in files if os.path.normpath(f) == str(music)]
    assert len(files) == _GROUPED_ALBUMS - 1  # the empty row contributes nothing
    with pytest.raises(LibraryRootUnavailableError) as excinfo:
        require_library_present(lib)
    assert "none of the music files the library names" in str(excinfo.value)


#: Albums in the flat-layout fixture below. More than ``_PRESENCE_SAMPLE_SIZE``
#: so the draw is a real sample rather than an exhaustive one — the refusal must
#: come from every drawn file being absent, not from the draw being forced.
_FLAT_ALBUMS = _PRESENCE_SAMPLE_SIZE + 3


def _flat_library(tmp_path: Path) -> tuple[Library, Path]:
    """A library whose ``paths.default`` has NO directory component.

    ``$title`` is beets' own shortest template and the field is editable from
    the app (Settings -> Naming writes ``paths:`` back into ``config.yaml``), so
    this is a supported layout, not a damaged install. Each file is placed where
    beets' OWN ``Item.destination()`` puts it, and the assert below reads that
    destination — so "every track lands in the music root" is a measurement of
    the template rather than an assumption about it, and it is made HERE rather
    than from the sampler's own output, which would only re-state whatever the
    sampler currently returns.
    """
    music = tmp_path / "music"
    music.mkdir()
    lib = build_library(str(tmp_path / "library.db"), str(music), path_format="$title")

    for a in range(_FLAT_ALBUMS):
        item = Item(
            album=f"Album {a:03d}",
            albumartist=f"Artist {a:03d}",
            artist=f"Artist {a:03d}",
            title=f"Song {a:03d}",
            track=1,
        )
        item.path = os.fsencode(str(music / f"provisional-{a:03d}.mp3"))
        lib.add_album([item]).store()
        dest = Path(os.fsdecode(item.destination()))
        assert dest.parent == music, f"the template must be flat, got {dest}"
        dest.write_bytes(b"\x00")  # placeholder bytes; tests never read audio
        item.path = os.fsencode(str(dest))
        item.store()
    return lib, music


def test_a_flat_layout_does_not_sample_the_music_root_against_itself(tmp_path: Path) -> None:
    """A dropped share must be refused whatever depth the template files a track at.

    The hole this closes: with the sampler taking ``os.path.dirname``, a flat
    library's every sample WAS the music root, so ``require_library_present``
    re-asked the question ``require_library_root`` had just answered — and a
    stray ``.stfolder`` on the local mountpoint answers it wrongly. Measured on
    the shipped folder check: the root came back 5 of 5 times and the predicate
    ACCEPTED, while the same rows under ``$albumartist/$album/$title`` were
    refused.

    The premise — that this template really does file every track in the music
    root — is asserted inside the fixture, from beets' own ``Item.destination()``
    rather than from the sampler's output: without it the refusal below could be
    coming from something else entirely and would pin nothing about flat layouts,
    and reading it back off the sampler would only re-state whatever the sampler
    happens to return.
    """
    lib, music = _flat_library(tmp_path)

    require_library_present(lib)  # healthy: the sampled files are all there

    shutil.rmtree(music)
    music.mkdir()
    (music / ".stfolder").mkdir()  # the stray entry the cheap predicate accepts

    require_library_root(lib)  # accepts the stray-entry mountpoint — the gap

    with pytest.raises(LibraryRootUnavailableError) as excinfo:
        require_library_present(lib)
    assert "none of the music files the library names" in str(excinfo.value)

    # And the reason it refused: the sample is the FILES, so nothing in it is
    # the music root. Asserted after the behaviour on purpose — restoring
    # ``dirname`` must fail the raise above, not merely this shape check.
    sample = _sampled_library_files(lib, _PRESENCE_SAMPLE_SIZE)
    assert len(sample) == _PRESENCE_SAMPLE_SIZE, sample
    assert not [p for p in sample if os.path.normpath(p) == str(music)], sample
