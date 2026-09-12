"""Tests for Plex-readable lyric sidecars (.lrc/.txt written next to tracks).

Plex does not read embedded lyrics tags — it reads an external sidecar file in
the track's folder, identically named: .lrc (synced) or .txt (plain). These
tests cover the writer, the synced overlay, and the wiring into the fetch path.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from beets.library import Item, Library
from beets.util.lyrics import Lyrics
from mediafile import MediaFile

# A synced LRCLib-style body (timestamped) vs a plain one.
SYNCED = "[00:01.00] line one\n[00:05.00] line two"
PLAIN = "line one\nline two"


def _fake_item(path: Path) -> Any:
    # write_lyric_sidecar / _sidecar_base only read item.path (beets = bytes).
    return SimpleNamespace(path=os.fsencode(str(path)))


def _album(root: Path) -> Path:
    """A real ``Artist/Album`` pair below ``root`` — the shape a beets library has."""
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    return album


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


# Most tests below keep the track in ``tmp_path`` itself and pass
# ``root=tmp_path``: the folder IS the root, which the writer opens directly since
# nothing sits below it to refuse. The two happy-path writer tests use ``_album``
# so the per-component descent runs there too.


def test_synced_lyrics_write_lrc(tmp_path: Path) -> None:
    from app.beets.lyrics import write_lyric_sidecar

    album = _album(tmp_path)
    track = album / "01 - Song.flac"
    track.write_bytes(b"")
    out = write_lyric_sidecar(_fake_item(track), Lyrics(SYNCED), root=tmp_path)
    lrc = album / "01 - Song.lrc"
    assert out == str(lrc)
    body = lrc.read_text(encoding="utf-8")
    assert "[00:01.00] line one" in body
    assert "[00:05.00] line two" in body


def test_plain_lyrics_write_txt(tmp_path: Path) -> None:
    from app.beets.lyrics import write_lyric_sidecar

    album = _album(tmp_path)
    track = album / "01 - Song.mp3"
    track.write_bytes(b"")
    out = write_lyric_sidecar(_fake_item(track), Lyrics(PLAIN), root=tmp_path)
    txt = album / "01 - Song.txt"
    assert out == str(txt)
    body = txt.read_text(encoding="utf-8")
    assert "line one" in body
    assert "line two" in body
    assert "[00:" not in body  # no timestamps in the plain sidecar


def test_the_written_mode_is_the_umask_and_not_a_fixed_0o644(tmp_path: Path) -> None:
    """This caller passes ``mode=None``, so the operator's umask decides.

    Kept here, not moved to the shared writer's suite: that suite pins the
    ``mode=None`` REGIME, this pins the argument THIS writer passes. Under
    umask 0o022 the two are indistinguishable (``0o644 & ~0o022 == 0o644``),
    which is measured: ``mode=0o644`` at the call site survived the whole file.
    Under 0o077 it does not — the same shape as the art writer's sibling pin.
    """
    from app.beets.lyrics import write_lyric_sidecar

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    old_umask = os.umask(0o077)
    try:
        write_lyric_sidecar(_fake_item(track), Lyrics(PLAIN), root=tmp_path)
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE((tmp_path / "t.txt").stat().st_mode) == 0o600


def test_the_longest_sidecar_name_the_derived_tmp_allowed_still_writes(
    tmp_path: Path,
) -> None:
    """The temp name must not shrink the set of destination names that write.

    ``.<dst.name>.tmp`` cost 5 bytes, so a sidecar name of ``NAME_MAX - 5``
    wrote. A temp name that embeds ``dst.name`` plus a pid and 16 hex costs 30,
    so names of 226-250 bytes fail ENAMETOOLONG instead — which
    ``write_lyric_sidecar`` logs and reports as None, skipping that track on
    every later run too. Only the destination's own ``NAME_MAX`` bounds it now.

    The end-to-end half of the claim. The temp name's own constant length is
    pinned on the shared writer, by ``test_playlists_atomic.py``'s
    ``test_the_temp_name_is_unpredictable_and_does_not_embed_the_target``."""
    from app.beets.lyrics import write_lyric_sidecar

    name_max = os.pathconf(str(tmp_path), "PC_NAME_MAX")
    stem = "a" * (name_max - 5 - len(".txt"))
    track = tmp_path / f"{stem}.flac"
    track.write_bytes(b"")

    out = write_lyric_sidecar(_fake_item(track), Lyrics(PLAIN), root=tmp_path)

    sidecar = tmp_path / f"{stem}.txt"
    assert len(sidecar.name.encode()) == name_max - 5  # the longest the old temp allowed
    assert out == str(sidecar)
    assert "line one" in sidecar.read_text(encoding="utf-8")


def test_the_fsynced_dir_fd_is_the_one_the_last_component_open_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole write hangs off one descriptor: the album folder is opened per
    component below the root and the temp create, the ``os.replace`` and the
    durability ``fsync`` all resolve against THAT fd. A publish that re-opened
    the folder by path would fsync a different fd — and could fsync a folder the
    walk never approved.
    """
    from app.beets.lyrics import write_lyric_sidecar

    album = _album(tmp_path)
    track = album / "t.flac"
    track.write_bytes(b"")

    real_open, real_fsync = os.open, os.fsync
    below: list[int] = []
    by_path: list[str] = []
    fsynced: list[int] = []

    # *args/**kwargs: the shared writer passes dir_fd= to os.open, which a
    # positional-only spy would reject with a TypeError.
    def spy_open(*args: Any, **kwargs: Any) -> int:
        fd = real_open(*args, **kwargs)
        flags = int(args[1])
        if flags & os.O_NOFOLLOW and flags & os.O_DIRECTORY:
            below.append(fd)
        if "dir_fd" not in kwargs:
            by_path.append(str(args[0]))
        return fd

    def spy_fsync(fd: int) -> None:
        fsynced.append(fd)
        return real_fsync(fd)

    # `os` is one shared module object, so patching it here is what the adapter
    # and the shared writer both see. (Reaching through `app.beets.lyrics.os`
    # instead fails mypy strict: the module does not explicitly export the name.)
    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "fsync", spy_fsync)

    assert write_lyric_sidecar(_fake_item(track), Lyrics(PLAIN), root=tmp_path) is not None

    assert below, "no component below the root was opened with O_DIRECTORY|O_NOFOLLOW"
    assert below[-1] in fsynced, "the fd the final component open returned was not fsynced"
    assert [q for q in by_path if Path(q) == album] == []  # never reached by path


# --- the folder must be reachable below the root without following a link -----
#
# Owner ruling 2026-09-12: art and lyrics writes REFUSE a symlinked directory
# component below the library root (bind mounts are the supported spelling for
# spanning disks); the root itself may be one. Measured before this slice: both
# writers followed such a link and wrote OUTSIDE the library.


def test_a_symlinked_album_folder_is_refused_and_nothing_lands_outside(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``music/Artist/Album -> ../../outside``: the album folder resolves out of
    the library, so the write is refused and the sidecar is NOT created at the
    link's target. Measured before the ruling: the sidecar landed in
    ``outside/``."""
    from app.beets.lyrics import write_lyric_sidecar

    root = tmp_path / "music"
    (root / "Artist").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    album = root / "Artist" / "Album"
    os.symlink("../../outside", album)
    track = album / "01 t.flac"
    track.write_bytes(b"")  # lands in outside/, through the link

    with caplog.at_level(logging.WARNING, logger="app.beets.lyrics"):
        out = write_lyric_sidecar(_fake_item(track), Lyrics(PLAIN), root=root)

    assert out is None
    assert sorted(q.name for q in outside.iterdir()) == ["01 t.flac"]
    assert len(caplog.records) == 1
    assert str(album) in caplog.text
    # The OSError arm: a link IS the thing a bind mount replaces.
    assert "bind mount" in caplog.text


def test_a_symlinked_folder_between_two_real_ones_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``music/Evil -> /elsewhere`` with a real ``Album/`` beneath it: the LEAF is
    a genuine directory, so an ``O_NOFOLLOW`` on the album folder alone accepts
    it and writes outside the library (measured). Every component below the root
    is walked, so this is refused at ``Evil``."""
    from app.beets.lyrics import write_lyric_sidecar

    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "Album").mkdir(parents=True)
    root = tmp_path / "music"
    root.mkdir()
    os.symlink(elsewhere, root / "Evil")
    track = root / "Evil" / "Album" / "01 t.flac"
    track.write_bytes(b"")

    with caplog.at_level(logging.WARNING, logger="app.beets.lyrics"):
        out = write_lyric_sidecar(_fake_item(track), Lyrics(PLAIN), root=root)

    assert out is None
    assert sorted(q.name for q in (elsewhere / "Album").iterdir()) == ["01 t.flac"]
    assert len(caplog.records) == 1
    assert str(root / "Evil" / "Album") in caplog.text


def test_a_folder_outside_the_root_is_refused_without_raising(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A row whose path never was under the library root — a legacy import, or a
    ``directory:`` the operator changed. ``Path.relative_to`` raises there, and
    this writer's contract is that it never raises: None, one log line, nothing
    written."""
    from app.beets.lyrics import write_lyric_sidecar

    root = tmp_path / "music"
    root.mkdir()
    stray = tmp_path / "stray"
    stray.mkdir()
    track = stray / "01 t.flac"
    track.write_bytes(b"")

    with caplog.at_level(logging.WARNING, logger="app.beets.lyrics"):
        out = write_lyric_sidecar(_fake_item(track), Lyrics(PLAIN), root=root)

    assert out is None
    assert sorted(q.name for q in stray.iterdir()) == ["01 t.flac"]
    assert len(caplog.records) == 1
    assert str(stray) in caplog.text
    # The ValueError arm gets its OWN sentence: this folder is not under the
    # root at all, and a bind mount would not put it there.
    assert "not under the library root" in caplog.text
    assert "bind mount" not in caplog.text


def test_a_track_in_the_library_root_itself_still_gets_its_sidecar(tmp_path: Path) -> None:
    """A flat ``path_formats`` leaves tracks directly in the root, so there is no
    component below it to refuse. ``open_below`` refuses zero parts by contract,
    so the writer opens the root itself — which the ruling allows to be a link."""
    from app.beets.lyrics import write_lyric_sidecar

    root = tmp_path / "music"
    root.mkdir()
    track = root / "Artist - Song.flac"
    track.write_bytes(b"")

    out = write_lyric_sidecar(_fake_item(track), Lyrics(PLAIN), root=root)

    assert out == str(root / "Artist - Song.txt")
    assert "line one" in (root / "Artist - Song.txt").read_text(encoding="utf-8")


def test_the_marker_clear_behind_a_symlinked_folder_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The DELETE half is anchored too, and it is the destructive one.

    Measured before this: the writer cleared the marker sidecar THROUGH the link
    and then logged "not reachable below the library root" and wrote nothing —
    the refusal line was not "nothing happened". The descriptor is opened first
    now, so a refused folder is refused for the read, the unlink and the write
    alike."""
    from app.beets.lyrics import write_lyric_sidecar

    root = tmp_path / "music"
    (root / "Artist").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    album = root / "Artist" / "Album"
    os.symlink("../../outside", album)
    track = album / "t.flac"
    track.write_bytes(b"")
    marker = outside / "t.lrc"
    marker.write_text("[00:01.00] [Instrumental]\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="app.beets.lyrics"):
        out = write_lyric_sidecar(_fake_item(track), Lyrics(SYNCED), root=root)

    assert out is None
    assert marker.read_text(encoding="utf-8") == "[00:01.00] [Instrumental]\n"
    assert sorted(q.name for q in outside.iterdir()) == ["t.flac", "t.lrc"]
    assert len(caplog.records) == 1  # ONE line: the clear did not log its own
    assert "lyric sidecar skipped" in caplog.text


def test_the_instrumental_verdict_clear_behind_a_symlinked_folder_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The same escape from the other caller — the instrumental verdict's clear.

    Measured before this: ``remove_instrumental_marker_sidecars`` deleted the
    file behind the link and reported the LINK's spelling as the path removed."""
    from app.beets.lyrics import remove_instrumental_marker_sidecars

    root = tmp_path / "music"
    (root / "Artist").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    album = root / "Artist" / "Album"
    os.symlink("../../outside", album)
    track = album / "t.flac"
    track.write_bytes(b"")
    marker = outside / "t.lrc"
    marker.write_text("[Instrumental]\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="app.beets.lyrics"):
        removed = remove_instrumental_marker_sidecars(_fake_item(track), root=root)

    assert removed == []
    assert marker.read_text(encoding="utf-8") == "[Instrumental]\n"
    assert len(caplog.records) == 1
    assert "lyric sidecars left alone" in caplog.text


def test_the_instrumental_verdict_anchors_against_the_items_own_library_root(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``_store_instrumental`` derives the root from the item's own library
    handle (``item._db``) rather than taking one on its signature, so the verdict
    path is anchored without changing what its callers pass."""
    from app.beets.lyrics import _store_instrumental

    root = tmp_path / "music"
    (root / "Artist").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    album = root / "Artist" / "Album"
    os.symlink("../../outside", album)
    track = album / "t.flac"
    track.write_bytes(b"")
    marker = outside / "t.txt"
    marker.write_text("[Instrumental]\n", encoding="utf-8")

    lib = Library(str(tmp_path / "lib.db"), directory=str(root))
    item = Item(path=os.fsencode(str(track)), title="t", artist="A", album="Al")
    lib.add(item)

    with caplog.at_level(logging.WARNING, logger="app.beets.lyrics"):
        _store_instrumental(item, Lyrics("", "lrclib", "u"))

    assert item["lyrics_instrumental"] == 1  # the verdict still lands on the row
    assert marker.read_text(encoding="utf-8") == "[Instrumental]\n"
    assert len(caplog.records) == 1
    assert "lyric sidecars left alone" in caplog.text


def test_every_sidecar_syscall_lands_in_the_folder_the_descriptor_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One descriptor decides the folder, so a swap after it is opened cannot
    move the gate, the clear or the write.

    The security seat's retarget race made deterministic: the album folder is
    renamed aside and a DECOY holding the user's own lyrics takes its path, the
    instant after the descriptor is opened. The two folders disagree about WHICH
    names exist, which is what separates the three by-path spellings:

    * a by-PATH presence gate reads the decoy's ``t.lrc``, cannot find it through
      the fd, and refuses — the marker in the opened folder is never cleared;
    * a by-PATH unlink deletes the DECOY's ``t.txt``, which holds real lyrics;
    * anchored, every name resolves in the folder the walk approved.
    """
    from app.beets import lyrics as mod

    root = tmp_path / "music"
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    track = album / "t.flac"
    track.write_bytes(b"")
    (album / "t.txt").write_text("[Instrumental]\n", encoding="utf-8")

    decoy = root / "Artist" / "Decoy"
    decoy.mkdir()
    (decoy / "t.txt").write_text("the user's own lyrics\n", encoding="utf-8")
    (decoy / "t.lrc").write_text("[00:09.00] the user's own synced line\n", encoding="utf-8")

    moved = root / "Artist" / "Moved"
    real_open_album = mod._open_album_dir

    def swapping_open(directory: Path, r: Path) -> int:
        fd = real_open_album(directory, r)
        os.rename(album, moved)
        os.rename(decoy, album)  # the decoy now answers to the album's path
        return fd

    monkeypatch.setattr(mod, "_open_album_dir", swapping_open)

    out = mod.write_lyric_sidecar(_fake_item(track), Lyrics(SYNCED), root=root)

    assert out == str(album / "t.lrc")  # the pre-swap spelling
    assert "[00:01.00] line one" in (moved / "t.lrc").read_text(encoding="utf-8")
    assert not (moved / "t.txt").exists()  # the marker cleared, in the opened folder
    assert (album / "t.txt").read_text(encoding="utf-8") == "the user's own lyrics\n"
    assert (album / "t.lrc").read_text(encoding="utf-8") == (
        "[00:09.00] the user's own synced line\n"
    )


def test_a_symlink_at_the_sidecar_name_is_neither_read_through_nor_replaced(
    tmp_path: Path,
) -> None:
    """``O_NOFOLLOW`` on the sidecar open: a symlink at ``t.txt`` is not read
    through, so its target's content cannot make the file look like a disposable
    marker. Something IS at the name, so the gap-fill gate refuses and the link
    survives. Without the flag the marker behind it qualifies, the link is
    unlinked and a regular file takes its place."""
    from app.beets.lyrics import write_lyric_sidecar

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    target = tmp_path / "elsewhere.txt"
    target.write_text("[Instrumental]\n", encoding="utf-8")
    link = tmp_path / "t.txt"
    link.symlink_to(target)

    assert write_lyric_sidecar(_fake_item(track), Lyrics(PLAIN), root=tmp_path) is None

    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "[Instrumental]\n"
    assert not (tmp_path / "t.lrc").exists()


def test_a_dangling_symlink_at_the_sidecar_name_is_not_a_gap(tmp_path: Path) -> None:
    """The presence stat is ``follow_symlinks=False``, so a DANGLING link counts
    as present and the writer refuses. Following it would read "absent" and
    publish a regular file over the link the user put there."""
    from app.beets.lyrics import write_lyric_sidecar

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    link = tmp_path / "t.txt"
    link.symlink_to(tmp_path / "gone.txt")

    assert write_lyric_sidecar(_fake_item(track), Lyrics(PLAIN), root=tmp_path) is None

    assert link.is_symlink()
    assert not (tmp_path / "gone.txt").exists()


def test_an_empty_body_clears_no_marker(tmp_path: Path) -> None:
    """The clear happens only on the path that goes on to WRITE. A fetched body
    that strips to nothing returns before the folder is even opened, so the stale
    marker is left for the next run rather than removed for no replacement."""
    from app.beets.lyrics import write_lyric_sidecar

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    marker = tmp_path / "t.txt"
    marker.write_text("[Instrumental]\n", encoding="utf-8")

    assert write_lyric_sidecar(_fake_item(track), Lyrics("   "), root=tmp_path) is None

    assert marker.read_text(encoding="utf-8") == "[Instrumental]\n"


def test_a_lone_surrogate_in_the_fetched_lyrics_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The shared writer's encode is STRICT UTF-8, so a lone surrogate raises
    ``UnicodeEncodeError`` — a ``ValueError``, which ``except OSError`` does not
    catch. This writer's contract is that it never raises: None, one log line,
    no file."""
    from app.beets.lyrics import write_lyric_sidecar

    track = tmp_path / "t.flac"
    track.write_bytes(b"")

    with caplog.at_level(logging.WARNING, logger="app.beets.lyrics"):
        out = write_lyric_sidecar(_fake_item(track), Lyrics("line \udcff one"), root=tmp_path)

    assert out is None
    assert not (tmp_path / "t.txt").exists()
    assert len(caplog.records) == 1
    assert "write failed" in caplog.text


def test_a_newline_in_the_folder_name_cannot_forge_a_second_log_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Paths are logged ``%r`` through ``display_path``, the rule stated at
    ``api/artists.py:896``. Measured with ``%s``: a folder named with a newline
    and an ANSI escape produced a second line carrying an attacker-chosen
    timestamp, severity and colour."""
    from app.beets.lyrics import write_lyric_sidecar

    root = tmp_path / "music"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    evil = "Artist\n2026-09-12 00:00:00 CRITICAL forged\x1b[31m"
    os.symlink(outside, root / evil)
    track = root / evil / "t.flac"
    track.write_bytes(b"")

    with caplog.at_level(logging.WARNING, logger="app.beets.lyrics"):
        assert write_lyric_sidecar(_fake_item(track), Lyrics(PLAIN), root=root) is None

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "\n" not in message  # one line, not two
    assert "\x1b" not in message  # no live escape
    assert "\\n2026-09-12 00:00:00 CRITICAL forged\\x1b[31m" in message


def test_synced_result_never_replaces_an_existing_txt(tmp_path: Path) -> None:
    """A1: an existing sidecar of EITHER extension means the writer does nothing
    — no .lrc written beside it, and the .txt is not unlinked. Nothing records
    who wrote a sidecar, so a user's file always wins."""
    from app.beets.lyrics import write_lyric_sidecar

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    txt = tmp_path / "t.txt"
    txt.write_text("the user's own plain lyrics", encoding="utf-8")

    assert write_lyric_sidecar(_fake_item(track), Lyrics(SYNCED), root=tmp_path) is None

    assert txt.read_text(encoding="utf-8") == "the user's own plain lyrics"
    assert not (tmp_path / "t.lrc").exists()  # and no coexisting second sidecar


def test_plain_result_never_downgrades_an_existing_lrc(tmp_path: Path) -> None:
    """A4 (never downgrade): a plain answer must not delete a synced .lrc to put
    a worse .txt in its place — and must not sit beside it either."""
    from app.beets.lyrics import write_lyric_sidecar

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    lrc = tmp_path / "t.lrc"
    lrc.write_text("[00:01.00] curated synced line", encoding="utf-8")

    assert write_lyric_sidecar(_fake_item(track), Lyrics(PLAIN), root=tmp_path) is None

    assert lrc.read_text(encoding="utf-8") == "[00:01.00] curated synced line"
    assert not (tmp_path / "t.txt").exists()


def test_plain_result_never_overwrites_an_existing_txt(tmp_path: Path) -> None:
    """A1's same-extension arm: an atomic replace is still destruction."""
    from app.beets.lyrics import write_lyric_sidecar

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    txt = tmp_path / "t.txt"
    txt.write_text("the user's own plain lyrics", encoding="utf-8")

    assert write_lyric_sidecar(_fake_item(track), Lyrics(PLAIN), root=tmp_path) is None

    assert txt.read_text(encoding="utf-8") == "the user's own plain lyrics"


def test_synced_result_never_overwrites_an_existing_lrc(tmp_path: Path) -> None:
    """A1's same-extension arm for .lrc: a fetched synced body never replaces a
    curated one, however good the match looks."""
    from app.beets.lyrics import write_lyric_sidecar

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    lrc = tmp_path / "t.lrc"
    lrc.write_text("[00:02.00] curated synced line", encoding="utf-8")

    assert write_lyric_sidecar(_fake_item(track), Lyrics(SYNCED), root=tmp_path) is None

    assert lrc.read_text(encoding="utf-8") == "[00:02.00] curated synced line"


def test_a_marker_only_sidecar_is_replaced_by_a_found_result(tmp_path: Path) -> None:
    """The one file an existing sidecar does NOT protect: a stale
    "[Instrumental]" marker AGREES with "this track has no lyrics", so leaving it
    in place while real lyrics arrive would keep Plex showing "[Instrumental]"
    forever — the mirror image of the verdict path, which deletes exactly these.
    An all-marker set counts as absent: it is cleared and the gap is filled."""
    from app.beets.lyrics import write_lyric_sidecar

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    (tmp_path / "t.txt").write_text("[Instrumental]\n", encoding="utf-8")

    out = write_lyric_sidecar(_fake_item(track), Lyrics(SYNCED), root=tmp_path)

    assert out == str(tmp_path / "t.lrc")
    assert not (tmp_path / "t.txt").exists()  # the stale marker is gone
    assert "[00:01.00] line one" in (tmp_path / "t.lrc").read_text(encoding="utf-8")


def test_a_mixed_marker_and_real_pair_is_left_entirely_alone(tmp_path: Path) -> None:
    """ANY non-marker sidecar refuses everything, including its marker sibling.

    A curated .lrc is not made disposable by a stray .txt next to it: the two
    files are one user's lyric state, and acting on half of it is acting on a
    guess. All-or-nothing, deliberately."""
    from app.beets.lyrics import write_lyric_sidecar

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    lrc = tmp_path / "t.lrc"
    real = b"[00:02.00] the user's own synced line\n"
    lrc.write_bytes(real)
    txt = tmp_path / "t.txt"
    marker = b"[Instrumental]\n"
    txt.write_bytes(marker)

    assert write_lyric_sidecar(_fake_item(track), Lyrics(PLAIN), root=tmp_path) is None

    # A lenient "any marker is enough" gate diverges on the refusal above AND on
    # the marker's content (it would be replaced by the fetched body). The .lrc
    # alone would NOT catch it — the remover is itself marker-guarded, so the
    # real file survives that mutant too.
    assert txt.read_bytes() == marker
    assert lrc.read_bytes() == real


def test_no_sidecars_is_not_an_all_marker_set(tmp_path: Path) -> None:
    """Vacuous truth is not a marker set: the gate reads the names PRESENT in the
    album folder's descriptor and only takes the all-markers branch when that
    list is non-empty. An empty list run through ``all()`` is True, which is how
    a content guard turns back into a blanket deleter."""
    from app.beets.lyrics import _present_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")

    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert _present_sidecars(str(tmp_path / "t"), dir_fd=fd) == []
    finally:
        os.close(fd)


def test_no_path_no_file_no_raise(tmp_path: Path) -> None:
    from app.beets.lyrics import write_lyric_sidecar

    assert write_lyric_sidecar(SimpleNamespace(path=b""), Lyrics(PLAIN), root=tmp_path) is None
    assert write_lyric_sidecar(SimpleNamespace(path=None), Lyrics(PLAIN), root=tmp_path) is None


def test_empty_body_no_file(tmp_path: Path) -> None:
    from app.beets.lyrics import write_lyric_sidecar

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    assert write_lyric_sidecar(_fake_item(track), Lyrics("   "), root=tmp_path) is None
    assert not (tmp_path / "t.txt").exists()
    assert not (tmp_path / "t.lrc").exists()


# --- the remover (a fresh instrumental verdict drops its OWN marker files) ----
#
# A3: the only deletion left anywhere in this module. Content is the authorship
# proxy — a file whose whole body is beets' "[Instrumental]" marker carries no
# lyric data and is the exact artifact legacy flows wrote, so removing it keeps
# Plex from serving "[Instrumental]" without ever destroying real lyrics.


def test_remove_marker_sidecars_deletes_both_marker_forms(tmp_path: Path) -> None:
    """Both shapes legacy MusicDrop wrote: a plain .txt marker and an
    LRC-TIMESTAMPED .lrc marker (pre-#122 synced writes). The timestamped form is
    why the matcher strips timestamps line-wise instead of comparing raw text."""
    from app.beets.lyrics import remove_instrumental_marker_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    (tmp_path / "t.lrc").write_text("[00:01.00] [Instrumental]\n", encoding="utf-8")
    (tmp_path / "t.txt").write_text("[Instrumental]\n", encoding="utf-8")

    removed = remove_instrumental_marker_sidecars(_fake_item(track), root=tmp_path)

    assert sorted(removed) == sorted([str(tmp_path / "t.lrc"), str(tmp_path / "t.txt")])
    assert not (tmp_path / "t.lrc").exists()
    assert not (tmp_path / "t.txt").exists()
    assert track.exists()  # never the audio file


def test_remove_marker_sidecars_keeps_real_lyrics(tmp_path: Path) -> None:
    """The guard that makes the remover safe to reach: real lyric content is
    kept, whichever extension it sits in."""
    from app.beets.lyrics import remove_instrumental_marker_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    lrc = tmp_path / "t.lrc"
    lrc.write_text("[00:01.00] a real synced line\n[00:05.00] and another\n", encoding="utf-8")
    txt = tmp_path / "t.txt"
    txt.write_text("a real plain line\n", encoding="utf-8")

    assert remove_instrumental_marker_sidecars(_fake_item(track), root=tmp_path) == []

    assert lrc.exists()
    assert txt.exists()


def test_remove_marker_sidecars_keeps_a_mixed_file(tmp_path: Path) -> None:
    """A marker line inside a file that ALSO has real lyrics is not a marker
    file — every non-empty line must be the marker."""
    from app.beets.lyrics import remove_instrumental_marker_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    lrc = tmp_path / "t.lrc"
    lrc.write_text("[00:01.00] [Instrumental]\n[00:30.00] then the singing\n", encoding="utf-8")

    assert remove_instrumental_marker_sidecars(_fake_item(track), root=tmp_path) == []
    assert lrc.exists()


def test_remove_marker_sidecars_keeps_an_empty_sidecar(tmp_path: Path) -> None:
    """An empty file is not the marker. Deliberate: "no non-empty line differs
    from the marker" is vacuously true, and deleting on a vacuous match is how a
    content guard quietly becomes a blanket deleter."""
    from app.beets.lyrics import remove_instrumental_marker_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    txt = tmp_path / "t.txt"
    txt.write_text("   \n", encoding="utf-8")

    assert remove_instrumental_marker_sidecars(_fake_item(track), root=tmp_path) == []
    assert txt.exists()


def _fail_read(exc: Exception) -> Any:
    """A stand-in for ``os.read`` that always raises ``exc``.

    Fault injection is the only root-independent way to reach the matcher's read
    branch: this deployment may run as root, for whom a 0o000 regular file is
    still readable, and every NON-regular fixture is rejected by the ``fstat``
    before any read happens.
    """

    def _read(*_args: Any, **_kwargs: Any) -> bytes:
        raise exc

    return _read


def _fail_name_open(exc: Exception) -> Any:
    """``os.open`` raising ``exc`` for every NAME resolved against a ``dir_fd``.

    The folder-descriptor opens (no ``dir_fd=``) still work, so the fault lands
    on the sidecar open alone.
    """
    real_open = os.open

    def _open(*args: Any, **kwargs: Any) -> int:
        if "dir_fd" in kwargs:
            raise exc
        return real_open(*args, **kwargs)

    return _open


def test_marker_check_never_reads_a_non_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``fstat`` must reject a non-regular file before any read.

    Reading a FIFO blocks until a writer appears, and the backfill worker is
    single-slot — one named pipe at a sidecar path would park it until the
    process restarts. ``O_NONBLOCK`` keeps the OPEN from blocking (a FIFO opens
    fine and a directory opens fine, measured), so the type check is what stands
    between the worker and that read. A forwarding spy counts the reads, which
    does not depend on a timeout."""
    from app.beets.lyrics import remove_instrumental_marker_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    os.mkfifo(tmp_path / "t.lrc")
    (tmp_path / "t.txt").mkdir()

    real_read = os.read
    reads: list[int] = []

    def spy_read(fd: int, length: int) -> bytes:
        reads.append(fd)
        return real_read(fd, length)

    monkeypatch.setattr(os, "read", spy_read)

    assert remove_instrumental_marker_sidecars(_fake_item(track), root=tmp_path) == []

    assert reads == []
    assert stat.S_ISFIFO((tmp_path / "t.lrc").stat().st_mode)
    assert (tmp_path / "t.txt").is_dir()


def test_a_fifo_at_the_sidecar_path_neither_blocks_nor_is_deleted(tmp_path: Path) -> None:
    """The same hazard end to end, with a REAL fifo and no monkeypatch: this test
    completing at all is the no-hang proof, and the pipe survives."""
    from app.beets.lyrics import remove_instrumental_marker_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    fifo = tmp_path / "t.lrc"
    os.mkfifo(fifo)

    assert remove_instrumental_marker_sidecars(_fake_item(track), root=tmp_path) == []

    assert stat.S_ISFIFO(fifo.stat().st_mode)


def test_remove_marker_sidecars_ignores_a_directory_silently(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A directory at the sidecar path is rejected by the cheap stat, not by a
    failed read — which is what the SILENCE proves. Without the stat guard the
    open raises IsADirectoryError and the read-error branch logs; the file
    survives either way, so "it still exists" alone would not be killable."""
    from app.beets.lyrics import remove_instrumental_marker_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    blocked = tmp_path / "t.lrc"
    blocked.mkdir()

    with caplog.at_level(logging.WARNING, logger="app.beets.lyrics"):
        assert remove_instrumental_marker_sidecars(_fake_item(track), root=tmp_path) == []

    assert blocked.is_dir()
    assert caplog.text == ""  # rejected by the stat, never opened


def test_remove_marker_sidecars_keeps_a_marker_it_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A read error means KEEP even when the file WOULD have qualified: unknown
    content may be the user's lyrics, and the unlink is never attempted."""
    from app.beets.lyrics import remove_instrumental_marker_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    marker = tmp_path / "t.txt"
    marker.write_text("[Instrumental]\n", encoding="utf-8")
    monkeypatch.setattr(os, "read", _fail_read(OSError("disk went away")))

    with caplog.at_level(logging.WARNING, logger="app.beets.lyrics"):
        assert remove_instrumental_marker_sidecars(_fake_item(track), root=tmp_path) == []

    assert marker.exists()
    assert "unreadable, keeping it" in caplog.text
    assert "removal failed" not in caplog.text  # no removal was attempted at all


def test_remove_marker_sidecars_keeps_a_marker_that_raced_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A file that vanishes before the open is KEPT and, unlike a real read
    error, logs NOTHING — a race is not a fault worth a warning, and it is the
    same arm the ordinary "no sidecar here" case takes."""
    from app.beets.lyrics import remove_instrumental_marker_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    marker = tmp_path / "t.txt"
    marker.write_text("[Instrumental]\n", encoding="utf-8")
    monkeypatch.setattr(os, "open", _fail_name_open(FileNotFoundError("raced away")))

    with caplog.at_level(logging.WARNING, logger="app.beets.lyrics"):
        assert remove_instrumental_marker_sidecars(_fake_item(track), root=tmp_path) == []

    assert marker.exists()
    assert caplog.text == ""


def test_remove_marker_sidecars_keeps_a_broken_symlink(tmp_path: Path) -> None:
    """A dangling sidecar symlink is not a regular file, so it is rejected by the
    stat guard and left alone — and unlike the directory case an unlink WOULD
    have succeeded here, so this is the fixture that proves the reject really
    does mean "keep" rather than merely "the delete failed"."""
    from app.beets.lyrics import remove_instrumental_marker_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    dangling = tmp_path / "t.lrc"
    dangling.symlink_to(tmp_path / "gone.lrc")

    assert remove_instrumental_marker_sidecars(_fake_item(track), root=tmp_path) == []
    assert dangling.is_symlink()


def test_remove_marker_sidecars_keeps_an_oversized_marker_file(tmp_path: Path) -> None:
    """The read is capped, and a file past the cap is kept rather than judged on
    a truncated body — a marker file is a handful of bytes, so anything larger is
    not the artifact this remover targets."""
    from app.beets.lyrics import _MARKER_READ_CAP, remove_instrumental_marker_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    txt = tmp_path / "t.txt"
    line = "[Instrumental]\n"
    txt.write_text(line * (_MARKER_READ_CAP // len(line) + 2), encoding="utf-8")
    assert txt.stat().st_size > _MARKER_READ_CAP

    assert remove_instrumental_marker_sidecars(_fake_item(track), root=tmp_path) == []
    assert txt.exists()


def test_remove_marker_sidecars_noop_when_none_exist(tmp_path: Path) -> None:
    from app.beets.lyrics import remove_instrumental_marker_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    assert remove_instrumental_marker_sidecars(_fake_item(track), root=tmp_path) == []
    assert track.exists()


def test_remove_marker_sidecars_leaves_neighbours_alone(tmp_path: Path) -> None:
    from app.beets.lyrics import remove_instrumental_marker_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    neighbour = tmp_path / "other.lrc"
    neighbour.write_text("[Instrumental]\n", encoding="utf-8")

    assert remove_instrumental_marker_sidecars(_fake_item(track), root=tmp_path) == []
    assert neighbour.exists()  # only this track's own siblings are in scope


def test_remove_marker_sidecars_no_path_no_raise(tmp_path: Path) -> None:
    from app.beets.lyrics import remove_instrumental_marker_sidecars

    assert remove_instrumental_marker_sidecars(SimpleNamespace(path=b""), root=tmp_path) == []
    assert remove_instrumental_marker_sidecars(SimpleNamespace(path=None), root=tmp_path) == []


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
