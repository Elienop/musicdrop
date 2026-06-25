import os
from pathlib import Path

from beets.library import Item, Library

from app.beets.library import list_artists
from app.beets.stats import build_stats_response, compute_stats, recent_albums
from tests.conftest import build_library


def _lib(tmp_path: Path) -> Library:
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def add(folder: str, fname: str, **fields: object) -> Item:
        base = music / folder
        base.mkdir(parents=True, exist_ok=True)
        f = base / fname
        f.write_bytes(b"\x00")
        it = Item(**fields)  # type: ignore[arg-type]  # beets Item kwargs are untyped
        it.path = os.fsencode(str(f))
        return it

    # Album A (older): 2 tracks, 120 s @ 320 kbps each.
    a1 = add(
        "A/One",
        "01.flac",
        album="One",
        albumartist="A",
        artist="A",
        title="t1",
        track=1,
        length=120.0,
        bitrate=320000,
    )
    a2 = add(
        "A/One",
        "02.flac",
        album="One",
        albumartist="A",
        artist="A",
        title="t2",
        track=2,
        length=120.0,
        bitrate=320000,
    )
    alb_a = lib.add_album([a1, a2])
    alb_a.added = 1000.0
    alb_a.store()
    # Album B (newer): 1 track, 60 s @ 256 kbps.
    b1 = add(
        "B/Two",
        "01.flac",
        album="Two",
        albumartist="B",
        artist="B",
        title="t3",
        track=1,
        length=60.0,
        bitrate=256000,
    )
    alb_b = lib.add_album([b1])
    alb_b.added = 2000.0
    alb_b.store()
    return lib


def test_compute_stats_counts_duration_and_size(tmp_path: Path) -> None:
    s = compute_stats(_lib(tmp_path))
    assert s.track_count == 3
    assert s.album_count == 2
    assert s.artist_count == 2
    assert s.total_seconds == 300.0  # 120 + 120 + 60
    # 320000*120/8 *2 + 256000*60/8 = 9_600_000 + 1_920_000 = 11_520_000
    assert s.total_bytes == 11_520_000


def test_recent_albums_newest_first(tmp_path: Path) -> None:
    recents = recent_albums(_lib(tmp_path), limit=8)
    assert [a.title for a in recents] == ["Two", "One"]  # B (added 2000) first


def test_recent_albums_respects_limit(tmp_path: Path) -> None:
    assert len(recent_albums(_lib(tmp_path), limit=1)) == 1


def test_build_stats_response(tmp_path: Path) -> None:
    resp = build_stats_response(_lib(tmp_path))
    assert resp.stats.track_count == 3
    assert resp.recently_added[0].title == "Two"
    assert resp.size_is_estimate is True


def test_empty_library_is_all_zero(tmp_path: Path) -> None:
    empty = build_library(str(tmp_path / "e.db"), str(tmp_path / "m"))
    resp = build_stats_response(empty)
    assert resp.stats.track_count == 0
    assert resp.stats.total_bytes == 0
    assert resp.recently_added == []


def test_artist_count_skips_blank_and_matches_roster(tmp_path: Path) -> None:
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def add(folder: str, fname: str, **fields: object) -> Item:
        base = music / folder
        base.mkdir(parents=True, exist_ok=True)
        f = base / fname
        f.write_bytes(b"\x00")
        it = Item(**fields)  # type: ignore[arg-type]  # beets Item kwargs are untyped
        it.path = os.fsencode(str(f))
        return it

    real = add("A/X", "01.flac", album="X", albumartist="A", artist="A", title="t", track=1)
    lib.add_album([real]).store()
    # An untagged album with a blank album artist — the roster filters it out.
    blank = add("blank", "01.flac", album="Y", albumartist="  ", artist="", title="t", track=1)
    lib.add_album([blank]).store()

    stats = compute_stats(lib)
    assert stats.album_count == 2  # both albums counted
    assert stats.artist_count == 1  # blank albumartist excluded
    assert stats.artist_count == len(list_artists(lib))  # matches the roster
