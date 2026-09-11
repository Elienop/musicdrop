import hashlib
import json
import logging
import os
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.models.playlist import PendingTrack
from app.models.plex import PlexTargetState
from app.playlists import store
from app.playlists.store import StoredEntry, StoredPlaylist


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


def test_artwork_write_parent_dir_fsync_open_carries_o_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dir open in the tail of ``_write_artwork_atomic``.

    A flag pin: measured, a FIFO swapped in at that path makes the bare
    ``os.O_RDONLY`` open block FOREVER (still blocked after 2 s, no error), so a
    behavioural test hangs rather than failing. ``O_DIRECTORY`` fails a
    non-directory ENOTDIR in 34 us.
    """
    real_open = os.open
    opened: list[tuple[object, int]] = []

    def spy_open(path: object, flags: int, mode: int = 0o777, *args: object) -> int:
        if not flags & os.O_CREAT:
            opened.append((path, flags))
        return real_open(path, flags, mode, *args)  # type: ignore[arg-type]  # pass-through spy

    monkeypatch.setattr(os, "open", spy_open)
    store._write_artwork_atomic(tmp_path / "art" / "cover.jpg", b"\xff\xd8\xff")

    parent = [flags for path, flags in opened if Path(str(path)) == tmp_path / "art"]
    assert parent, "the parent directory was not fsynced through os.open"
    bare = [f"{flags:#o}" for flags in parent if not flags & os.O_DIRECTORY]
    assert not bare, f"parent-dir fsync open(s) without O_DIRECTORY: {bare}"


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


def test_legacy_uids_are_stable_across_reads(tmp_path: Path) -> None:
    """A legacy record must yield IDENTICAL uids on every read (no write-on-read).

    The migration validator mints deterministic uids, so two reads of the same
    on-disk file agree. Before that fix it minted a fresh ``uuid4`` per read, so
    a uid captured from one read 404'd on the next (remove/reorder/resolve).
    """
    record = store.create_playlist(tmp_path, name="Old")
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

    first = store.get_playlist(tmp_path, record.id)
    second = store.get_playlist(tmp_path, record.id)
    assert first is not None
    assert second is not None
    assert [e.uid for e in first.entries] == [e.uid for e in second.entries]


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


def _force_lost_update_interleave(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministically stage the classic read-modify-write lost update.

    Patches ``store._write_atomic`` so the FIRST mutator to reach the write is
    paused — after it has read — until the other mutator's whole mutation has
    landed, and only then writes its own (by now stale) copy.

    Under the store's process-wide ``_LOCK`` the second mutator cannot even
    start while the first holds it, so the pause times out harmlessly and the
    two mutations serialize: both survive. If the two mutators do NOT share one
    lock, they interleave exactly as staged and the stale copy silently drops
    the other's change. Every caller therefore asserts BOTH mutations survived,
    which passes only while both take the SAME lock.

    Assumes exactly two writes reach ``_write_atomic`` — do all seeding before
    calling this.
    """
    real_write = store._write_atomic
    other_landed = threading.Event()
    arrival = threading.Lock()
    reached = {"count": 0}

    def coordinated_write(path: Path, rec: StoredPlaylist) -> None:
        with arrival:
            is_first_writer = reached["count"] == 0
            reached["count"] += 1
        if is_first_writer:
            # Hold our write until the other mutation fully lands. Under the real
            # store lock the other thread is blocked on us instead, so this times
            # out (no deadlock) and we simply write first.
            other_landed.wait(timeout=2.0)
            real_write(path, rec)
        else:
            real_write(path, rec)
            other_landed.set()

    monkeypatch.setattr(store, "_write_atomic", coordinated_write)


def _run_both(first: Callable[[], object], second: Callable[[], object]) -> None:
    """Run two mutators on parallel threads and re-raise whatever either hit."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(first), pool.submit(second)]
        for future in futures:
            future.result()


def test_concurrent_mutations_do_not_lose_an_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two overlapping add_tracks must BOTH persist — no lost update."""
    record = store.create_playlist(tmp_path, name="Mix")
    pid = record.id

    _force_lost_update_interleave(monkeypatch)

    def add(track_id: int) -> Callable[[], object]:
        return lambda: store.add_tracks(tmp_path, pid, track_ids=[track_id])

    _run_both(add(101), add(202))

    final = store.get_playlist(tmp_path, pid)
    assert final is not None
    ids = final.resolved_item_ids
    assert 101 in ids, f"a concurrent mutation was lost: {ids}"
    assert 202 in ids, f"a concurrent mutation was lost: {ids}"


def test_set_artwork_round_trip(tmp_path: Path) -> None:
    created = store.create_playlist(tmp_path, name="P")
    data = b"\xff\xd8\xff\x00jpeg-bytes"
    updated = store.set_artwork(tmp_path, created.id, data, "jpg")
    assert updated is not None
    assert updated.artwork is not None
    assert updated.artwork.format == "jpg"
    assert updated.artwork.hash == hashlib.sha256(data).hexdigest()[:16]
    # File landed at the deterministic artwork path with the exact bytes.
    art = store.artwork_path(tmp_path, created.id, "jpg")
    assert art.is_file()
    assert art.read_bytes() == data
    # Adding artwork is a real edit — it must bump updated_at (drives Plex staleness).
    assert updated.updated_at > created.updated_at
    # Round-trips through disk.
    reloaded = store.get_playlist(tmp_path, created.id)
    assert reloaded is not None
    assert reloaded.artwork is not None
    assert reloaded.artwork.format == "jpg"


def test_set_artwork_format_replacement_removes_old_file(tmp_path: Path) -> None:
    created = store.create_playlist(tmp_path, name="P")
    store.set_artwork(tmp_path, created.id, b"jpeg", "jpg")
    jpg = store.artwork_path(tmp_path, created.id, "jpg")
    assert jpg.is_file()
    updated = store.set_artwork(tmp_path, created.id, b"png", "png")
    assert updated is not None
    assert updated.artwork is not None
    assert updated.artwork.format == "png"
    # The stale .jpg must be gone — otherwise it would be re-served if the format
    # ever flipped back to jpg.
    assert not jpg.exists()
    assert store.artwork_path(tmp_path, created.id, "png").is_file()


def test_set_artwork_unknown_id_returns_none_writes_no_file(tmp_path: Path) -> None:
    missing = "0" * 32
    assert store.set_artwork(tmp_path, missing, b"data", "jpg") is None
    assert not store.artwork_path(tmp_path, missing, "jpg").exists()


def test_delete_artwork_removes_file_clears_field_and_bumps(tmp_path: Path) -> None:
    created = store.create_playlist(tmp_path, name="P")
    with_art = store.set_artwork(tmp_path, created.id, b"jpeg", "jpg")
    assert with_art is not None
    art = store.artwork_path(tmp_path, created.id, "jpg")
    assert art.is_file()
    cleared = store.delete_artwork(tmp_path, created.id)
    assert cleared is not None
    assert cleared.artwork is None
    assert not art.exists()
    assert cleared.updated_at > with_art.updated_at


def test_delete_artwork_no_artwork_is_noop_returning_record(tmp_path: Path) -> None:
    created = store.create_playlist(tmp_path, name="P")
    result = store.delete_artwork(tmp_path, created.id)
    assert result is not None
    assert result.id == created.id
    assert result.artwork is None


def test_delete_artwork_unknown_id_returns_none(tmp_path: Path) -> None:
    assert store.delete_artwork(tmp_path, "0" * 32) is None


def test_delete_playlist_removes_artwork_file(tmp_path: Path) -> None:
    created = store.create_playlist(tmp_path, name="P")
    store.set_artwork(tmp_path, created.id, b"jpeg", "jpg")
    art = store.artwork_path(tmp_path, created.id, "jpg")
    assert art.is_file()
    assert store.delete_playlist(tmp_path, created.id) is True
    # No record survives to point at the file, so it must be swept too.
    assert not art.exists()


def test_legacy_record_without_artwork_key_loads_as_none(tmp_path: Path) -> None:
    record = store.create_playlist(tmp_path, name="Old")
    raw = {
        "id": record.id,
        "name": "Old",
        "description": "",
        "track_ids": [],
        "entries": [],
        "target_plex_users": [],
        "plex": {},
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
    (tmp_path / f"{record.id}.json").write_text(json.dumps(raw), encoding="utf-8")
    loaded = store.get_playlist(tmp_path, record.id)
    assert loaded is not None
    assert loaded.artwork is None


def test_merge_appends_source_rows_after_the_target_rows_in_source_order(tmp_path: Path) -> None:
    target = store.create_playlist(tmp_path, name="Keep")
    store.add_tracks(tmp_path, target.id, track_ids=[1, 2], position=None)
    source = store.create_playlist(tmp_path, name="Fold in")
    store.add_tracks(tmp_path, source.id, track_ids=[7, 8], position=None)

    outcome = store.merge_playlists(tmp_path, target.id, source.id, delete_source=False)

    assert outcome is not None
    assert outcome.added == 2
    assert outcome.skipped_duplicates == 0
    assert [e.item_id for e in outcome.playlist.entries] == [1, 2, 7, 8]
    reread = store.get_playlist(tmp_path, target.id)
    assert reread is not None
    assert [e.item_id for e in reread.entries] == [1, 2, 7, 8]


def test_merge_skips_a_source_row_whose_item_is_already_in_the_target(tmp_path: Path) -> None:
    target = store.create_playlist(tmp_path, name="Keep")
    store.add_tracks(tmp_path, target.id, track_ids=[1, 2], position=None)
    source = store.create_playlist(tmp_path, name="Fold in")
    store.add_tracks(tmp_path, source.id, track_ids=[2, 3], position=None)

    outcome = store.merge_playlists(tmp_path, target.id, source.id, delete_source=False)

    assert outcome is not None
    assert outcome.added == 1
    assert outcome.skipped_duplicates == 1
    assert [e.item_id for e in outcome.playlist.entries] == [1, 2, 3]


def test_merge_copies_a_pending_row_even_when_the_target_holds_the_same_text(
    tmp_path: Path,
) -> None:
    """THE ANTI-GUESSING GUARD (store half).

    Both playlists hold a pending row remembering the SAME artist and title.
    Neither has a library track behind it, so any dedupe here would be guessing
    on text - "Last Christmas" by Wham! and by Ariana Grande are different
    recordings a strict text match would happily collapse. Both rows must
    survive. If this test ever fails because the titles matched, merge has
    started dropping songs, which is the one thing it must never do.
    """
    target = store.create_playlist(
        tmp_path,
        name="Keep",
        entries=[
            StoredEntry(uid="t1", item_id=41),
            StoredEntry(
                uid="t2",
                pending=PendingTrack(artist="Wham!", title="Last Christmas", source="target line"),
            ),
        ],
    )
    source = store.create_playlist(
        tmp_path,
        name="Fold in",
        entries=[
            StoredEntry(
                uid="s1",
                pending=PendingTrack(artist="Wham!", title="Last Christmas", source="source line"),
            )
        ],
    )

    outcome = store.merge_playlists(tmp_path, target.id, source.id, delete_source=False)

    assert outcome is not None
    assert outcome.added == 1
    assert outcome.skipped_duplicates == 0
    sources = [e.pending.source for e in outcome.playlist.entries if e.pending is not None]
    assert sources == ["target line", "source line"]


def test_merge_copies_an_entry_with_an_id_the_target_lacks_keeping_that_id(
    tmp_path: Path,
) -> None:
    """An "unavailable" row is, at this layer, just an entry with an item id
    whose library track is gone. It must come across with that id intact so it
    can be re-pointed later."""
    target = store.create_playlist(tmp_path, name="Keep")
    source = store.create_playlist(
        tmp_path, name="Fold in", entries=[StoredEntry(uid="s1", item_id=999_001)]
    )

    outcome = store.merge_playlists(tmp_path, target.id, source.id, delete_source=False)

    assert outcome is not None
    assert outcome.added == 1
    assert [e.item_id for e in outcome.playlist.entries] == [999_001]


def test_merge_mints_fresh_uids_and_keeps_a_duplicated_source_row_addressable(
    tmp_path: Path,
) -> None:
    """Fresh uids for every copied row, and the dedupe snapshot never grows.

    The source lists ONE track twice. The set of ids already in the target is
    taken before copying and is not extended as rows land, so both rows come
    across - and each gets its own uuid4 uid, keeping "duplicate tracks are
    individually addressable" true and staying clear of legacy-<i>-<id> uids.
    """
    target = store.create_playlist(tmp_path, name="Keep")
    source = store.create_playlist(tmp_path, name="Fold in")
    seeded = store.add_tracks(tmp_path, source.id, track_ids=[5, 5], position=None)
    assert seeded is not None
    source_uids = {e.uid for e in seeded.entries}

    outcome = store.merge_playlists(tmp_path, target.id, source.id, delete_source=False)

    assert outcome is not None
    assert outcome.added == 2
    assert outcome.skipped_duplicates == 0
    assert [e.item_id for e in outcome.playlist.entries] == [5, 5]
    new_uids = [e.uid for e in outcome.playlist.entries]
    assert len(set(new_uids)) == 2
    assert not (set(new_uids) & source_uids)
    survivor = store.get_playlist(tmp_path, source.id)
    assert survivor is not None
    assert {e.uid for e in survivor.entries} == source_uids


def test_merge_does_not_inherit_artwork_or_plex_targets(tmp_path: Path) -> None:
    target = store.create_playlist(tmp_path, name="Keep")
    store.update_playlist(tmp_path, target.id, target_plex_users=["u-keep"])
    source = store.create_playlist(tmp_path, name="Fold in")
    store.update_playlist(tmp_path, source.id, target_plex_users=["u-source"])
    store.set_artwork(tmp_path, source.id, b"\x89PNG\r\n\x1a\n", "png")

    outcome = store.merge_playlists(tmp_path, target.id, source.id, delete_source=False)

    assert outcome is not None
    # Both belong to the playlist you are KEEPING, not to its contents -
    # inheriting either would push a merged playlist to accounts nobody chose.
    assert outcome.playlist.artwork is None
    assert outcome.playlist.target_plex_users == ["u-keep"]


def test_merge_bumps_updated_at_and_leaves_plex_state_alone(tmp_path: Path) -> None:
    target = store.create_playlist(tmp_path, name="Keep")
    store.set_plex_state(
        tmp_path,
        target.id,
        "admin",
        PlexTargetState(
            rating_key="7", status="ok", missing=0, synced_at="2020-01-01T00:00:00+00:00"
        ),
    )
    before = store.get_playlist(tmp_path, target.id)
    assert before is not None
    source = store.create_playlist(tmp_path, name="Fold in")
    store.add_tracks(tmp_path, source.id, track_ids=[9], position=None)

    outcome = store.merge_playlists(tmp_path, target.id, source.id, delete_source=False)

    assert outcome is not None
    assert outcome.playlist.updated_at > before.updated_at
    # Plex bookkeeping is untouched: a stale state correctly reads out of date.
    assert outcome.playlist.plex["admin"].rating_key == "7"
    assert outcome.playlist.plex["admin"].synced_at == "2020-01-01T00:00:00+00:00"
    # The editor's rule (updated_at > synced_at => out of date) now fires.
    assert outcome.playlist.updated_at > "2020-01-01T00:00:00+00:00"


def test_merge_delete_source_removes_the_source_record_and_keeps_the_target(
    tmp_path: Path,
) -> None:
    target = store.create_playlist(tmp_path, name="Keep")
    source = store.create_playlist(tmp_path, name="Fold in")
    store.add_tracks(tmp_path, source.id, track_ids=[3], position=None)
    store.set_artwork(tmp_path, source.id, b"\x89PNG\r\n\x1a\n", "png")

    outcome = store.merge_playlists(tmp_path, target.id, source.id, delete_source=True)

    assert outcome is not None
    assert outcome.source_deleted is True
    assert store.get_playlist(tmp_path, source.id) is None
    assert not store.artwork_path(tmp_path, source.id, "png").exists()
    kept = store.get_playlist(tmp_path, target.id)
    assert kept is not None
    assert [e.item_id for e in kept.entries] == [3]


def test_merge_without_delete_source_leaves_both_records(tmp_path: Path) -> None:
    target = store.create_playlist(tmp_path, name="Keep")
    source = store.create_playlist(tmp_path, name="Fold in")
    store.add_tracks(tmp_path, source.id, track_ids=[3], position=None)

    outcome = store.merge_playlists(tmp_path, target.id, source.id, delete_source=False)

    assert outcome is not None
    assert outcome.source_deleted is False
    survivor = store.get_playlist(tmp_path, source.id)
    assert survivor is not None
    assert [e.item_id for e in survivor.entries] == [3]


def test_merge_with_a_missing_record_returns_none_and_writes_nothing(tmp_path: Path) -> None:
    real = store.create_playlist(tmp_path, name="Keep")
    store.add_tracks(tmp_path, real.id, track_ids=[1], position=None)
    before = store.get_playlist(tmp_path, real.id)
    assert before is not None

    assert store.merge_playlists(tmp_path, real.id, "0" * 32, delete_source=True) is None
    assert store.merge_playlists(tmp_path, "0" * 32, real.id, delete_source=True) is None
    # A non-hex id never reaches the filesystem (the _VALID_ID guard).
    assert store.merge_playlists(tmp_path, real.id, "../../etc/passwd", delete_source=True) is None

    after = store.get_playlist(tmp_path, real.id)
    assert after is not None
    assert [e.item_id for e in after.entries] == [1]
    assert after.updated_at == before.updated_at


def test_merge_and_a_concurrent_target_mutation_both_survive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Merge must take the SAME process-wide lock as every other mutator.

    Merge's whole atomicity claim is that both reads, the target write and the
    optional source removal happen inside ONE ``_LOCK`` acquisition, so nothing
    can interleave against either record mid-merge. A merge holding a lock only
    IT can see would satisfy every other test in this file while leaving that
    claim false, so this pins the lock's identity rather than its presence: a
    merge and an ``add_tracks`` on the SAME target are forced into the classic
    read-modify-write interleave (see ``_force_lost_update_interleave``), and
    the merged row and the added row must both be on disk afterwards. Give
    ``merge_playlists`` a private ``threading.Lock()`` and one of them is
    silently dropped.
    """
    target = store.create_playlist(tmp_path, name="Keep")
    source = store.create_playlist(tmp_path, name="Fold in")
    store.add_tracks(tmp_path, source.id, track_ids=[7], position=None)

    _force_lost_update_interleave(monkeypatch)

    _run_both(
        lambda: store.merge_playlists(tmp_path, target.id, source.id, delete_source=False),
        lambda: store.add_tracks(tmp_path, target.id, track_ids=[99]),
    )

    final = store.get_playlist(tmp_path, target.id)
    assert final is not None
    ids = final.resolved_item_ids
    assert 7 in ids, f"the merged row was dropped by a concurrent mutation: {ids}"
    assert 99 in ids, f"the concurrent mutation was dropped by the merge: {ids}"


def test_merge_with_an_unparseable_source_returns_none_and_leaves_the_target_alone(
    tmp_path: Path,
) -> None:
    """A source record that exists but does not parse is NOT an error.

    ``get_playlist`` swallows the validation ``ValueError`` and returns
    ``None``, so a corrupt source is indistinguishable from an absent one:
    ``merge_playlists`` returns ``None`` (the caller owns the 404) instead of
    raising, and the target is left completely untouched — no appended rows and
    no ``updated_at`` bump, because both records are read before anything is
    written. Pinned because the behaviour is load-bearing and an early draft of
    the design stated it backwards.
    """
    target = store.create_playlist(tmp_path, name="Keep")
    store.add_tracks(tmp_path, target.id, track_ids=[1], position=None)
    before = store.get_playlist(tmp_path, target.id)
    assert before is not None
    source = store.create_playlist(tmp_path, name="Fold in")
    # Valid JSON, wrong shape - "entries" is a string and the required
    # timestamps are absent - so model_validate_json raises the pydantic
    # ValidationError (a ValueError) that get_playlist turns into None.
    (tmp_path / f"{source.id}.json").write_text('{"entries": "not a list"}', encoding="utf-8")

    outcome = store.merge_playlists(tmp_path, target.id, source.id, delete_source=True)

    assert outcome is None
    after = store.get_playlist(tmp_path, target.id)
    assert after is not None
    assert [e.item_id for e in after.entries] == [1]
    assert after.updated_at == before.updated_at
    # A failed merge must not delete the source either, corrupt or not.
    assert (tmp_path / f"{source.id}.json").is_file()


# --- Lone-surrogate round-trip (SINGLE SINK RULE, mirroring app/bank/store) ---


def test_round_trip_lossless_with_lone_surrogates(tmp_path: Path) -> None:
    """The store must round-trip lone surrogates LOSSLESSLY (no U+FFFD).

    A playlist name or a pending track's artist/title can carry a lone surrogate
    (a client can deliver one with a `"\\udce9"` JSON escape, or an m3u line can
    decode to one). The store is the single sink and must preserve the exact code
    points on disk. ``model_dump_json`` rejects them (``PydanticSerializationError``
    -> a 500 on every mutation), so the sink goes through stdlib json (see the
    module's ``_write_atomic`` docstring); the wire is responsible for the U+FFFD
    degradation on the way out, NEVER the store.
    """
    record = store.create_playlist(
        tmp_path,
        name="Caf\udce9",
        entries=[
            StoredEntry(
                uid=uuid.uuid4().hex,
                pending=PendingTrack(artist="A\ud800r", title="T\udce9tle", source="line"),
            )
        ],
    )
    # Lossless through BOTH read sites (get_playlist and list_playlists).
    reloaded = store.get_playlist(tmp_path, record.id)
    assert reloaded is not None
    assert reloaded.name == "Caf\udce9"
    assert reloaded.entries[0].pending is not None
    assert reloaded.entries[0].pending.artist == "A\ud800r"
    assert reloaded.entries[0].pending.title == "T\udce9tle"
    listed = store.list_playlists(tmp_path)
    assert [p.id for p in listed] == [record.id]
    assert listed[0].name == "Caf\udce9"
    # No U+FFFD anywhere in the parsed values — lossless, not scrubbed.
    assert "\ufffd" not in reloaded.name
    assert "\ufffd" not in reloaded.entries[0].pending.artist
    assert "\ufffd" not in reloaded.entries[0].pending.title


def test_round_trip_survives_a_rename_with_a_lone_surrogate(tmp_path: Path) -> None:
    """A non-create mutation (update_playlist) through the SAME sink is lossless.

    create_playlist is the 500 the user hit; rename/update share ``_write_atomic``,
    so pin a mutation too: the reloaded name keeps its exact code points.
    """
    record = store.create_playlist(tmp_path, name="Old")
    updated = store.update_playlist(tmp_path, record.id, name="New\udce9")
    assert updated is not None
    reloaded = store.get_playlist(tmp_path, record.id)
    assert reloaded is not None
    assert reloaded.name == "New\udce9"
    assert "\ufffd" not in reloaded.name


def test_non_finite_floats_persist_as_null_like_the_rust_sink_did(tmp_path: Path) -> None:
    """A non-finite duration must land on disk as ``null``, never ``Infinity``.

    ``json.loads("1e400")`` returns a real ``inf`` and pydantic admits it into
    ``float | None``, so a request body can put one in a pending track. The Rust
    sink wrote ``null`` for it (``ser_json_inf_nan``); the stdlib sink must do
    the same — its default writes the bare token ``Infinity``, which is not JSON
    and 500s the detail response forever once on disk (Starlette renders with
    ``allow_nan=False``, and the wire net only catches ``UnicodeEncodeError``).
    """
    record = store.create_playlist(
        tmp_path,
        name="P",
        entries=[
            StoredEntry(
                uid=uuid.uuid4().hex,
                pending=PendingTrack(
                    artist="A", title="T", source="line", duration_seconds=float("inf")
                ),
            ),
            StoredEntry(
                uid=uuid.uuid4().hex,
                pending=PendingTrack(
                    artist="B", title="U", source="line", duration_seconds=float("nan")
                ),
            ),
        ],
    )
    raw = (tmp_path / f"{record.id}.json").read_text(encoding="utf-8")
    assert "Infinity" not in raw
    assert "NaN" not in raw
    # Strict RFC-JSON parse: the non-finite constants would trip parse_constant.
    json.loads(raw, parse_constant=lambda token: pytest.fail(f"non-JSON token {token!r} on disk"))
    reloaded = store.get_playlist(tmp_path, record.id)
    assert reloaded is not None
    assert reloaded.entries[0].pending is not None
    assert reloaded.entries[0].pending.duration_seconds is None
    assert reloaded.entries[1].pending is not None
    assert reloaded.entries[1].pending.duration_seconds is None


def test_legacy_sink_file_still_loads_through_new_read_path(tmp_path: Path) -> None:
    """A record written by the OLD sink (model_dump_json) still loads identically.

    The old sink wrote literal UTF-8 (a real "é", non-ASCII text). The new read
    path (``json.loads`` -> ``model_validate``) must read such a file back with
    the exact same value — backward-compatible with earlier rows.
    """
    record = store.create_playlist(tmp_path, name="Café")
    # Emulate a file produced by the OLD ``model_dump_json`` sink, with non-ASCII.
    (tmp_path / f"{record.id}.json").write_text(
        StoredPlaylist(id=record.id, name="Café", created_at="t", updated_at="t").model_dump_json(
            indent=2
        ),
        encoding="utf-8",
    )
    loaded = store.get_playlist(tmp_path, record.id)
    assert loaded is not None
    assert loaded.name == "Café"
    listed = store.list_playlists(tmp_path)
    assert [p.name for p in listed] == ["Café"]


def test_invalid_json_get_returns_none(tmp_path: Path) -> None:
    """Corrupt-file posture preserved: get_playlist on invalid JSON returns None.

    ``json.JSONDecodeError`` is a ``ValueError`` subclass (as the bank noted), so
    the same ``except ValueError`` guard that used to catch the pydantic path still
    swallows the new stdlib path.
    """
    record = store.create_playlist(tmp_path, name="P")
    (tmp_path / f"{record.id}.json").write_text("{ not json", encoding="utf-8")
    assert store.get_playlist(tmp_path, record.id) is None


def test_invalid_schema_get_returns_none(tmp_path: Path) -> None:
    """Corrupt-file posture preserved: get_playlist on a valid-JSON-wrong-schema
    file returns None (pydantic ``ValidationError`` is a ``ValueError`` subclass).
    """
    record = store.create_playlist(tmp_path, name="P")
    (tmp_path / f"{record.id}.json").write_text('{"entries": "not a list"}', encoding="utf-8")
    assert store.get_playlist(tmp_path, record.id) is None


def test_invalid_json_list_skips_file(tmp_path: Path) -> None:
    """Corrupt-file posture preserved: list_playlists still skips unreadable files."""
    good = store.create_playlist(tmp_path, name="Good")
    (tmp_path / "garbage.json").write_text("{ not json", encoding="utf-8")
    assert [p.id for p in store.list_playlists(tmp_path)] == [good.id]


# Non-UTF-8 bytes: the 500 class the "{not json" fixtures miss — those are
# valid UTF-8 and only exercise JSONDecodeError. UnicodeDecodeError is a
# ValueError, NOT an OSError, so an OSError-only read guard lets it 500.
_NON_UTF8 = b"\x00\xe9\xff"


def test_non_utf8_get_returns_none(tmp_path: Path) -> None:
    """One read posture: a non-UTF-8 record reads as ABSENT, never a 500."""
    record = store.create_playlist(tmp_path, name="P")
    (tmp_path / f"{record.id}.json").write_bytes(_NON_UTF8)
    assert store.get_playlist(tmp_path, record.id) is None


def test_non_utf8_list_skips_and_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Loud skip: the file vanishes from the list AND the skip is logged,
    naming the file and the reason — a silent skip reads as 'deleted'."""
    good = store.create_playlist(tmp_path, name="Good")
    (tmp_path / "corrupt.json").write_bytes(_NON_UTF8)
    with caplog.at_level(logging.WARNING, logger="app.playlists.store"):
        ids = [p.id for p in store.list_playlists(tmp_path)]
    assert ids == [good.id]
    warnings = [
        r
        for r in caplog.records
        if r.name == "app.playlists.store" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "corrupt.json" in warnings[0].getMessage()
