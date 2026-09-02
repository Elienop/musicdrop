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

``tests/test_library_presence.py`` pins what the predicate ANSWERS; this file
pins the properties of HOW it asks that a caller can feel.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest
from beets.library import Item, Library

from app.beets.library import (
    _PRESENCE_SAMPLE_SIZE,
    LibraryRootUnavailableError,
    _sampled_library_dirs,
    require_library_present,
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
    """The folders the sampler would keep re-checking with ``ORDER BY RANDOM()`` gone.

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
    folders = [os.path.dirname(os.fsdecode(row[0])) for row in rows]
    assert len(folders) == _PRESENCE_SAMPLE_SIZE, folders
    return folders


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
    assert not any(os.path.isdir(folder) for folder in fixed), fixed

    require_library_present(lib)  # must not raise


def test_two_draws_from_the_same_library_are_not_the_same_sample(tmp_path: Path) -> None:
    """The mechanism behind that recovery: consecutive calls see different albums.

    Two identical draws are possible but not credible at this size (1 in 2.5
    billion, the bound asserted above), so a repeat here means the sample is
    fixed rather than re-rolled.
    """
    lib = _stale_library(tmp_path)

    first = _sampled_library_dirs(lib, _PRESENCE_SAMPLE_SIZE)
    second = _sampled_library_dirs(lib, _PRESENCE_SAMPLE_SIZE)

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
    assert not any(os.path.isdir(folder) for folder in fixed), fixed

    require_library_present(lib)  # must not raise


def test_two_draws_from_a_singleton_library_are_not_the_same_sample(tmp_path: Path) -> None:
    """The mechanism, on the fallback arm: consecutive calls see different tracks.

    Same bound as above — a repeat at this size means the fallback's sample is
    fixed rather than re-rolled.
    """
    lib = _singleton_library(tmp_path)

    first = _sampled_library_dirs(lib, _PRESENCE_SAMPLE_SIZE)
    second = _sampled_library_dirs(lib, _PRESENCE_SAMPLE_SIZE)

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

    assert _sampled_library_dirs(lib, _PRESENCE_SAMPLE_SIZE), (
        "the sampler must reach rows whose album_id points nowhere"
    )
    with pytest.raises(LibraryRootUnavailableError) as excinfo:
        require_library_present(lib)
    assert "holds none of the library's albums" in str(excinfo.value)


def test_a_mounted_dangling_album_id_library_is_still_accepted(tmp_path: Path) -> None:
    """And the other direction, so the fix is not "refuse whenever albums are gone".

    Byte-identical fixture with the folders present. A damaged ``albums`` table
    is not a dropped share and must not be reported as one — the sampler asks
    about the paths it has and the first hit accepts, exactly as for a library
    whose rows are all in order.
    """
    lib = _dangling_album_id_library(tmp_path, on_disk=True)

    require_library_present(lib)  # must not raise
