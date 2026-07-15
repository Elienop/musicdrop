# backend/tests/test_reorganize_adapter.py
import os
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
