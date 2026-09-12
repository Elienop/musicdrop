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
from beets.library import Library
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


def test_sidecar_mode_is_world_readable(tmp_path: Path) -> None:
    """Kept here, not moved to the shared writer's suite: that suite pins the
    ``mode=None`` REGIME, this pins the argument THIS writer passes. A sidecar
    written 0o600 is unreadable by the Plex process (another uid), and the shared
    suite stays green for that mutant."""
    from app.beets.lyrics import write_lyric_sidecar

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    old_umask = os.umask(0o022)
    try:
        write_lyric_sidecar(_fake_item(track), Lyrics(PLAIN), root=tmp_path)
    finally:
        os.umask(old_umask)
    mode = stat.S_IMODE((tmp_path / "t.txt").stat().st_mode)
    # first write takes the umask default; umask 0o022 -> 0o644, matching the
    # rest of the library, so the Plex process (other uid) can read it
    assert mode == 0o644


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
    """Vacuous truth is not a marker set: with nothing on disk, "every sidecar is
    a marker" must be False, not an empty-``all()`` True. The writer gates this
    behind ``_has_sidecar`` today, so the clause is what keeps a future caller
    from reading the predicate as "safe to delete"."""
    from app.beets.lyrics import _sidecars_are_all_markers

    track = tmp_path / "t.flac"
    track.write_bytes(b"")

    assert _sidecars_are_all_markers(_fake_item(track)) is False


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

    removed = remove_instrumental_marker_sidecars(_fake_item(track))

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

    assert remove_instrumental_marker_sidecars(_fake_item(track)) == []

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

    assert remove_instrumental_marker_sidecars(_fake_item(track)) == []
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

    assert remove_instrumental_marker_sidecars(_fake_item(track)) == []
    assert txt.exists()


def _boom(exc: Exception) -> Any:
    """A stand-in for the module's ``open`` that always raises ``exc``.

    Fault injection is the only root-independent way to reach the matcher's read
    branches: this deployment may run as root, for whom a 0o000 regular file is
    still readable, and every NON-regular fixture is now rejected by the stat
    guard before any read happens.
    """

    def _open(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    return _open


def test_marker_check_never_opens_a_non_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stat guard must reject a non-regular path BEFORE any open.

    Opening a FIFO for reading blocks until a writer appears, and the backfill
    worker is single-slot — one named pipe at a sidecar path would park it until
    the process restarts. Monkeypatching the module's ``open`` to explode turns
    that hang into a fast, deterministic failure, so the guard has a pin that
    does not depend on a timeout."""
    from app.beets.lyrics import remove_instrumental_marker_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    os.mkfifo(tmp_path / "t.lrc")
    (tmp_path / "t.txt").mkdir()
    # raising=False creates the module-level name; a module global shadows the
    # builtin for code inside that module, which scopes the fault injection to
    # exactly this one caller instead of patching builtins.open globally.
    monkeypatch.setattr(
        "app.beets.lyrics.open",
        _boom(AssertionError("must not open a non-regular file")),
        raising=False,
    )

    assert remove_instrumental_marker_sidecars(_fake_item(track)) == []


def test_a_fifo_at_the_sidecar_path_neither_blocks_nor_is_deleted(tmp_path: Path) -> None:
    """The same hazard end to end, with a REAL fifo and no monkeypatch: this test
    completing at all is the no-hang proof, and the pipe survives."""
    from app.beets.lyrics import remove_instrumental_marker_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    fifo = tmp_path / "t.lrc"
    os.mkfifo(fifo)

    assert remove_instrumental_marker_sidecars(_fake_item(track)) == []

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
        assert remove_instrumental_marker_sidecars(_fake_item(track)) == []

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
    monkeypatch.setattr("app.beets.lyrics.open", _boom(OSError("disk went away")), raising=False)

    with caplog.at_level(logging.WARNING, logger="app.beets.lyrics"):
        assert remove_instrumental_marker_sidecars(_fake_item(track)) == []

    assert marker.exists()
    assert "unreadable, keeping it" in caplog.text
    assert "removal failed" not in caplog.text  # no removal was attempted at all


def test_remove_marker_sidecars_keeps_a_marker_that_raced_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A file that vanishes between the stat and the open is KEPT and, unlike a
    real read error, logs NOTHING — a race is not a fault worth a warning."""
    from app.beets.lyrics import remove_instrumental_marker_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    marker = tmp_path / "t.txt"
    marker.write_text("[Instrumental]\n", encoding="utf-8")
    monkeypatch.setattr(
        "app.beets.lyrics.open", _boom(FileNotFoundError("raced away")), raising=False
    )

    with caplog.at_level(logging.WARNING, logger="app.beets.lyrics"):
        assert remove_instrumental_marker_sidecars(_fake_item(track)) == []

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

    assert remove_instrumental_marker_sidecars(_fake_item(track)) == []
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

    assert remove_instrumental_marker_sidecars(_fake_item(track)) == []
    assert txt.exists()


def test_remove_marker_sidecars_noop_when_none_exist(tmp_path: Path) -> None:
    from app.beets.lyrics import remove_instrumental_marker_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    assert remove_instrumental_marker_sidecars(_fake_item(track)) == []
    assert track.exists()


def test_remove_marker_sidecars_leaves_neighbours_alone(tmp_path: Path) -> None:
    from app.beets.lyrics import remove_instrumental_marker_sidecars

    track = tmp_path / "t.flac"
    track.write_bytes(b"")
    neighbour = tmp_path / "other.lrc"
    neighbour.write_text("[Instrumental]\n", encoding="utf-8")

    assert remove_instrumental_marker_sidecars(_fake_item(track)) == []
    assert neighbour.exists()  # only this track's own siblings are in scope


def test_remove_marker_sidecars_no_path_no_raise() -> None:
    from app.beets.lyrics import remove_instrumental_marker_sidecars

    assert remove_instrumental_marker_sidecars(SimpleNamespace(path=b"")) == []
    assert remove_instrumental_marker_sidecars(SimpleNamespace(path=None)) == []


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
