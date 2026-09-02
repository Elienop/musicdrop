"""``require_library_present`` must RE-ROLL its sample, never fix it.

``_SAMPLE_ALBUM_PATHS_SQL`` draws its albums with ``ORDER BY RANDOM()``, and the
comment above it says why: a FIXED sample makes a false refusal permanent — the
same few long-deleted albums are re-checked forever — while a re-rolled sample
lets a healthy-but-stale library recover on the next attempt.

Nothing pinned that. Dropping ``ORDER BY RANDOM()`` leaves the whole suite
green, and the library it breaks is not exotic: albums removed outside MusicDrop
sit at whatever rowids they were added at, and the oldest rows are exactly the
ones most likely to have been cleaned up by hand. With a plain ``LIMIT`` the
sampler asks about those same rows on every call, so a perfectly mounted share
answers "holds none of the library's albums" forever and the user cannot delete
a single row through the app.

``tests/test_library_presence.py`` pins what the predicate ANSWERS; this file
pins the one property of HOW it asks that a caller can feel.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

from beets.library import Item, Library

from app.beets.library import (
    _PRESENCE_SAMPLE_SIZE,
    _sampled_library_dirs,
    require_library_present,
)
from tests.conftest import build_library

#: Total albums in the stale-library fixture. Only the false-failure rate sets
#: it: exactly ``_PRESENCE_SAMPLE_SIZE`` of these albums are ghosts, so a call
#: refuses only when the uniform draw picks all of them, with probability
#: ``1 / C(_TOTAL_ALBUMS, _PRESENCE_SAMPLE_SIZE)`` — 1 in 2.5 billion here.
#:
#: The test asserts that bound rather than trusting this comment, and the
#: direction it guards is LOWERING ``_PRESENCE_SAMPLE_SIZE`` (or shrinking
#: ``_TOTAL_ALBUMS``), not raising it: ``1 / C(N, K)`` FALLS as K grows, so a
#: bigger sample only makes the all-ghost draw rarer. Measured against the
#: assert: K 5 -> 4 fails it, K 5 -> 8 passes. K raised past ``N / 2`` would turn
#: the curve back around, but N here is 40x K.
_TOTAL_ALBUMS = 200


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
    for i in range(_TOTAL_ALBUMS - _PRESENCE_SAMPLE_SIZE):
        add(artist=f"Real {i:03d}", folder=music / f"Real {i:03d}" / "Album", on_disk=True)
    return lib


def _what_an_unordered_limit_would_draw(lib: Library) -> list[str]:
    """The folders the sampler would keep re-checking with ``ORDER BY RANDOM()`` gone.

    Spelled out in SQL rather than derived from the fixture's insertion order,
    so it measures what SQLite actually returns for a bare ``LIMIT`` instead of
    assuming rowid order.
    """
    with lib.transaction() as tx:
        rows = tx.query(
            "SELECT MIN(path) FROM items"
            " WHERE album_id IN (SELECT id FROM albums LIMIT ?)"
            " GROUP BY album_id",
            (_PRESENCE_SAMPLE_SIZE,),
        )
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
    assert 1 / math.comb(_TOTAL_ALBUMS, _PRESENCE_SAMPLE_SIZE) < 1e-9, (
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
