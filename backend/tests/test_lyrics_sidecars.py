"""Tests for Plex-readable lyric sidecars (.lrc/.txt written next to tracks).

Plex does not read embedded lyrics tags — it reads an external sidecar file in
the track's folder, identically named: .lrc (synced) or .txt (plain). These
tests cover the writer, the synced overlay, and the wiring into the fetch path.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from beets.library import Library
from beets.util.lyrics import Lyrics
from mediafile import MediaFile

# A synced LRCLib-style body (timestamped) vs a plain one.
SYNCED = "[00:01.00] line one\n[00:05.00] line two"
PLAIN = "line one\nline two"


def _fake_item(path: Path) -> Any:
    # write_lyric_sidecar / _sidecar_base only read item.path (beets = bytes).
    return SimpleNamespace(path=os.fsencode(str(path)))


class _FakeBackend:
    def __init__(self, *, result: Lyrics | None) -> None:
        self._result = result

    def fetch(self, artist: str, title: str, album: str, length: int) -> Lyrics | None:
        return self._result


class _FakePlugin:
    def __init__(self, backends: list[_FakeBackend]) -> None:
        self.backends = backends


def _first_item(lib: Library) -> Any:
    album = next(iter(lib.albums()))
    return sorted(album.items(), key=lambda it: it.track)[0]


# --- Task 1: the writer -------------------------------------------------------


def test_synced_lyrics_write_lrc(tmp_path: Path) -> None:
    from app.beets.lyrics import write_lyric_sidecar

    track = tmp_path / "01 - Song.flac"
    track.write_bytes(b"")
    out = write_lyric_sidecar(_fake_item(track), Lyrics(SYNCED))
    lrc = tmp_path / "01 - Song.lrc"
    assert out == str(lrc)
    body = lrc.read_text(encoding="utf-8")
    assert "[00:01.00] line one" in body
    assert "[00:05.00] line two" in body


def test_plain_lyrics_write_txt(tmp_path: Path) -> None:
    from app.beets.lyrics import write_lyric_sidecar

    track = tmp_path / "01 - Song.mp3"
    track.write_bytes(b"")
    out = write_lyric_sidecar(_fake_item(track), Lyrics(PLAIN))
    txt = tmp_path / "01 - Song.txt"
    assert out == str(txt)
    body = txt.read_text(encoding="utf-8")
    assert "line one" in body
    assert "line two" in body
    assert "[00:" not in body  # no timestamps in the plain sidecar


def test_sidecar_mode_is_world_readable(tmp_path: Path) -> None:
    from app.beets.lyrics import write_lyric_sidecar

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    old_umask = os.umask(0o022)
    try:
        write_lyric_sidecar(_fake_item(track), Lyrics(PLAIN))
    finally:
        os.umask(old_umask)
    mode = stat.S_IMODE((tmp_path / "t.txt").stat().st_mode)
    # first write takes the umask default; umask 0o022 -> 0o644, matching the
    # rest of the library, so the Plex process (other uid) can read it
    assert mode == 0o644


def test_sidecar_rewrite_preserves_tightened_mode(tmp_path: Path) -> None:
    from app.beets.lyrics import write_lyric_sidecar

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    write_lyric_sidecar(_fake_item(track), Lyrics(PLAIN))
    txt = tmp_path / "t.txt"
    txt.chmod(0o600)
    # same sidecar type (.txt -> .txt) so the rewrite hits the same dst
    write_lyric_sidecar(_fake_item(track), Lyrics("updated line one\nupdated line two"))
    assert stat.S_IMODE(txt.stat().st_mode) == 0o600  # rewrite preserves the tightened mode
    body = txt.read_text(encoding="utf-8")
    assert "updated line one" in body  # and the content was updated


def test_writing_lrc_removes_stale_txt(tmp_path: Path) -> None:
    from app.beets.lyrics import write_lyric_sidecar

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    (tmp_path / "t.txt").write_text("old plain", encoding="utf-8")
    write_lyric_sidecar(_fake_item(track), Lyrics(SYNCED))
    assert (tmp_path / "t.lrc").exists()
    assert not (tmp_path / "t.txt").exists()  # opposite sibling removed


def test_writing_txt_removes_stale_lrc(tmp_path: Path) -> None:
    from app.beets.lyrics import write_lyric_sidecar

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    (tmp_path / "t.lrc").write_text("[00:01.00] old", encoding="utf-8")
    write_lyric_sidecar(_fake_item(track), Lyrics(PLAIN))
    assert (tmp_path / "t.txt").exists()
    assert not (tmp_path / "t.lrc").exists()


def test_no_path_no_file_no_raise() -> None:
    from app.beets.lyrics import write_lyric_sidecar

    assert write_lyric_sidecar(SimpleNamespace(path=b""), Lyrics(PLAIN)) is None
    assert write_lyric_sidecar(SimpleNamespace(path=None), Lyrics(PLAIN)) is None


def test_empty_body_no_file(tmp_path: Path) -> None:
    from app.beets.lyrics import write_lyric_sidecar

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    assert write_lyric_sidecar(_fake_item(track), Lyrics("   ")) is None
    assert not (tmp_path / "t.txt").exists()
    assert not (tmp_path / "t.lrc").exists()


# --- the remover (instrumental verdicts drop stale sidecars) ------------------


def test_remove_sidecars_deletes_both_extensions(tmp_path: Path) -> None:
    from app.beets.lyrics import remove_lyric_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    (tmp_path / "t.lrc").write_text("[00:01.00] old", encoding="utf-8")
    (tmp_path / "t.txt").write_text("old", encoding="utf-8")

    removed = remove_lyric_sidecars(_fake_item(track))

    assert sorted(removed) == sorted([str(tmp_path / "t.lrc"), str(tmp_path / "t.txt")])
    assert not (tmp_path / "t.lrc").exists()
    assert not (tmp_path / "t.txt").exists()
    assert track.exists()  # never the audio file


def test_remove_sidecars_noop_when_none_exist(tmp_path: Path) -> None:
    from app.beets.lyrics import remove_lyric_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    assert remove_lyric_sidecars(_fake_item(track)) == []
    assert track.exists()


def test_remove_sidecars_leaves_neighbours_alone(tmp_path: Path) -> None:
    from app.beets.lyrics import remove_lyric_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    neighbour = tmp_path / "other.lrc"
    neighbour.write_text("someone else's lyrics", encoding="utf-8")

    assert remove_lyric_sidecars(_fake_item(track)) == []
    assert neighbour.exists()  # only this track's own siblings are in scope


def test_remove_sidecars_no_path_no_raise() -> None:
    from app.beets.lyrics import remove_lyric_sidecars

    assert remove_lyric_sidecars(SimpleNamespace(path=b"")) == []
    assert remove_lyric_sidecars(SimpleNamespace(path=None)) == []


# --- Task 2: force synced fetching -------------------------------------------


def test_make_lyrics_plugin_forces_synced() -> None:
    import beets

    from app.beets.lyrics import make_lyrics_plugin

    make_lyrics_plugin()
    assert beets.config["lyrics"]["synced"].get(bool) is True
    assert beets.config["lyrics"]["auto"].get(bool) is False


# --- Task 3: wiring into the fetch path (embed plain + always sidecar) --------


def test_synced_fetch_embeds_plain_and_writes_lrc(edit_lib: Library) -> None:
    from app.beets.lyrics import fetch_item_lyrics

    item = _first_item(edit_lib)
    plugin = _FakePlugin([_FakeBackend(result=Lyrics(SYNCED))])
    out = fetch_item_lyrics(plugin, item, force=False, write=True)

    assert out.status == "found"
    # embedded tag stores PLAIN text (timestamps stripped)
    assert item.lyrics == "line one\nline two"
    assert "[00:" not in (item.lyrics or "")
    # .lrc sidecar written next to the track, with timing
    base, _ext = os.path.splitext(os.fsdecode(item.path))
    lrc = Path(base + ".lrc")
    assert lrc.exists()
    assert "[00:01.00] line one" in lrc.read_text(encoding="utf-8")


def test_sidecar_written_even_when_write_off(edit_lib: Library) -> None:
    from app.beets.lyrics import fetch_item_lyrics

    item = _first_item(edit_lib)
    plugin = _FakePlugin([_FakeBackend(result=Lyrics(PLAIN))])
    out = fetch_item_lyrics(plugin, item, force=False, write=False)

    assert out.written is False  # tag NOT written (write gate off)
    base, _ext = os.path.splitext(os.fsdecode(item.path))
    assert Path(base + ".txt").exists()  # sidecar independent of the write gate
    assert not MediaFile(os.fsdecode(item.path)).lyrics  # file tag empty
