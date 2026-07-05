import json
import uuid
from pathlib import Path

from app.models.playlist import PendingTrack
from app.playlists import store
from app.playlists.store import StoredEntry


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
    assert [e.item_id for e in updated.entries] == [1, 2]
    again = store.add_tracks(tmp_path, p.id, track_ids=[3])
    assert again is not None
    assert [e.item_id for e in again.entries] == [1, 2, 3]


def test_add_tracks_at_position(tmp_path: Path) -> None:
    p = store.create_playlist(tmp_path, name="P")
    store.add_tracks(tmp_path, p.id, track_ids=[1, 2, 3])
    updated = store.add_tracks(tmp_path, p.id, track_ids=[9], position=1)
    assert updated is not None
    assert [e.item_id for e in updated.entries] == [1, 9, 2, 3]


def test_add_tracks_position_clamps(tmp_path: Path) -> None:
    p = store.create_playlist(tmp_path, name="P")
    store.add_tracks(tmp_path, p.id, track_ids=[1, 2])
    high = store.add_tracks(tmp_path, p.id, track_ids=[5], position=99)
    assert high is not None
    assert [e.item_id for e in high.entries] == [1, 2, 5]
    low = store.add_tracks(tmp_path, p.id, track_ids=[0], position=-3)
    assert low is not None
    assert [e.item_id for e in low.entries] == [0, 1, 2, 5]


def test_add_tracks_missing_playlist(tmp_path: Path) -> None:
    assert store.add_tracks(tmp_path, "0" * 32, track_ids=[1]) is None


def test_remove_entry_unknown_uid_is_noop(tmp_path: Path) -> None:
    p = store.create_playlist(tmp_path, name="P")
    record = store.add_tracks(tmp_path, p.id, track_ids=[1, 2])
    assert record is not None
    before = [e.item_id for e in record.entries]
    updated = store.remove_entry(tmp_path, p.id, "no-such-uid")
    assert updated is not None
    assert [e.item_id for e in updated.entries] == before  # absent uid -> unchanged


def test_set_entry_order_reorders_full_set(tmp_path: Path) -> None:
    p = store.create_playlist(tmp_path, name="P")
    record = store.add_tracks(tmp_path, p.id, track_ids=[1, 2, 3])
    assert record is not None
    u1, u2, u3 = (e.uid for e in record.entries)
    updated = store.set_entry_order(tmp_path, p.id, uids=[u3, u1, u2])
    assert updated is not None
    assert [e.item_id for e in updated.entries] == [3, 1, 2]


def test_entry_ops_on_missing_playlist_return_none(tmp_path: Path) -> None:
    missing = "0" * 32
    assert store.remove_entry(tmp_path, missing, "uid") is None
    assert store.set_entry_order(tmp_path, missing, uids=["uid"]) is None
    assert store.resolve_entry(tmp_path, missing, "uid", item_id=1) is None


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
    # Recording sync state is bookkeeping — it must NOT bump updated_at (else a
    # freshly-synced playlist would read "out of date").
    assert updated.updated_at == p.updated_at
    assert again.updated_at == p.updated_at


def test_patch_target_users_excludes_admin_and_dedupes() -> None:
    from app.models.playlist import PlaylistUpdateRequest

    body = PlaylistUpdateRequest(target_plex_users=["7", "admin", "7", "8"])
    assert body.target_plex_users == ["7", "8"]


def test_legacy_track_ids_migrate_to_entries_on_read(tmp_path: Path) -> None:
    record = store.create_playlist(tmp_path, name="Old")
    # Write a LEGACY-shaped record directly (pre-entries schema).
    raw = {
        "id": record.id,
        "name": "Old",
        "description": "",
        "track_ids": [7, 9, 7],
        "target_plex_users": [],
        "plex": {},
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
    (tmp_path / f"{record.id}.json").write_text(json.dumps(raw), encoding="utf-8")
    loaded = store.get_playlist(tmp_path, record.id)
    assert loaded is not None
    assert [e.item_id for e in loaded.entries] == [7, 9, 7]
    assert loaded.track_ids == []
    uids = [e.uid for e in loaded.entries]
    assert len(set(uids)) == 3  # every entry got its own uid (duplicates included)
    assert loaded.resolved_item_ids == [7, 9, 7]


def test_add_tracks_creates_uid_entries_at_position(tmp_path: Path) -> None:
    record = store.create_playlist(tmp_path, name="P")
    store.add_tracks(tmp_path, record.id, track_ids=[1, 2], position=None)
    record2 = store.add_tracks(tmp_path, record.id, track_ids=[3], position=1)
    assert record2 is not None
    assert [e.item_id for e in record2.entries] == [1, 3, 2]
    assert all(e.pending is None for e in record2.entries)


def test_remove_entry_by_uid_removes_only_that_entry(tmp_path: Path) -> None:
    created = store.create_playlist(tmp_path, name="P")
    record = store.add_tracks(tmp_path, created.id, track_ids=[5, 5], position=None)
    assert record is not None
    first_uid = record.entries[0].uid
    updated = store.remove_entry(tmp_path, created.id, first_uid)
    assert updated is not None
    assert [e.item_id for e in updated.entries] == [5]  # the duplicate survives


def test_set_entry_order_subset_reorders_and_drops(tmp_path: Path) -> None:
    created = store.create_playlist(tmp_path, name="P")
    record = store.add_tracks(tmp_path, created.id, track_ids=[1, 2, 3], position=None)
    assert record is not None
    u1, u2, _u3 = (e.uid for e in record.entries)
    updated = store.set_entry_order(tmp_path, created.id, uids=[u2, u1])
    assert updated is not None
    assert [e.item_id for e in updated.entries] == [2, 1]  # 3 dropped, order flipped


def test_resolve_entry_sets_item_and_clears_pending(tmp_path: Path) -> None:
    pending = PendingTrack(artist="A", title="T", source="line")
    entry = StoredEntry(uid=uuid.uuid4().hex, item_id=None, pending=pending)
    record = store.create_playlist(tmp_path, name="P", entries=[entry])
    updated = store.resolve_entry(tmp_path, record.id, entry.uid, item_id=42)
    assert updated is not None
    assert updated.entries[0].item_id == 42
    assert updated.entries[0].pending is None
    assert updated.entries[0].uid == entry.uid  # identity survives resolution
