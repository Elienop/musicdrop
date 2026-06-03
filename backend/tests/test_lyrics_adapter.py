"""Tests for the lyrics adapter (app/beets/lyrics.py) and its models."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
import requests
from beets.library import Library
from beets.util.lyrics import Lyrics
from beetsplug._utils.requests import HTTPNotFoundError
from mediafile import MediaFile


def test_album_lyrics_result_model_roundtrips() -> None:
    from app.models.lyrics import AlbumLyricsResult, ItemLyricsOutcome

    result = AlbumLyricsResult(
        album_id=7,
        fetched=1,
        not_found=1,
        failed=0,
        skipped=2,
        items=[
            ItemLyricsOutcome(item_id=1, status="found", source="lrclib", written=True),
            ItemLyricsOutcome(item_id=2, status="not_found", source=None, written=False),
        ],
        writes_enabled=True,
    )
    assert result.fetched == 1
    assert result.items[0].status == "found"
    assert result.model_dump()["items"][1]["status"] == "not_found"


def _first_item(lib: Library) -> Any:
    album = next(iter(lib.albums()))
    return sorted(album.items(), key=lambda it: it.track)[0]


class _FakeBackend:
    """Stand-in for a beets lyrics Backend; .fetch returns/raises on demand."""

    def __init__(self, *, result: Lyrics | None = None, exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc
        self.calls = 0

    def fetch(self, artist: str, title: str, album: str, length: int) -> Lyrics | None:
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._result


class _FakePlugin:
    def __init__(self, backends: list[_FakeBackend]) -> None:
        self.backends = backends


def test_fetch_item_found_stores_and_writes(edit_lib: Library) -> None:
    from app.beets.lyrics import fetch_item_lyrics

    item = _first_item(edit_lib)
    lyr = Lyrics("These are the lyrics", "lrclib", "https://lrclib.net/api/get/1")
    plugin = _FakePlugin([_FakeBackend(result=lyr)])

    out = fetch_item_lyrics(plugin, item, force=False, write=True)

    assert out.status == "found"
    assert out.source == "lrclib"
    assert out.written is True
    assert item.lyrics == "These are the lyrics"
    assert item["lyrics_backend"] == "lrclib"
    # written into the file tag (mutagen) -> Plex can read it
    assert "These are the lyrics" in (MediaFile(os.fsdecode(item.path)).lyrics or "")


def test_fetch_item_write_gated_off(edit_lib: Library) -> None:
    from app.beets.lyrics import fetch_item_lyrics

    item = _first_item(edit_lib)
    plugin = _FakePlugin([_FakeBackend(result=Lyrics("x", "lrclib", "u"))])
    out = fetch_item_lyrics(plugin, item, force=False, write=False)
    assert out.status == "found"
    assert out.written is False
    assert item.lyrics == "x"  # stored in DB
    assert not (MediaFile(os.fsdecode(item.path)).lyrics or "")  # NOT written to file


class _WriteFailItem:
    """Wraps a beets Item but makes try_write() report failure (returns False).

    A plain ``monkeypatch.setattr(item, "try_write", ...)`` can't be used: a
    beets Item is a flex-attribute model, so assigning to ``try_write`` writes a
    flex field that ``store()`` then tries (and fails) to persist. This proxy
    delegates everything to the wrapped item except ``try_write``.
    """

    def __init__(self, item: Any) -> None:
        object.__setattr__(self, "_item", item)

    def try_write(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._item, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._item, name, value)

    def __getitem__(self, key: str) -> Any:
        return self._item[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._item[key] = value


def test_fetch_item_write_failure_reports_not_written_but_stores(edit_lib: Library) -> None:
    """If the file write fails, written=False (but DB store still ran)."""
    from app.beets.lyrics import fetch_item_lyrics

    item = _WriteFailItem(_first_item(edit_lib))
    plugin = _FakePlugin([_FakeBackend(result=Lyrics("file write fails", "lrclib", "u"))])

    out = fetch_item_lyrics(plugin, item, force=False, write=True)

    assert out.status == "found"
    assert out.written is False  # try_write returned False
    assert item.lyrics == "file write fails"  # store() still ran


def test_fetch_item_not_found(edit_lib: Library) -> None:
    from app.beets.lyrics import fetch_item_lyrics

    plugin = _FakePlugin([_FakeBackend(result=None)])
    out = fetch_item_lyrics(plugin, _first_item(edit_lib), force=False, write=True)
    assert out.status == "not_found"
    assert out.written is False


def test_fetch_item_http_404_is_not_found_not_failed(edit_lib: Library) -> None:
    from app.beets.lyrics import fetch_item_lyrics

    plugin = _FakePlugin([_FakeBackend(exc=HTTPNotFoundError())])
    out = fetch_item_lyrics(plugin, _first_item(edit_lib), force=False, write=True)
    assert out.status == "not_found"


def test_fetch_item_network_error_is_fetch_failed(edit_lib: Library) -> None:
    from app.beets.lyrics import fetch_item_lyrics

    plugin = _FakePlugin([_FakeBackend(exc=requests.exceptions.ConnectionError("boom"))])
    out = fetch_item_lyrics(plugin, _first_item(edit_lib), force=False, write=True)
    assert out.status == "fetch_failed"


def test_fetch_item_skips_existing_unless_forced(edit_lib: Library) -> None:
    from app.beets.lyrics import fetch_item_lyrics

    item = _first_item(edit_lib)
    item.lyrics = "already here"
    item.store()
    plugin = _FakePlugin([_FakeBackend(result=Lyrics("new", "lrclib", "u"))])

    skipped = fetch_item_lyrics(plugin, item, force=False, write=True)
    assert skipped.status == "skipped_existing"
    assert item.lyrics == "already here"  # untouched

    forced = fetch_item_lyrics(plugin, item, force=True, write=True)
    assert forced.status == "found"
    assert item.lyrics == "new"


def test_fetch_item_runs_from_worker_thread(edit_lib: Library) -> None:
    from app.beets.lyrics import fetch_item_lyrics

    item = _first_item(edit_lib)
    plugin = _FakePlugin([_FakeBackend(result=Lyrics("t", "lrclib", "u"))])
    with edit_lib.music_dir_context(), ThreadPoolExecutor(max_workers=1) as pool:
        out = pool.submit(fetch_item_lyrics, plugin, item, force=False, write=True).result()
    assert out.status == "found"


def test_fetch_album_lyrics_aggregates_and_skips_existing(
    edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.beets import lyrics as lyrics_mod
    from app.beets.lyrics import fetch_album_lyrics

    album = next(iter(edit_lib.albums()))
    items = sorted(album.items(), key=lambda it: it.track)
    items[0].lyrics = "pre-existing"  # one already has lyrics
    items[0].store()

    plugin = _FakePlugin([_FakeBackend(result=Lyrics("found text", "lrclib", "u"))])
    monkeypatch.setattr(lyrics_mod, "_make_lyrics_plugin", lambda: plugin)

    result = fetch_album_lyrics(edit_lib, int(album.id), force=False, write=True)

    assert result.album_id == int(album.id)
    assert result.skipped == 1  # the pre-existing one
    assert result.fetched == 2  # the other two got "found text"
    assert result.writes_enabled is True
    assert {o.status for o in result.items} == {"skipped_existing", "found"}


def test_fetch_album_lyrics_unknown_album(edit_lib: Library) -> None:
    from app.beets.lyrics import AlbumNotFoundError, fetch_album_lyrics

    with pytest.raises(AlbumNotFoundError):
        fetch_album_lyrics(edit_lib, 999999, force=False, write=True)
