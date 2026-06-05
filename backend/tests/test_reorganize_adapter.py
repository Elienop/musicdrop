# backend/tests/test_reorganize_adapter.py
from beets.library import Album, Library

from app.beets.reorganize import (
    _describe_album,
    _item_moves,
    album_label,
    collect_units,
    plan_reorganize,
)


def _album(lib: Library, name: str) -> Album:
    return next(a for a in lib.albums() if a.album == name)


def test_album_label(reorganize_lib: Library) -> None:
    with reorganize_lib.music_dir_context():
        assert album_label(_album(reorganize_lib, "In Rainbows")) == "Radiohead — In Rainbows"


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
