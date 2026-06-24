"""Tests for the lyrics adapter (app/beets/lyrics.py) and its models."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests
from beets.library import Library
from beets.util.lyrics import Lyrics
from beetsplug._utils.requests import HTTPNotFoundError
from mediafile import MediaFile


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


def test_fetch_item_skips_when_lyrics_and_sidecar_exist_unless_forced(edit_lib: Library) -> None:
    # Skip-existing now requires BOTH a lyrics tag AND a sidecar on disk, so a
    # track fetched before sidecars existed gets reprocessed on the next run.
    from app.beets.lyrics import fetch_item_lyrics

    item = _first_item(edit_lib)
    plugin = _FakePlugin([_FakeBackend(result=Lyrics("new", "lrclib", "u"))])

    # First fetch: stores lyrics AND writes a sidecar next to the track.
    first = fetch_item_lyrics(plugin, item, force=False, write=True)
    assert first.status == "found"

    # Second fetch: lyrics + sidecar both present -> skipped.
    skipped = fetch_item_lyrics(plugin, item, force=False, write=True)
    assert skipped.status == "skipped_existing"
    assert item.lyrics == "new"  # untouched

    # force still re-fetches.
    forced = fetch_item_lyrics(plugin, item, force=True, write=True)
    assert forced.status == "found"


def test_fetch_item_reprocesses_lyrics_without_sidecar(edit_lib: Library) -> None:
    # Embedded lyrics but no sidecar (the pre-feature state) -> NOT skipped.
    from app.beets.lyrics import fetch_item_lyrics

    item = _first_item(edit_lib)
    item.lyrics = "already here"
    item.store()
    plugin = _FakePlugin([_FakeBackend(result=Lyrics("new", "lrclib", "u"))])

    out = fetch_item_lyrics(plugin, item, force=False, write=True)
    assert out.status == "found"  # reprocessed to emit the sidecar
    assert item.lyrics == "new"


def test_fetch_item_runs_from_worker_thread(edit_lib: Library) -> None:
    from app.beets.lyrics import fetch_item_lyrics

    item = _first_item(edit_lib)
    plugin = _FakePlugin([_FakeBackend(result=Lyrics("t", "lrclib", "u"))])
    with edit_lib.music_dir_context(), ThreadPoolExecutor(max_workers=1) as pool:
        out = pool.submit(fetch_item_lyrics, plugin, item, force=False, write=True).result()
    assert out.status == "found"
