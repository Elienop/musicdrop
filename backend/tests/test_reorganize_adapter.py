# backend/tests/test_reorganize_adapter.py
import logging
import os
import shutil
from pathlib import Path

import pytest
from beets.library import Album, Item, Library

from app.beets import reorganize as reorg
from app.beets.reorganize import (
    _describe_album,
    _item_moves,
    album_label,
    collect_units,
    plan_reorganize,
)
from tests.conftest import build_library

SAMPLE_FLAC = Path(__file__).parent / "fixtures" / "silent.flac"


def _album(lib: Library, name: str) -> Album:
    return next(a for a in lib.albums() if a.album == name)


def test_album_label(reorganize_lib: Library) -> None:
    with reorganize_lib.music_dir_context():
        assert album_label(_album(reorganize_lib, "In Rainbows")) == "Radiohead - In Rainbows"


def test_item_moves_detects_misfiled(reorganize_lib: Library) -> None:
    with reorganize_lib.music_dir_context():
        ir = _album(reorganize_lib, "In Rainbows")
        assert all(_item_moves(reorganize_lib, i) for i in ir.items())
        disc = _album(reorganize_lib, "Discovery")
        assert not any(_item_moves(reorganize_lib, i) for i in disc.items())


def test_describe_album_misfiled_has_distinct_dirs(reorganize_lib: Library) -> None:
    with reorganize_lib.music_dir_context():
        m = _describe_album(reorganize_lib, _album(reorganize_lib, "In Rainbows"))
        assert m is not None
        assert m.kind == "album"
        assert m.track_count == 3
        assert m.from_path != m.to_path
        assert m.to_path.endswith("Radiohead/In Rainbows")


def test_describe_album_already_in_place_is_none(reorganize_lib: Library) -> None:
    with reorganize_lib.music_dir_context():
        assert _describe_album(reorganize_lib, _album(reorganize_lib, "Discovery")) is None


def test_describe_album_rename_in_place_same_dir(reorganize_lib: Library) -> None:
    with reorganize_lib.music_dir_context():
        m = _describe_album(reorganize_lib, _album(reorganize_lib, "Geogaddi"))
        assert m is not None
        assert m.from_path == m.to_path  # only filenames change


def test_collect_units_library_includes_singleton(reorganize_lib: Library) -> None:
    albums, singletons = collect_units(reorganize_lib, scope="library", artist=None, album_id=None)
    assert len(albums) == 3
    assert len(singletons) == 1


def test_plan_library_counts(reorganize_lib: Library) -> None:
    plan = plan_reorganize(reorganize_lib, scope="library", artist=None, album_id=None)
    # 3 albums + 1 singleton = 4 units; Discovery already in place -> 3 will move.
    assert plan.total == 4
    assert plan.will_move == 3
    assert plan.already_in_place == 1
    assert plan.truncated is False
    assert {m.kind for m in plan.moves} == {"album", "singleton"}


def test_plan_artist_scope(reorganize_lib: Library) -> None:
    plan = plan_reorganize(reorganize_lib, scope="artist", artist="Radiohead", album_id=None)
    assert plan.scope_label == "Radiohead"
    assert plan.total == 1 and plan.will_move == 1


def test_multidisc_to_path_is_album_root(tmp_path: Path) -> None:
    music = tmp_path / "music"
    lib = build_library(
        str(tmp_path / "library.db"),
        str(music),
        path_format="$albumartist/$album/Disc $disc/$track $title",
    )
    base = music / "junk"
    base.mkdir(parents=True, exist_ok=True)
    items = []
    for disc in (1, 2):
        f = base / f"d{disc}.mp3"
        f.write_bytes(b"\x00")
        it = Item(album="Wall", albumartist="PF", artist="PF", title=f"T{disc}", track=1, disc=disc)
        it.path = os.fsencode(str(f))
        items.append(it)
    lib.add_album(items).store()
    with lib.music_dir_context():
        album = next(iter(lib.albums()))
        m = reorg._describe_album(lib, album)
    assert m is not None
    # New layout splits across Disc 1/ Disc 2/, but the reported root is the album dir.
    assert m.to_path.endswith("PF/Wall")
    assert m.track_count == 2


def test_preview_row_cap(monkeypatch: pytest.MonkeyPatch, reorganize_lib: Library) -> None:
    monkeypatch.setattr(reorg, "PREVIEW_ROW_CAP", 1)
    plan = reorg.plan_reorganize(reorganize_lib, scope="library", artist=None, album_id=None)
    assert plan.will_move == 3  # exact count unaffected by the cap
    assert len(plan.moves) == 1  # rows capped
    assert plan.truncated is True


def test_reorganize_album_moves_and_prunes(reorganize_lib: Library) -> None:
    with reorganize_lib.music_dir_context():
        album = _album(reorganize_lib, "In Rainbows")
        old_dir = os.path.dirname(os.fsdecode(next(iter(album.items())).path))
        outcome = reorg.reorganize_album(reorganize_lib, album)
    assert outcome.status == "moved"
    assert not os.path.exists(old_dir)  # vacated source pruned
    music = os.fsdecode(reorganize_lib.directory)
    moved = os.path.join(music, "Radiohead", "In Rainbows")
    assert os.path.isdir(moved)
    # DB paths now point at the new location.
    with reorganize_lib.music_dir_context():
        again = _album(reorganize_lib, "In Rainbows")
        assert all(not reorg._item_moves(reorganize_lib, i) for i in again.items())


def test_reorganize_album_already_in_place_skips(reorganize_lib: Library) -> None:
    with reorganize_lib.music_dir_context():
        outcome = reorg.reorganize_album(reorganize_lib, _album(reorganize_lib, "Discovery"))
    assert outcome.status == "skipped"


def test_reorganize_singleton_moves(reorganize_lib: Library) -> None:
    with reorganize_lib.music_dir_context():
        item = next(iter(reorganize_lib.items("singleton:true")))
        outcome = reorg.reorganize_singleton(reorganize_lib, item)
    assert outcome.status == "moved"


def test_reorganize_album_failure_is_caught(
    monkeypatch: pytest.MonkeyPatch, reorganize_lib: Library
) -> None:
    with reorganize_lib.music_dir_context():
        album = _album(reorganize_lib, "In Rainbows")

        def boom(*a: object, **k: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(type(album), "move", boom)
        outcome = reorg.reorganize_album(reorganize_lib, album)
    assert outcome.status == "failed"
    assert "disk full" in (outcome.error or "")


def test_reorganize_album_isolates_beets_filesystem_error(
    monkeypatch: pytest.MonkeyPatch, reorganize_lib: Library
) -> None:
    """beets raises FilesystemError (a HumanReadableError, NOT an OSError) for real
    move failures — permission denied, disk full, NAS I/O. The 'Never raises'
    adapter must catch it and record status='failed', or one bad album escapes and
    aborts the ENTIRE library-wide reorganize sweep."""
    from beets.util import FilesystemError

    with reorganize_lib.music_dir_context():
        album = _album(reorganize_lib, "In Rainbows")

        def boom(*a: object, **k: object) -> None:
            raise FilesystemError(OSError("permission denied"), "move", (b"/a", b"/b"))

        monkeypatch.setattr(type(album), "move", boom)
        outcome = reorg.reorganize_album(reorganize_lib, album)  # must not raise
    assert outcome.status == "failed"
    assert outcome.error


def test_reorganize_singleton_isolates_beets_filesystem_error(
    monkeypatch: pytest.MonkeyPatch, reorganize_lib: Library
) -> None:
    """Same FilesystemError isolation for the singleton path."""
    from beets.util import FilesystemError

    with reorganize_lib.music_dir_context():
        item = next(iter(reorganize_lib.items("singleton:true")))

        def boom(*a: object, **k: object) -> None:
            raise FilesystemError(OSError("disk full"), "move", (b"/a", b"/b"))

        monkeypatch.setattr(type(item), "move", boom)
        outcome = reorg.reorganize_singleton(reorganize_lib, item)  # must not raise
    assert outcome.status == "failed"
    assert outcome.error


def test_reorganize_album_missing_source_fails(reorganize_lib: Library) -> None:
    """A moving item whose file is gone at the DB path: beets 2.12 silently skips
    the move (models.py:1142 — no store, no exception). Verification must turn the
    unit into `failed` and name the file + cause. Other tracks may still move; we
    assert only status/error."""
    with reorganize_lib.music_dir_context():
        album = _album(reorganize_lib, "In Rainbows")
        victim = os.fsdecode(next(iter(album.items())).path)
        os.remove(victim)  # DB row untouched; on-disk file gone
        outcome = reorg.reorganize_album(reorganize_lib, album)
    assert outcome.status == "failed"
    assert "not found on disk" in (outcome.error or "")


def test_reorganize_album_destination_collision_fails(tmp_path: Path) -> None:
    """Two tracks share track#+title, so both compute the SAME destination. beets
    diverts the second to a `.1`-suffixed name (unique_path) — no exception, and the
    preview re-flags it forever. Verification must report `failed`."""
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))
    base = music / "junk" / "col"
    base.mkdir(parents=True, exist_ok=True)
    items = []
    for fname in ("01 Song.mp3", "01 Song other.mp3"):
        f = base / fname
        f.write_bytes(b"\x00")
        it = Item(album="Collide", albumartist="X", artist="X", title="Song", track=1, disc=1)
        it.path = os.fsencode(str(f))
        items.append(it)
    lib.add_album(items).store()

    with lib.music_dir_context():
        outcome = reorg.reorganize_album(lib, next(iter(lib.albums())))

    assert outcome.status == "failed"
    assert "already taken by another track" in (outcome.error or "")
    # Documents the ping-pong: the diverted track landed at the .1 name on disk.
    assert (music / "X" / "Collide" / "01 Song.1.mp3").exists()


# --- lyric sidecars follow the audio ------------------------------------------
#
# beets moves audio + album art only. MusicDrop's own .lrc/.txt sidecars (the
# files Plex actually reads) are named after the AUDIO STEM, so every move and
# every in-place rename orphans them: they stay in the vacated folder, the
# post-run orphan sweep classifies it as an audio-empty husk, and the lyrics go
# to Trash while the job reports success.


def _seed_flac_album(
    lib: Library,
    music: Path,
    *,
    folder: str,
    artist: str,
    album: str,
    titles: list[str],
) -> Album:
    """One album of REAL flac files under ``folder``, filenames a.flac/b.flac/...

    Real audio (copies of the silent.flac fixture) and a real ``lib.add_album`` so
    the move under test is beets' own, unmocked. The raw stems guarantee every
    track's destination filename differs from its current one.
    """
    base = music / folder
    base.mkdir(parents=True, exist_ok=True)
    items = []
    for i, title in enumerate(titles, start=1):
        f = base / f"{chr(ord('a') + i - 1)}.flac"
        shutil.copyfile(SAMPLE_FLAC, f)
        it = Item(album=album, albumartist=artist, artist=artist, title=title, track=i, disc=1)
        it.path = os.fsencode(str(f))
        items.append(it)
    return lib.add_album(items)


def _ir_lib(tmp_path: Path) -> tuple[Library, Path, Album]:
    """(lib, music_dir, album) with a mis-filed 3-track In Rainbows in junk/ir."""
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))
    album = _seed_flac_album(
        lib,
        music,
        folder="junk/ir",
        artist="Radiohead",
        album="In Rainbows",
        titles=["15 Step", "Bodysnatchers", "Nude"],
    )
    return lib, music, album


def test_reorganize_album_carries_lyric_sidecars(tmp_path: Path) -> None:
    """Folder move: each track's .lrc/.txt lands beside it under its NEW name."""
    lib, music, album = _ir_lib(tmp_path)
    src = music / "junk" / "ir"
    (src / "a.lrc").write_text("[00:01.00] step\n", encoding="utf-8")
    (src / "b.txt").write_text("bodysnatchers\n", encoding="utf-8")

    with lib.music_dir_context():
        outcome = reorg.reorganize_album(lib, album)

    assert outcome.status == "moved"
    dest = music / "Radiohead" / "In Rainbows"
    assert (dest / "01 15 Step.lrc").read_text(encoding="utf-8") == "[00:01.00] step\n"
    assert (dest / "02 Bodysnatchers.txt").read_text(encoding="utf-8") == "bodysnatchers\n"
    # Track 3 had no sidecar: none is invented for it.
    assert not (dest / "03 Nude.lrc").exists()
    assert not (dest / "03 Nude.txt").exists()
    # Vacated folder is gone, so the orphan sweep has no husk to trash.
    assert not src.exists()


def test_reorganize_album_rename_in_place_carries_sidecars(tmp_path: Path) -> None:
    """Same directory, filenames only: the sidecar must be renamed alongside."""
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))
    album = _seed_flac_album(
        lib,
        music,
        folder="Boards of Canada/Geogaddi",
        artist="Boards of Canada",
        album="Geogaddi",
        titles=["Ready Lets Go", "Music Is Math"],
    )
    base = music / "Boards of Canada" / "Geogaddi"
    (base / "a.lrc").write_text("[00:02.00] ready\n", encoding="utf-8")

    with lib.music_dir_context():
        outcome = reorg.reorganize_album(lib, album)

    assert outcome.status == "moved"
    assert (base / "01 Ready Lets Go.lrc").read_text(encoding="utf-8") == "[00:02.00] ready\n"
    assert not (base / "a.lrc").exists()
    # The album's own folder still holds its audio — the re-prune never eats a live dir.
    assert (base / "01 Ready Lets Go.flac").exists()


def test_reorganize_singleton_carries_lyric_sidecars(tmp_path: Path) -> None:
    """The singleton path moves sidecars too — both extensions when both exist."""
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))
    loose = music / "loose"
    loose.mkdir(parents=True)
    audio = loose / "z.flac"
    shutil.copyfile(SAMPLE_FLAC, audio)
    item = Item(artist="Aphex Twin", albumartist="Aphex Twin", title="Xtal", track=1)
    item.path = os.fsencode(str(audio))
    lib.add(item)
    (loose / "z.lrc").write_text("[00:03.00] xtal\n", encoding="utf-8")
    (loose / "z.txt").write_text("xtal plain\n", encoding="utf-8")

    with lib.music_dir_context():
        outcome = reorg.reorganize_singleton(lib, item)

    assert outcome.status == "moved"
    dest = music / "Non-Album" / "Aphex Twin"  # beets' built-in singleton: format
    assert (dest / "Xtal.lrc").read_text(encoding="utf-8") == "[00:03.00] xtal\n"
    assert (dest / "Xtal.txt").read_text(encoding="utf-8") == "xtal plain\n"
    assert not loose.exists()


def test_reorganize_album_sidecar_move_never_clobbers(tmp_path: Path) -> None:
    """A sidecar already at the destination wins; the source one is left in place
    (two different lyrics for what is now the same track name — losing either
    silently is worse than leaving one behind where the user can see it)."""
    lib, music, album = _ir_lib(tmp_path)
    src = music / "junk" / "ir"
    (src / "a.lrc").write_text("SOURCE\n", encoding="utf-8")
    dest = music / "Radiohead" / "In Rainbows"
    dest.mkdir(parents=True)
    (dest / "01 15 Step.lrc").write_text("KEEP\n", encoding="utf-8")

    with lib.music_dir_context():
        outcome = reorg.reorganize_album(lib, album)

    assert outcome.status == "moved"  # the audio move is unaffected
    assert (dest / "01 15 Step.lrc").read_text(encoding="utf-8") == "KEEP\n"
    assert (src / "a.lrc").read_text(encoding="utf-8") == "SOURCE\n"


def test_reorganize_album_sidecar_failure_does_not_fail_the_audio_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An OSError moving a sidecar is logged, never raised: the audio has already
    moved, so the unit's outcome must stay truthful about the audio."""
    lib, music, album = _ir_lib(tmp_path)
    src = music / "junk" / "ir"
    (src / "a.lrc").write_text("[00:01.00] step\n", encoding="utf-8")

    def boom(*a: object, **k: object) -> None:
        raise OSError("read-only file system")

    # beets' own util.move uses os.replace (+ copyfileobj), never shutil.move, so
    # this breaks ONLY the sidecar move.
    monkeypatch.setattr(shutil, "move", boom)

    with caplog.at_level(logging.WARNING, logger="app.beets.sidecars"):
        with lib.music_dir_context():
            outcome = reorg.reorganize_album(lib, album)

    assert outcome.status == "moved"
    assert (music / "Radiohead" / "In Rainbows" / "01 15 Step.flac").exists()
    assert (src / "a.lrc").exists()  # left behind, not destroyed
    assert "a.lrc" in caplog.text
    assert any(r.exc_info for r in caplog.records)  # the traceback reaches the logs


def test_reorganize_album_leaves_non_sidecar_files_alone(tmp_path: Path) -> None:
    """Only stem-matched .lrc/.txt travel: art, an unrelated sidecar and any other
    file stay exactly where they are."""
    lib, music, album = _ir_lib(tmp_path)
    src = music / "junk" / "ir"
    (src / "a.lrc").write_text("[00:01.00] step\n", encoding="utf-8")
    (src / "scan.jpg").write_bytes(b"\xff\xd8")
    (src / "unrelated.lrc").write_text("not a track here\n", encoding="utf-8")

    with lib.music_dir_context():
        outcome = reorg.reorganize_album(lib, album)

    assert outcome.status == "moved"
    dest = music / "Radiohead" / "In Rainbows"
    assert (dest / "01 15 Step.lrc").exists()
    assert (src / "scan.jpg").exists()
    assert (src / "unrelated.lrc").exists()
    assert not (dest / "scan.jpg").exists()
    assert not (dest / "unrelated.lrc").exists()


def test_reorganize_album_leaves_a_non_moving_items_sidecar_alone(tmp_path: Path) -> None:
    """A track beets silently skipped (source file gone) keeps its sidecar where it
    is, while its album-mates' sidecars still travel — the carry runs even when
    verification is about to report the unit failed."""
    lib, music, album = _ir_lib(tmp_path)
    src = music / "junk" / "ir"
    (src / "a.lrc").write_text("stranded\n", encoding="utf-8")
    (src / "b.lrc").write_text("travels\n", encoding="utf-8")
    os.remove(src / "a.flac")  # beets 2.13 skips this item's move, no exception

    with lib.music_dir_context():
        outcome = reorg.reorganize_album(lib, album)

    assert outcome.status == "failed"  # the missing file is reported, not hidden
    assert (src / "a.lrc").read_text(encoding="utf-8") == "stranded\n"
    dest = music / "Radiohead" / "In Rainbows"
    assert (dest / "02 Bodysnatchers.lrc").read_text(encoding="utf-8") == "travels\n"


def test_move_sidecars_same_path_is_a_noop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An item whose path did not change must not even look like a collision —
    a naive implementation would see the sidecar at its own destination and log a
    bogus 'destination exists' warning on every run."""
    from app.beets.sidecars import move_sidecars

    audio = tmp_path / "01 Song.flac"
    audio.write_bytes(b"\x00")
    (tmp_path / "01 Song.lrc").write_text("[00:01.00] la\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="app.beets.sidecars"):
        assert move_sidecars(str(audio), str(audio)) == []

    assert (tmp_path / "01 Song.lrc").exists()
    assert caplog.records == []


def test_reorganize_album_crash_midway_still_carries_moved_items_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Album.move moves+stores one item at a time, and beets commits the
    transaction even while an exception propagates — so items moved before a
    mid-album crash are permanently re-pathed and never re-enter ``pending`` on a
    later run. Their sidecars must be carried from the except path, or they are
    stranded for good and the orphan sweep eventually trashes them."""
    from beets import util as beets_util

    lib, music, album = _ir_lib(tmp_path)
    src = music / "junk" / "ir"
    for stem in ("a", "b", "c"):
        (src / f"{stem}.lrc").write_text(f"{stem}-lyrics\n", encoding="utf-8")

    real_move = beets_util.move
    calls = {"n": 0}

    def move_then_die(sour: bytes, dest: bytes, replace: bool = False) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise beets_util.FilesystemError(
                OSError(28, "No space left on device"), "move", (sour, dest)
            )
        real_move(sour, dest, replace)

    # Breaks the SECOND audio move only; sidecars go through shutil.move.
    monkeypatch.setattr(beets_util, "move", move_then_die)

    with lib.music_dir_context():
        outcome = reorg.reorganize_album(lib, album)

    assert outcome.status == "failed"  # the crash is reported, not hidden
    dest = music / "Radiohead" / "In Rainbows"
    moved_audio = [p for p in dest.glob("*.flac")] if dest.exists() else []
    assert len(moved_audio) == 1  # exactly one item moved before the crash
    # THE point: the moved item's sidecar followed it despite the crash...
    lrc = moved_audio[0].with_suffix(".lrc")
    assert lrc.exists()
    # ...and the unmoved items keep audio + sidecars intact at the old location.
    assert len(list(src.glob("*.flac"))) == 2
    assert len(list(src.glob("*.lrc"))) == 2


def test_reorganize_album_collision_diverted_file_keeps_its_lyrics(tmp_path: Path) -> None:
    """The carry keys off the ACTUAL landing path: when unique_path diverts the
    collision loser to `01 Song.1.mp3`, its own sidecar must land at
    `01 Song.1.lrc` — paired by content, whichever item won the plain name."""
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))
    base = music / "junk" / "col"
    base.mkdir(parents=True, exist_ok=True)
    audio_to_lyrics = {b"\x00A": "lyrics-A\n", b"\x00B": "lyrics-B\n"}
    items = []
    for fname, (audio_bytes, lyrics) in zip(
        ("01 Song.mp3", "01 Song other.mp3"), audio_to_lyrics.items(), strict=True
    ):
        f = base / fname
        f.write_bytes(audio_bytes)
        (base / fname).with_suffix(".lrc").write_text(lyrics, encoding="utf-8")
        it = Item(album="Collide", albumartist="X", artist="X", title="Song", track=1, disc=1)
        it.path = os.fsencode(str(f))
        items.append(it)
    lib.add_album(items).store()

    with lib.music_dir_context():
        outcome = reorg.reorganize_album(lib, next(iter(lib.albums())))

    assert outcome.status == "failed"  # the collision is still reported
    dest = music / "X" / "Collide"
    # Both audio files landed (one plain, one diverted) and each kept ITS lyrics.
    for audio_name, lrc_name in (
        ("01 Song.mp3", "01 Song.lrc"),
        ("01 Song.1.mp3", "01 Song.1.lrc"),
    ):
        audio_bytes = (dest / audio_name).read_bytes()
        assert (dest / lrc_name).read_text(encoding="utf-8") == audio_to_lyrics[audio_bytes]
    assert list(base.glob("*.lrc")) == []  # nothing stranded behind
