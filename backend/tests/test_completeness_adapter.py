"""Tests for the missing-tracks adapter (app/beets/completeness.py).

The beets MusicBrainz fetch is faked by monkeypatching
``metadata_plugins.get_metadata_source`` to return a stub whose ``album_for_id``
yields a lightweight release object (only the attributes the adapter reads).
No network, no real AlbumInfo construction.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from beets.library import Item, Library

from app.beets.library import _require_id
from tests.conftest import build_library


def _make_lib(tmp_path: Any, *, mb_albumid: str, trackids: list[str | None]) -> Library:
    """A hermetic 1-album library; each item gets the given mb_trackid (or none)."""
    music = tmp_path / "music"
    base = music / "Radiohead" / "In Rainbows"
    base.mkdir(parents=True, exist_ok=True)
    lib = build_library(str(tmp_path / "library.db"), str(music))
    items = []
    for i, tid in enumerate(trackids, start=1):
        f = base / f"{i:02d} Track {i}.mp3"
        f.write_bytes(b"\x00")
        it = Item(
            album="In Rainbows",
            albumartist="Radiohead",
            artist="Radiohead",
            title=f"Track {i}",
            track=i,
            disc=1,
        )
        it.path = os.fsencode(str(f))
        if tid:
            it.mb_trackid = tid
        items.append(it)
    al = lib.add_album(items)
    if mb_albumid:
        al["mb_albumid"] = mb_albumid
    al.store()
    return lib


def _release(*trackids: str) -> SimpleNamespace:
    """A fake release: tracks with track_id/index/medium/title/length."""
    return SimpleNamespace(
        tracks=[
            SimpleNamespace(
                track_id=tid, index=i, medium=1, title=f"Track {i}", length=float(180 + i)
            )
            for i, tid in enumerate(trackids, start=1)
        ]
    )


def _patch_source(monkeypatch: pytest.MonkeyPatch, album_for_id: Any) -> dict[str, int]:
    """Patch get_metadata_source -> a stub source; return a call counter dict."""
    from app.beets import completeness as comp

    calls = {"n": 0}

    class _Source:
        def album_for_id(self, mbid: str) -> Any:
            calls["n"] += 1
            return album_for_id(mbid)

    monkeypatch.setattr(
        comp.metadata_plugins,  # type: ignore[attr-defined]  # re-exported beets symbol
        "get_metadata_source",
        lambda name: _Source(),
    )
    return calls


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    from app.beets.completeness import clear_release_cache

    clear_release_cache()


def _aid(lib: Library) -> int:
    return _require_id(next(iter(lib.albums())).id)


def test_classifies_present_and_missing(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.beets.completeness import release_missing_report

    lib = _make_lib(tmp_path, mb_albumid="rel-1", trackids=["t1", "t2"])
    _patch_source(monkeypatch, lambda mbid: _release("t1", "t2", "t3", "t4"))
    report = release_missing_report(lib, _aid(lib))
    assert report.status == "ok"
    assert report.total == 4
    assert report.present_count == 2
    assert [m.mb_trackid for m in report.missing] == ["t3", "t4"]
    assert report.missing[0].index == 3
    assert report.missing[0].disc == 1
    assert report.source == "MusicBrainz"


def test_numeric_release_ids_match_string_item_ids(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Deezer-sourced release yields integer track_id; the library stores the
    same id as a string (beets mb_trackid is a String field). The owned tracks
    must NOT be flagged missing despite the int-vs-str difference."""
    from app.beets.completeness import release_missing_report

    lib = _make_lib(tmp_path, mb_albumid="123", trackids=["1421196172", "1421196173"])
    release = SimpleNamespace(
        tracks=[
            SimpleNamespace(track_id=1421196172, index=1, medium=1, title="Track 1", length=181.0),
            SimpleNamespace(track_id=1421196173, index=2, medium=1, title="Track 2", length=182.0),
        ]
    )
    _patch_source(monkeypatch, lambda mbid: release)
    report = release_missing_report(lib, _aid(lib))
    assert report.status == "ok"
    assert report.total == 2
    assert report.present_count == 2  # both owned -> nothing missing
    assert report.missing == []


def test_no_mb_albumid(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.beets.completeness import release_missing_report

    lib = _make_lib(tmp_path, mb_albumid="", trackids=["t1"])
    calls = _patch_source(monkeypatch, lambda mbid: _release("t1"))
    report = release_missing_report(lib, _aid(lib))
    assert report.status == "no_musicbrainz_id"
    assert report.total == 0 and report.missing == []
    assert calls["n"] == 0  # no fetch attempted


def test_mb_album_but_no_item_ids(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.beets.completeness import release_missing_report

    lib = _make_lib(tmp_path, mb_albumid="rel-1", trackids=[None, None])
    calls = _patch_source(monkeypatch, lambda mbid: _release("t1"))
    report = release_missing_report(lib, _aid(lib))
    assert report.status == "no_musicbrainz_id"
    assert calls["n"] == 0  # guarded before fetch — avoids "everything missing"


def test_release_unavailable_on_none(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.beets.completeness import release_missing_report

    lib = _make_lib(tmp_path, mb_albumid="rel-x", trackids=["t1"])
    _patch_source(monkeypatch, lambda mbid: None)
    report = release_missing_report(lib, _aid(lib))
    assert report.status == "release_unavailable"
    assert report.source == "MusicBrainz"  # falls back to MB when no data_source set


def test_error_report_carries_resolved_source(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fetch failure still names WHICH source it tried, so the UI can label it
    (e.g. 'Couldn't reach Deezer') instead of always saying MusicBrainz."""
    from app.beets.completeness import release_missing_report

    lib = _make_lib(tmp_path, mb_albumid="rel-1", trackids=["t1"])
    album = lib.get_album(_aid(lib))
    assert album is not None
    album["data_source"] = "Deezer"
    album.store()

    _patch_source(monkeypatch, lambda mbid: None)  # release not found
    unavailable = release_missing_report(lib, _aid(lib))
    assert unavailable.status == "release_unavailable"
    assert unavailable.source == "Deezer"

    def _boom(mbid: str) -> Any:
        raise requests.exceptions.ConnectionError("network down")

    from app.beets.completeness import clear_release_cache

    clear_release_cache()
    _patch_source(monkeypatch, _boom)  # network error
    failed = release_missing_report(lib, _aid(lib))
    assert failed.status == "fetch_failed"
    assert failed.source == "Deezer"


def test_release_unavailable_when_source_missing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.beets import completeness as comp
    from app.beets.completeness import release_missing_report

    lib = _make_lib(tmp_path, mb_albumid="rel-1", trackids=["t1"])
    monkeypatch.setattr(
        comp.metadata_plugins,  # type: ignore[attr-defined]  # re-exported beets symbol
        "get_metadata_source",
        lambda name: None,
    )
    report = release_missing_report(lib, _aid(lib))
    assert report.status == "release_unavailable"


def test_fetch_failed_on_network_error(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.beets.completeness import release_missing_report

    def _boom(mbid: str) -> Any:
        raise requests.exceptions.ConnectionError("network down")

    lib = _make_lib(tmp_path, mb_albumid="rel-1", trackids=["t1"])
    _patch_source(monkeypatch, _boom)
    report = release_missing_report(lib, _aid(lib))
    assert report.status == "fetch_failed"
    assert report.source == "MusicBrainz"  # falls back to MB when no data_source set


def test_unknown_album_raises(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.beets.completeness import AlbumNotFoundError, release_missing_report

    lib = _make_lib(tmp_path, mb_albumid="rel-1", trackids=["t1"])
    _patch_source(monkeypatch, lambda mbid: _release("t1"))
    with pytest.raises(AlbumNotFoundError):
        release_missing_report(lib, 999999)


def test_caches_release_but_reclassifies(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Second call does NOT re-fetch, but reflects a newly-acquired track."""
    from app.beets.completeness import release_missing_report

    lib = _make_lib(tmp_path, mb_albumid="rel-1", trackids=["t1"])
    calls = _patch_source(monkeypatch, lambda mbid: _release("t1", "t2"))

    first = release_missing_report(lib, _aid(lib))
    assert first.present_count == 1 and [m.mb_trackid for m in first.missing] == ["t2"]

    # "Acquire" t2: add an item carrying mb_trackid t2.
    album = lib.get_album(_aid(lib))
    assert album is not None
    base = os.path.dirname(os.fsdecode(next(iter(album.items())).path))
    f = os.path.join(base, "02 Track 2.mp3")
    with open(f, "wb") as fh:
        fh.write(b"\x00")
    it = Item(
        album="In Rainbows",
        albumartist="Radiohead",
        artist="Radiohead",
        title="Track 2",
        track=2,
        disc=1,
        mb_trackid="t2",
    )
    it.path = os.fsencode(f)
    it.album_id = album.id  # attach to the existing album
    lib.add(it)

    second = release_missing_report(lib, _aid(lib))
    assert second.present_count == 2 and second.missing == []
    assert calls["n"] == 1  # release fetched once; classification recomputed locally


def test_runs_from_worker_thread(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from app.beets.completeness import release_missing_report

    lib = _make_lib(tmp_path, mb_albumid="rel-1", trackids=["t1"])
    _patch_source(monkeypatch, lambda mbid: _release("t1", "t2"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        report = pool.submit(release_missing_report, lib, _aid(lib)).result()
    assert report.status == "ok" and report.present_count == 1


def test_fetch_release_survives_a_raced_eviction_of_a_just_read_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The LRU cache hit path is check-then-act: _CACHE.get(k) (hit) then
    # _CACHE.move_to_end(k). On a threadpool a concurrent insert can trip the
    # eviction (popitem) that removes exactly k in between, so move_to_end raises
    # KeyError. That must NOT propagate as a 500 — the read still succeeded, the
    # LRU promote is best-effort. Simulate the eviction by making move_to_end
    # raise, and assert the cached value is still returned.
    from app.beets import completeness as comp

    comp.clear_release_cache()
    info = SimpleNamespace(tracks=[], mb="rel-1")
    comp._CACHE["rel-1"] = info  # a warm hit

    def _evicted(key: str, last: bool = True) -> None:
        raise KeyError(key)  # another thread evicted it between get and here

    monkeypatch.setattr(comp._CACHE, "move_to_end", _evicted)
    result = comp._fetch_release("rel-1", "MusicBrainz")
    assert result.status == "ok"
    assert result.info is info  # the read still returned the cached release
