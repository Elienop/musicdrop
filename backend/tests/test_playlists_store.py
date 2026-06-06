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
