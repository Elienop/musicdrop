from pathlib import Path

from app.playlists import store


def test_create_then_get_round_trip(tmp_path: Path) -> None:
    created = store.create_playlist(tmp_path, name="Jazz", description="smooth")
    assert created.id
    assert created.name == "Jazz"
    assert created.description == "smooth"
    assert created.track_ids == []
    assert created.target_plex_users == []
    assert created.created_at == created.updated_at

    loaded = store.get_playlist(tmp_path, created.id)
    assert loaded is not None
    assert loaded.id == created.id
    assert loaded.name == "Jazz"
    # On-disk file is named <id>.json
    assert (tmp_path / f"{created.id}.json").is_file()


def test_get_missing_returns_none(tmp_path: Path) -> None:
    assert store.get_playlist(tmp_path, "does-not-exist") is None


def test_create_makes_dir_lazily(tmp_path: Path) -> None:
    nested = tmp_path / "data" / "playlists"
    assert not nested.exists()
    store.create_playlist(nested, name="X")
    assert nested.is_dir()


def test_list_is_sorted_by_created_at(tmp_path: Path) -> None:
    a = store.create_playlist(tmp_path, name="A")
    b = store.create_playlist(tmp_path, name="B")
    ids = [p.id for p in store.list_playlists(tmp_path)]
    assert ids == [a.id, b.id]  # creation order (created_at ascending)


def test_list_empty_when_dir_absent(tmp_path: Path) -> None:
    assert store.list_playlists(tmp_path / "nope") == []


def test_list_skips_corrupt_files(tmp_path: Path) -> None:
    good = store.create_playlist(tmp_path, name="Good")
    (tmp_path / "garbage.json").write_text("{not json", encoding="utf-8")
    ids = [p.id for p in store.list_playlists(tmp_path)]
    assert ids == [good.id]


def test_update_changes_fields_and_touches_updated_at(tmp_path: Path) -> None:
    created = store.create_playlist(tmp_path, name="Old", description="x")
    updated = store.update_playlist(tmp_path, created.id, name="New", description="y")
    assert updated is not None
    assert updated.name == "New"
    assert updated.description == "y"
    assert updated.created_at == created.created_at
    assert updated.updated_at >= created.updated_at


def test_partial_update_leaves_other_fields(tmp_path: Path) -> None:
    created = store.create_playlist(tmp_path, name="Keep", description="desc")
    updated = store.update_playlist(tmp_path, created.id, name="Renamed")
    assert updated is not None
    assert updated.name == "Renamed"
    assert updated.description == "desc"


def test_update_missing_returns_none(tmp_path: Path) -> None:
    assert store.update_playlist(tmp_path, "missing", name="X") is None


def test_delete_returns_true_then_false(tmp_path: Path) -> None:
    created = store.create_playlist(tmp_path, name="Bye")
    assert store.delete_playlist(tmp_path, created.id) is True
    assert store.get_playlist(tmp_path, created.id) is None
    assert store.delete_playlist(tmp_path, created.id) is False
