"""Tests for the lyrics adapter (app/beets/lyrics.py) and its models."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests
from beets.library import Library
from beets.util.lyrics import INSTRUMENTAL_LYRICS, Lyrics
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


def test_store_lyrics_persists_mtime_so_disk_sync_gate_stays_shut(edit_lib: Library) -> None:
    """Writing the lyrics tag bumps the file's on-disk mtime; the DB store must run
    AFTER the write so that fresh mtime is persisted. Storing BEFORE the write leaves
    the DB mtime behind the file, and disk-sync's staleness gate then re-probes every
    lyric-written track forever (full MediaFile parse per file over HDD/NAS)."""
    from app.beets.lyrics import _store_lyrics

    item = _first_item(edit_lib)
    _store_lyrics(item, Lyrics("la la la", "lrclib", "u"), write=True)

    row = edit_lib.get_item(item.id)
    assert row is not None
    # DB mtime equals the file the write just touched -> the mtime gate is shut.
    assert row.mtime == int(os.path.getmtime(os.fsdecode(row.path)))


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
    assert not MediaFile(os.fsdecode(item.path)).lyrics  # NOT written to file


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


def test_fetch_item_null_text_result_is_not_found_not_crash(edit_lib: Library) -> None:
    # beets 2.12's LRCLib can hand back a Lyrics whose .text is None (a candidate
    # with null plainLyrics, synced not selected). beets' own Lyrics.text_lines
    # then does None.splitlines() and raises AttributeError, which used to abort
    # the WHOLE backfill on one such track. Treat it as no usable lyrics.
    from app.beets.lyrics import fetch_item_lyrics

    item = _first_item(edit_lib)
    null_text = Lyrics(None, "lrclib", "u")  # type: ignore[arg-type]  # the bug: backend returns text=None
    plugin = _FakePlugin([_FakeBackend(result=null_text)])

    out = fetch_item_lyrics(plugin, item, force=False, write=True)

    assert out.status == "not_found"  # not a crash
    assert out.written is False
    assert not item.lyrics  # nothing stored
    assert item.get("lyrics_checked")  # marked searched -> won't re-crash next run


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


def test_skip_instrumental_cleans_a_stale_sidecar(edit_lib: Library) -> None:
    """beets' 2.13 migration leaves already-instrumental tracks flagged, and
    sweeps skip flagged tracks without re-searching — so the skip gate is the
    ONLY place their old "[Instrumental]" sidecars (which Plex keeps reading)
    can ever be cleaned up."""
    from app.beets.lyrics import _sidecar_base, fetch_item_lyrics

    seed = _first_item(edit_lib)
    seed["lyrics_instrumental"] = 1
    seed.store()
    item = edit_lib.get_item(seed.id)
    assert item is not None
    base = _sidecar_base(item)
    assert base is not None
    stale = Path(base + ".txt")
    stale.write_text("[Instrumental]", encoding="utf-8")
    never = _FakeBackend(result=Lyrics("should not be fetched", "lrclib", "u"))

    out = fetch_item_lyrics(_FakePlugin([never]), item, force=False, write=True)

    assert out.status == "skipped_instrumental"
    assert never.calls == 0  # still no re-search — cleanup only
    assert not stale.exists()


def test_skip_existing_keeps_its_sidecar(edit_lib: Library) -> None:
    """The cleanup lives ONLY in the instrumental gate: a track skipped for
    having lyrics + sidecar must keep that sidecar."""
    from app.beets.lyrics import _sidecar_base, fetch_item_lyrics

    item = _first_item(edit_lib)
    plugin = _FakePlugin([_FakeBackend(result=Lyrics("real", "lrclib", "u"))])
    assert fetch_item_lyrics(plugin, item, force=False, write=True).status == "found"
    base = _sidecar_base(item)
    assert base is not None
    sidecar = Path(base + ".lrc" if Path(base + ".lrc").exists() else base + ".txt")
    assert sidecar.exists()

    assert fetch_item_lyrics(plugin, item, force=False, write=True).status == "skipped_existing"
    assert sidecar.exists()


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


def test_fetch_item_not_found_marks_checked(edit_lib: Library) -> None:
    from app.beets.lyrics import fetch_item_lyrics

    item = _first_item(edit_lib)
    out = fetch_item_lyrics(_FakePlugin([_FakeBackend(result=None)]), item, force=False, write=True)
    assert out.status == "not_found"
    assert item.get("lyrics_checked")  # persisted "searched, found nothing"


def test_fetch_item_fetch_failed_does_not_mark_checked(edit_lib: Library) -> None:
    from app.beets.lyrics import fetch_item_lyrics

    item = _first_item(edit_lib)
    plugin = _FakePlugin([_FakeBackend(exc=requests.exceptions.ConnectionError("boom"))])
    out = fetch_item_lyrics(plugin, item, force=False, write=True)
    assert out.status == "fetch_failed"
    assert not item.get("lyrics_checked")  # transient — must be retried next run


def test_fetch_item_skips_checked_unless_recheck(edit_lib: Library) -> None:
    from app.beets.lyrics import fetch_item_lyrics

    item = _first_item(edit_lib)
    empty = _FakeBackend(result=None)
    plugin = _FakePlugin([empty])

    fetch_item_lyrics(plugin, item, force=False, write=True)  # marks checked
    after_first = empty.calls

    out = fetch_item_lyrics(plugin, item, force=False, write=True)
    assert out.status == "skipped_checked"
    assert empty.calls == after_first  # backend NOT called again

    out2 = fetch_item_lyrics(plugin, item, force=False, write=True, recheck_misses=True)
    assert out2.status == "not_found"
    assert empty.calls > after_first  # recheck re-searches


# --- instrumental: a definitive answer, not a miss ---------------------------


def _instrumental(backend: str = "lrclib") -> Lyrics:
    """What a backend really hands back for an instrumental: beets' Lyrics
    normalises the "[Instrumental]" marker to text="" + instrumental=True."""
    lyr = Lyrics(INSTRUMENTAL_LYRICS, backend, "https://lrclib.net/api/get/1")
    assert lyr.text == ""  # guards the beets contract
    assert lyr.instrumental is True  # guards the beets contract
    return lyr


def test_fetch_item_instrumental_stops_searching_and_flags(edit_lib: Library) -> None:
    from app.beets.lyrics import fetch_item_lyrics

    item = _first_item(edit_lib)
    hit = _FakeBackend(result=_instrumental())
    never = _FakeBackend(result=Lyrics("should never be reached", "genius", "u"))
    plugin = _FakePlugin([hit, never])

    out = fetch_item_lyrics(plugin, item, force=False, write=True)

    assert out.status == "instrumental"
    assert out.source == "lrclib"
    assert out.written is False
    # Definitive: the very first hit ends the search — no second backend, and no
    # further search pairs (search_pairs yields several titles per item).
    assert hit.calls == 1
    assert never.calls == 0
    assert item.lyrics == ""
    assert item.get("lyrics_instrumental")  # flagged the way beets flags it
    assert item.get("lyrics_checked")  # and marked searched


def test_fetch_item_instrumental_clears_stale_lyrics_and_sidecars(edit_lib: Library) -> None:
    """A force re-fetch of a track that still carries the old "[Instrumental]"
    text must end with an empty lyrics field and no sidecars — but the audio
    file's own tag is out of scope and stays untouched."""
    from app.beets.lyrics import fetch_item_lyrics

    item = _first_item(edit_lib)
    item.lyrics = INSTRUMENTAL_LYRICS
    item.try_write()  # the stale marker also sits in the FILE tag
    item.store()
    base, _ext = os.path.splitext(os.fsdecode(item.path))
    Path(base + ".lrc").write_text("[00:01.00] [Instrumental]\n", encoding="utf-8")
    Path(base + ".txt").write_text("[Instrumental]\n", encoding="utf-8")

    out = fetch_item_lyrics(
        _FakePlugin([_FakeBackend(result=_instrumental())]), item, force=True, write=True
    )

    assert out.status == "instrumental"
    assert item.lyrics == ""
    row = edit_lib.get_item(item.id)
    assert row is not None
    assert row.lyrics == ""  # persisted, not just in memory
    assert not Path(base + ".lrc").exists()
    assert not Path(base + ".txt").exists()
    assert INSTRUMENTAL_LYRICS in (MediaFile(os.fsdecode(item.path)).lyrics or "")


def test_fetch_item_instrumental_sidecar_removal_noop_when_none(edit_lib: Library) -> None:
    from app.beets.lyrics import fetch_item_lyrics

    item = _first_item(edit_lib)
    base, _ext = os.path.splitext(os.fsdecode(item.path))
    assert not Path(base + ".lrc").exists()
    assert not Path(base + ".txt").exists()

    out = fetch_item_lyrics(
        _FakePlugin([_FakeBackend(result=_instrumental())]), item, force=False, write=True
    )

    assert out.status == "instrumental"  # no sidecar to delete is not an error
    assert not Path(base + ".lrc").exists()
    assert not Path(base + ".txt").exists()


def test_fetch_item_skips_known_instrumental_even_on_recheck(edit_lib: Library) -> None:
    """The migrated shape: beets' 2.13 migration sets the flex flag and empties
    ``lyrics`` but leaves NO ``lyrics_checked``, so only the instrumental gate
    can keep those tracks out of a recheck-misses sweep."""
    from app.beets.lyrics import fetch_item_lyrics

    seed = _first_item(edit_lib)
    seed["lyrics_instrumental"] = 1
    seed.store()
    item = edit_lib.get_item(seed.id)  # reload: the flex value reads back as "1"
    assert item is not None
    assert not item.get("lyrics_checked")

    backend = _FakeBackend(result=Lyrics("late-arriving lyrics", "lrclib", "u"))
    plugin = _FakePlugin([backend])

    assert fetch_item_lyrics(plugin, item, force=False, write=True).status == (
        "skipped_instrumental"
    )
    assert (
        fetch_item_lyrics(plugin, item, force=False, write=True, recheck_misses=True).status
        == "skipped_instrumental"
    )
    assert backend.calls == 0  # neither sweep re-searched it

    forced = fetch_item_lyrics(plugin, item, force=True, write=True)
    assert forced.status == "found"  # force is the one way back in
    assert backend.calls == 1

    # ...and the way back in must STICK: a found result clears the stale verdict
    # (beets' own plugin writes lyrics_instrumental=False on every found track).
    # Without the reset, every later sweep mislabels this track
    # skipped_instrumental, and clearing its lyrics again would lock it out of
    # recheck sweeps entirely.
    from app.beets.library import _is_instrumental

    refetched = edit_lib.get_item(seed.id)
    assert refetched is not None
    assert not _is_instrumental(refetched)
    after = fetch_item_lyrics(plugin, refetched, force=False, write=True)
    assert after.status == "skipped_existing"


def test_fetch_item_false_instrumental_flag_is_not_a_skip(edit_lib: Library) -> None:
    """beets writes ``lyrics_instrumental`` = False on tracks it found real
    lyrics for, and that reads back as the string "0" — which is truthy in
    Python. Those tracks must still be searched."""
    from app.beets.lyrics import fetch_item_lyrics

    seed = _first_item(edit_lib)
    seed["lyrics_instrumental"] = False
    seed.store()
    item = edit_lib.get_item(seed.id)
    assert item is not None
    assert item.get("lyrics_instrumental") in (False, "0", 0)

    backend = _FakeBackend(result=Lyrics("real lyrics", "lrclib", "u"))
    out = fetch_item_lyrics(_FakePlugin([backend]), item, force=False, write=True)

    assert out.status == "found"
    assert backend.calls == 1


def test_fetch_item_runs_from_worker_thread(edit_lib: Library) -> None:
    from app.beets.lyrics import fetch_item_lyrics

    item = _first_item(edit_lib)
    plugin = _FakePlugin([_FakeBackend(result=Lyrics("t", "lrclib", "u"))])
    with edit_lib.music_dir_context(), ThreadPoolExecutor(max_workers=1) as pool:
        out = pool.submit(fetch_item_lyrics, plugin, item, force=False, write=True).result()
    assert out.status == "found"


def test_make_lyrics_plugin_drops_keyless_google() -> None:
    import beets

    from app.beets.lyrics import make_lyrics_plugin

    beets.config["lyrics"]["sources"].set(["lrclib", "google", "genius"])
    beets.config["lyrics"]["google_API_key"].set(None)

    plugin = make_lyrics_plugin()

    # We pin sources (dropping a keyless google) BEFORE construction, so beets'
    # "Disabling Google source" warning never fires.
    assert list(beets.config["lyrics"]["sources"].as_str_seq()) == ["lrclib", "genius"]
    assert [getattr(type(b), "name", None) for b in plugin.backends] == ["lrclib", "genius"]


def test_make_lyrics_plugin_keeps_google_with_key() -> None:
    import beets

    from app.beets.lyrics import make_lyrics_plugin

    beets.config["lyrics"]["sources"].set(["lrclib", "google"])
    beets.config["lyrics"]["google_API_key"].set("test-key")
    beets.config["lyrics"]["google_engine_ID"].set("test-engine")

    plugin = make_lyrics_plugin()
    names = [getattr(type(b), "name", None) for b in plugin.backends]
    assert "google" in names


def test_make_lyrics_plugin_lrclib_only_drops_genius() -> None:
    import beets

    from app.beets.lyrics import make_lyrics_plugin

    beets.config["lyrics"]["sources"].set(["lrclib", "genius"])

    # Library sweeps pass lrclib_only=True so a big bulk run never hammers Genius
    # (which 429s under load); Genius stays for the targeted per-album fetch.
    plugin = make_lyrics_plugin(lrclib_only=True)
    assert [getattr(type(b), "name", None) for b in plugin.backends] == ["lrclib"]
