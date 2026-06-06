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


def test_get_rejects_non_uuid_ids(tmp_path: Path) -> None:
    # Anything that is not a 32-char lowercase-hex uuid is treated as missing.
    for bad_id in ["does-not-exist", "ABC", "../../etc/passwd", "/etc/passwd", ""]:
        assert store.get_playlist(tmp_path, bad_id) is None


def test_traversal_id_cannot_read_outside_dir(tmp_path: Path) -> None:
    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()
    secret = tmp_path / "secret.json"
    secret.write_text('{"id": "x"}', encoding="utf-8")
    # ../secret would escape playlists_dir if the id were trusted.
    assert store.get_playlist(playlists_dir, "../secret") is None


def test_traversal_id_cannot_delete_outside_dir(tmp_path: Path) -> None:
    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()
    victim = tmp_path / "victim.json"
    victim.write_text("{}", encoding="utf-8")
    assert store.delete_playlist(playlists_dir, "../victim") is False
    assert victim.exists()  # untouched
    # An absolute-path id is likewise rejected, not followed.
    assert store.delete_playlist(playlists_dir, str(victim.with_suffix(""))) is False
    assert victim.exists()


def test_add_tracks_appends(tmp_path: Path) -> None:
    p = store.create_playlist(tmp_path, name="P")
    updated = store.add_tracks(tmp_path, p.id, track_ids=[1, 2])
    assert updated is not None
    assert updated.track_ids == [1, 2]
    again = store.add_tracks(tmp_path, p.id, track_ids=[3])
    assert again is not None
    assert again.track_ids == [1, 2, 3]


def test_add_tracks_at_position(tmp_path: Path) -> None:
    p = store.create_playlist(tmp_path, name="P")
    store.add_tracks(tmp_path, p.id, track_ids=[1, 2, 3])
    updated = store.add_tracks(tmp_path, p.id, track_ids=[9], position=1)
    assert updated is not None
    assert updated.track_ids == [1, 9, 2, 3]


def test_add_tracks_position_clamps(tmp_path: Path) -> None:
    p = store.create_playlist(tmp_path, name="P")
    store.add_tracks(tmp_path, p.id, track_ids=[1, 2])
    high = store.add_tracks(tmp_path, p.id, track_ids=[5], position=99)
    assert high is not None
    assert high.track_ids == [1, 2, 5]
    low = store.add_tracks(tmp_path, p.id, track_ids=[0], position=-3)
    assert low is not None
    assert low.track_ids == [0, 1, 2, 5]


def test_add_tracks_missing_playlist(tmp_path: Path) -> None:
    assert store.add_tracks(tmp_path, "0" * 32, track_ids=[1]) is None


def test_remove_track_drops_all_occurrences(tmp_path: Path) -> None:
    p = store.create_playlist(tmp_path, name="P")
    store.add_tracks(tmp_path, p.id, track_ids=[1, 2, 1, 3, 1])
    updated = store.remove_track(tmp_path, p.id, 1)
    assert updated is not None
    assert updated.track_ids == [2, 3]


def test_remove_track_absent_is_noop(tmp_path: Path) -> None:
    p = store.create_playlist(tmp_path, name="P")
    store.add_tracks(tmp_path, p.id, track_ids=[1, 2])
    updated = store.remove_track(tmp_path, p.id, 99)
    assert updated is not None
    assert updated.track_ids == [1, 2]


def test_set_track_order_replaces(tmp_path: Path) -> None:
    p = store.create_playlist(tmp_path, name="P")
    store.add_tracks(tmp_path, p.id, track_ids=[1, 2, 3])
    updated = store.set_track_order(tmp_path, p.id, track_ids=[3, 1, 2])
    assert updated is not None
    assert updated.track_ids == [3, 1, 2]


def test_track_ops_on_missing_playlist_return_none(tmp_path: Path) -> None:
    missing = "0" * 32
    assert store.remove_track(tmp_path, missing, 1) is None
    assert store.set_track_order(tmp_path, missing, track_ids=[1]) is None


def test_set_plex_state_records_per_target(tmp_path: Path) -> None:
    from app.models.plex import PlexTargetState

    p = store.create_playlist(tmp_path, name="P")
    updated = store.set_plex_state(
        tmp_path,
        p.id,
        "admin",
        PlexTargetState(
            rating_key="218550", status="ok", missing=0, synced_at="2026-06-07T00:00:00+00:00"
        ),
    )
    assert updated is not None
    assert updated.plex["admin"].rating_key == "218550"
    assert updated.plex["admin"].status == "ok"
    # round-trips through disk
    reloaded = store.get_playlist(tmp_path, p.id)
    assert reloaded is not None
    assert reloaded.plex["admin"].rating_key == "218550"


def test_set_plex_state_missing_playlist(tmp_path: Path) -> None:
    from app.models.plex import PlexTargetState

    assert store.set_plex_state(tmp_path, "0" * 32, "admin", PlexTargetState()) is None


def test_update_sets_target_plex_users(tmp_path: Path) -> None:
    p = store.create_playlist(tmp_path, name="P")
    updated = store.update_playlist(tmp_path, p.id, target_plex_users=["1", "2"])
    assert updated is not None
    assert updated.target_plex_users == ["1", "2"]
    assert updated.updated_at >= p.updated_at  # changing targets is a real edit


def test_replace_plex_states_sets_whole_map(tmp_path: Path) -> None:
    from app.models.plex import PlexTargetState

    p = store.create_playlist(tmp_path, name="P")
    states = {
        "admin": PlexTargetState(rating_key="1", status="ok", synced_at="t"),
        "7": PlexTargetState(rating_key="2", status="partial", missing=1, synced_at="t"),
    }
    updated = store.replace_plex_states(tmp_path, p.id, states)
    assert updated is not None
    assert set(updated.plex) == {"admin", "7"}
    assert updated.plex["7"].status == "partial"
    # whole-map replace drops a prior target not in the new map
    again = store.replace_plex_states(
        tmp_path, p.id, {"admin": PlexTargetState(status="ok", synced_at="t")}
    )
    assert again is not None
    assert set(again.plex) == {"admin"}
