import json
import os
from pathlib import Path

import pytest
from beets.library import Item
from fastapi.testclient import TestClient

from app.api.playlists import get_playlists_dir
from app.beets.library import LibraryHandle, _require_id
from app.models.playlist import PendingTrack, Playlist, PlaylistDetail
from app.playlists import store
from app.playlists.store import StoredEntry
from app.plex import sync as plex_sync
from tests.plex_fakes import FakePlaylist, FakeServer, FakeTrack


def _dir() -> Path:
    """The owned-playlist store dir the API resolves from settings.

    The ``client``/``beets_library`` fixtures monkeypatch ``settings.beets_dir``
    at the per-test ``tmp_path``, so this returns the same directory the
    endpoints read/write — letting a test seed the store directly."""
    return get_playlists_dir()


def test_list_empty(client: TestClient) -> None:
    r = client.get("/api/playlists")
    assert r.status_code == 200
    assert r.json() == []


def test_create_then_list_and_get(client: TestClient) -> None:
    r = client.post("/api/playlists", json={"name": "Favorites"})
    assert r.status_code == 200
    created = r.json()
    assert created["name"] == "Favorites"
    assert created["track_count"] == 0
    assert created["target_plex_users"] == []
    pid = created["id"]

    r = client.get("/api/playlists")
    assert r.status_code == 200
    assert [p["id"] for p in r.json()] == [pid]

    r = client.get(f"/api/playlists/{pid}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["id"] == pid
    assert detail["tracks"] == []  # was track_ids in Chunk 1


def test_create_blank_name_422(client: TestClient) -> None:
    r = client.post("/api/playlists", json={"name": "   "})
    assert r.status_code == 422


def test_get_missing_404(client: TestClient) -> None:
    r = client.get("/api/playlists/nope")
    assert r.status_code == 404


def test_patch_rename(client: TestClient) -> None:
    pid = client.post("/api/playlists", json={"name": "Old"}).json()["id"]
    r = client.patch(f"/api/playlists/{pid}", json={"name": "New"})
    assert r.status_code == 200
    assert r.json()["name"] == "New"


def test_patch_blank_name_422(client: TestClient) -> None:
    pid = client.post("/api/playlists", json={"name": "Old"}).json()["id"]
    r = client.patch(f"/api/playlists/{pid}", json={"name": "  "})
    assert r.status_code == 422


def test_patch_missing_404(client: TestClient) -> None:
    r = client.patch("/api/playlists/nope", json={"name": "X"})
    assert r.status_code == 404


def test_delete_then_404(client: TestClient) -> None:
    pid = client.post("/api/playlists", json={"name": "Bye"}).json()["id"]
    r = client.delete(f"/api/playlists/{pid}")
    assert r.status_code == 204
    assert client.get(f"/api/playlists/{pid}").status_code == 404
    assert client.delete(f"/api/playlists/{pid}").status_code == 404


def test_malformed_id_is_404_not_500(client: TestClient) -> None:
    # A non-uuid {playlist_id} is rejected as not-found (never touches the FS).
    assert client.get("/api/playlists/not-a-uuid").status_code == 404
    assert client.patch("/api/playlists/not-a-uuid", json={"name": "X"}).status_code == 404
    assert client.delete("/api/playlists/not-a-uuid").status_code == 404


def _add_track(handle: LibraryHandle, title: str, *, length: float | None = None) -> int:
    music = handle.lib.directory  # bytes
    folder = os.path.join(os.fsdecode(music), "Seed")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{title}.flac")
    with open(path, "wb") as fh:
        fh.write(b"\x00")
    item = Item(album="Seed", albumartist="Art", artist="Art", title=title, track=1)
    if length is not None:
        item.length = length
    item.path = os.fsencode(path)
    handle.lib.add(item)
    return _require_id(item.id)


def test_add_tracks_then_detail_resolves(client: TestClient, beets_library: LibraryHandle) -> None:
    t1 = _add_track(beets_library, "Alpha")
    t2 = _add_track(beets_library, "Beta")
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]

    r = client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1, t2]})
    assert r.status_code == 200
    tracks = r.json()["tracks"]
    assert [t["title"] for t in tracks] == ["Alpha", "Beta"]
    assert all(t["available"] for t in tracks)

    # track_count on the summary reflects membership
    summary = client.get("/api/playlists").json()[0]
    assert summary["track_count"] == 2


def test_add_unavailable_track(client: TestClient, beets_library: LibraryHandle) -> None:
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    r = client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [424242]})
    assert r.status_code == 200
    track = r.json()["tracks"][0]
    assert track["available"] is False
    assert track["id"] == 424242


def test_add_tracks_empty_422(client: TestClient) -> None:
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    r = client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": []})
    assert r.status_code == 422


def test_add_tracks_missing_playlist_404(client: TestClient) -> None:
    r = client.post(f"/api/playlists/{'0' * 32}/tracks", json={"track_ids": [1]})
    assert r.status_code == 404


def test_remove_track(client: TestClient, beets_library: LibraryHandle) -> None:
    t1 = _add_track(beets_library, "Alpha")
    t2 = _add_track(beets_library, "Beta")
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1, t2]})
    loaded = store.get_playlist(_dir(), pid)
    assert loaded is not None
    uid1 = loaded.entries[0].uid
    r = client.delete(f"/api/playlists/{pid}/entries/{uid1}")
    assert r.status_code == 200
    assert [t["id"] for t in r.json()["tracks"]] == [t2]


def test_reorder_tracks(client: TestClient, beets_library: LibraryHandle) -> None:
    t1 = _add_track(beets_library, "Alpha")
    t2 = _add_track(beets_library, "Beta")
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1, t2]})
    loaded = store.get_playlist(_dir(), pid)
    assert loaded is not None
    u1, u2 = (e.uid for e in loaded.entries)
    r = client.put(f"/api/playlists/{pid}/tracks", json={"entry_uids": [u2, u1]})
    assert r.status_code == 200
    assert [t["id"] for t in r.json()["tracks"]] == [t2, t1]


def test_reorder_missing_playlist_404(client: TestClient) -> None:
    r = client.put(f"/api/playlists/{'0' * 32}/tracks", json={"entry_uids": ["u1"]})
    assert r.status_code == 404


def test_remove_track_missing_playlist_404(client: TestClient) -> None:
    r = client.delete(f"/api/playlists/{'0' * 32}/entries/u1")
    assert r.status_code == 404


def test_reorder_empty_clears_playlist(client: TestClient, beets_library: LibraryHandle) -> None:
    t1 = _add_track(beets_library, "Alpha")
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1]})
    # Reorder is a full replacement; an empty list is a valid "clear".
    r = client.put(f"/api/playlists/{pid}/tracks", json={"entry_uids": []})
    assert r.status_code == 200
    assert r.json()["tracks"] == []


def test_add_too_many_tracks_422(client: TestClient) -> None:
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    r = client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": list(range(10_001))})
    assert r.status_code == 422


def test_detail_carries_uids_pending_and_counts(client: TestClient, tmp_path: Path) -> None:
    record = store.create_playlist(
        _dir(),
        name="Mix",
        entries=[StoredEntry(uid="u1", pending=PendingTrack(title="Ghost", source="x"))],
    )
    body = client.get(f"/api/playlists/{record.id}").json()
    assert body["track_count"] == 0
    assert body["pending_count"] == 1
    assert body["tracks"][0]["uid"] == "u1"
    assert body["tracks"][0]["pending"] is True
    assert body["tracks"][0]["id"] is None


def test_reorder_takes_entry_uids_subset(client: TestClient, tmp_path: Path) -> None:
    record = store.create_playlist(_dir(), name="P")
    store.add_tracks(_dir(), record.id, track_ids=[1, 2, 3])
    loaded = store.get_playlist(_dir(), record.id)
    assert loaded is not None
    u1, _u2, u3 = (e.uid for e in loaded.entries)
    r = client.put(f"/api/playlists/{record.id}/tracks", json={"entry_uids": [u3, u1]})
    assert r.status_code == 200
    reread = store.get_playlist(_dir(), record.id)
    assert reread is not None
    assert [e.uid for e in reread.entries] == [u3, u1]
    # unknown uid -> 422, playlist untouched
    r = client.put(f"/api/playlists/{record.id}/tracks", json={"entry_uids": ["nope"]})
    assert r.status_code == 422
    still = store.get_playlist(_dir(), record.id)
    assert still is not None
    assert [e.uid for e in still.entries] == [u3, u1]
    # duplicate uid -> 422
    r = client.put(f"/api/playlists/{record.id}/tracks", json={"entry_uids": [u1, u1]})
    assert r.status_code == 422


def test_remove_entry_endpoint(client: TestClient, tmp_path: Path) -> None:
    record = store.create_playlist(_dir(), name="P")
    store.add_tracks(_dir(), record.id, track_ids=[1])
    loaded = store.get_playlist(_dir(), record.id)
    assert loaded is not None
    uid = loaded.entries[0].uid
    assert client.delete(f"/api/playlists/{record.id}/entries/{uid}").status_code == 200
    assert client.delete(f"/api/playlists/{record.id}/entries/{uid}").status_code == 404


def test_resolve_entry_endpoint_validates_item(
    client: TestClient, beets_library: LibraryHandle, tmp_path: Path
) -> None:
    real_id = _add_track(beets_library, "Real")
    record = store.create_playlist(
        _dir(), name="P", entries=[StoredEntry(uid="u1", pending=PendingTrack(source="x"))]
    )
    # 422: not a library item.
    assert (
        client.patch(f"/api/playlists/{record.id}/entries/u1", json={"item_id": 424242}).status_code
        == 422
    )
    # 404: unknown entry uid (entry-existence is checked before item validity).
    assert (
        client.patch(f"/api/playlists/{record.id}/entries/zz", json={"item_id": 1}).status_code
        == 404
    )
    # Happy path: the pending row resolves to the real item, keeping its uid.
    r = client.patch(f"/api/playlists/{record.id}/entries/u1", json={"item_id": real_id})
    assert r.status_code == 200
    track = r.json()["tracks"][0]
    assert track["uid"] == "u1"
    assert track["id"] == real_id
    assert track["pending"] is False
    assert track["available"] is True


def test_resolve_entry_repoints_an_already_resolved_row_in_place(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """A RESOLVED row can be re-pointed: same slot, new library track.

    This is what lets the UI offer "Match" on a row that is unavailable, or
    resolved but missing from Plex - the endpoint has no pending-only guard and
    store.resolve_entry re-points in place ("replace track, keep position").
    Nothing about that is new; this test pins it so the UI change in this PR
    cannot be undone by a backend tightening.
    """
    first = _add_track(beets_library, "First")
    second = _add_track(beets_library, "Second")
    third = _add_track(beets_library, "Third")
    record = store.create_playlist(
        _dir(),
        name="P",
        entries=[
            StoredEntry(uid="u1", item_id=first),
            StoredEntry(uid="u2", item_id=second),
        ],
    )

    r = client.patch(f"/api/playlists/{record.id}/entries/u2", json={"item_id": third})

    assert r.status_code == 200
    tracks = r.json()["tracks"]
    # The slot kept its position (still second) and its uid...
    assert [t["uid"] for t in tracks] == ["u1", "u2"]
    # ...and now points at a different library track - replaced, not appended.
    assert [t["id"] for t in tracks] == [first, third]
    assert tracks[1]["title"] == "Third"
    assert tracks[1]["pending"] is False
    assert tracks[1]["available"] is True


def test_legacy_record_entry_delete_by_uid(client: TestClient) -> None:
    """End-to-end proof: a legacy on-disk record's detail uids are addressable.

    GET detail exposes per-slot uids; DELETE by one of those uids must land 200
    (the DELETE handler re-reads the file, and deterministic legacy uids survive
    that re-read), not 404 as it did while uids were minted randomly per read.
    """
    record = store.create_playlist(_dir(), name="Old")
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
    (_dir() / f"{record.id}.json").write_text(json.dumps(raw), encoding="utf-8")

    detail = client.get(f"/api/playlists/{record.id}").json()
    uid = detail["tracks"][0]["uid"]
    r = client.delete(f"/api/playlists/{record.id}/entries/{uid}")
    assert r.status_code == 200
    assert [t["id"] for t in r.json()["tracks"]] == [9, 7]  # first slot removed


def test_export_and_sync_feed_from_resolved_entries_only(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    t1 = _add_track(beets_library, "Alpha")
    t2 = _add_track(beets_library, "Gamma")
    record = store.create_playlist(
        _dir(),
        name="Mix",
        entries=[
            StoredEntry(uid="u1", item_id=t1),
            StoredEntry(uid="u2", pending=PendingTrack(title="Ghost", source="x")),
            StoredEntry(uid="u3", item_id=t2),
        ],
    )
    # A no-op reorder (all three uids) triggers the .m3u8 (re)write.
    r = client.put(f"/api/playlists/{record.id}/tracks", json={"entry_uids": ["u1", "u2", "u3"]})
    assert r.status_code == 200
    # The pending row has no file, so the export carries exactly the 2 resolved.
    m3u = os.path.join(_export_dir(beets_library), f"{record.id}.m3u8")
    with open(m3u, encoding="utf-8") as fh:
        body = fh.read()
    assert body.count("#EXTINF:") == 2
    assert "Alpha" in body and "Gamma" in body and "Ghost" not in body
    # The Plex specs likewise resolve only the 2 real items.
    from app.api.playlists import _plex_specs_for
    from app.plex.config import PlexConfig

    reread = store.get_playlist(_dir(), record.id)
    assert reread is not None
    specs = _plex_specs_for(reread, beets_library, PlexConfig(base_url="http://x", token="t"))
    assert len(specs) == 2


def test_plex_specs_carry_the_track_length(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    # The (album, title) fallback accepts a candidate only on a length that
    # agrees, so a spec built without one turns that fallback off for the track.
    # Nothing between beets and the matcher may drop the value.
    from app.api.playlists import _plex_specs_for
    from app.plex.config import PlexConfig

    t1 = _add_track(beets_library, "Timed", length=251.5)
    record = store.create_playlist(_dir(), name="Mix", entries=[StoredEntry(uid="u1", item_id=t1)])
    specs = _plex_specs_for(record, beets_library, PlexConfig(base_url="http://x", token="t"))
    assert [s.length_seconds for s in specs] == [251.5]


def _export_dir(handle: LibraryHandle) -> str:
    return os.path.join(os.fsdecode(handle.lib.directory), ".playlists")


def test_export_written_on_add(client: TestClient, beets_library: LibraryHandle) -> None:
    t1 = _add_track(beets_library, "Alpha")
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1]})

    m3u = os.path.join(_export_dir(beets_library), f"{pid}.m3u8")
    assert os.path.isfile(m3u)
    with open(m3u, encoding="utf-8") as fh:
        body = fh.read()
    assert body.startswith("#EXTM3U\n#PLAYLIST:Mix\n")
    assert "Alpha" in body
    assert "../Seed/Alpha.flac" in body


def test_export_updates_name_on_rename(client: TestClient, beets_library: LibraryHandle) -> None:
    pid = client.post("/api/playlists", json={"name": "Old"}).json()["id"]
    client.patch(f"/api/playlists/{pid}", json={"name": "Renamed"})
    m3u = os.path.join(_export_dir(beets_library), f"{pid}.m3u8")
    with open(m3u, encoding="utf-8") as fh:
        assert "#PLAYLIST:Renamed" in fh.read()


def test_export_removed_on_delete(client: TestClient, beets_library: LibraryHandle) -> None:
    pid = client.post("/api/playlists", json={"name": "Bye"}).json()["id"]
    m3u = os.path.join(_export_dir(beets_library), f"{pid}.m3u8")
    assert os.path.isfile(m3u)
    client.delete(f"/api/playlists/{pid}")
    assert not os.path.exists(m3u)


def _plex_path(handle: LibraryHandle, title: str) -> str:
    """Where ``_add_track`` put that title's file, as Plex sees it.

    No ``library_path`` is configured in these tests, so ``translate_path``
    leaves the beets path alone and the two views coincide.
    """
    return os.path.join(os.fsdecode(handle.lib.directory), "Seed", f"{title}.flac")


def _fake_plex(monkeypatch: pytest.MonkeyPatch, tracks: list[FakeTrack]) -> FakeServer:
    """Patch the sync seam (``plex_sync.client.connect``) at ONE fake server.

    One server for the whole test on purpose: it KEEPS what it created, so a
    second sync sees the first one's playlist and exercises the in-place
    reconcile rather than a fresh create. The double comes from
    ``tests.plex_fakes`` — see its module docstring for why a local one-off fake
    (whose ``removeItems`` cleared everything) let wrong diffs pass.
    """
    server = FakeServer(tracks)
    monkeypatch.setattr(plex_sync.client, "connect", lambda base_url, token: server)
    return server


def test_sync_pushes_to_plex(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    t1 = _add_track(beets_library, "Alpha")
    server = _fake_plex(monkeypatch, [FakeTrack(10, [_plex_path(beets_library, "Alpha")])])
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})

    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1]})

    r = client.post(f"/api/playlists/{pid}/sync")
    assert r.status_code == 200
    state = r.json()["plex"]["admin"]
    assert state["status"] == "ok"
    assert state["rating_key"] == str(server.created[0].ratingKey)
    assert server.created[0].live_keys() == [10]
    assert state["synced_at"]


def test_sync_updates_existing_plex_copy_in_place_and_reports_missing(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-sync UPDATES the same Plex playlist, and each miss is named.

    The second sync must not mint a second copy: the ratingKey is the identity
    Plex clients hold, and a delete-then-recreate would break every one of them.
    """
    t1 = _add_track(beets_library, "Alpha")
    t2 = _add_track(beets_library, "Beta")  # deliberately NOT in Plex
    server = _fake_plex(monkeypatch, [FakeTrack(10, [_plex_path(beets_library, "Alpha")])])
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})

    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1, t2]})

    r1 = client.post(f"/api/playlists/{pid}/sync")
    assert r1.status_code == 200
    first = r1.json()["plex"]["admin"]
    assert first["status"] == "partial"
    assert first["missing"] == 1
    assert [m["item_id"] for m in first["missing_tracks"]] == [t2]
    assert first["missing_tracks"][0]["reason"] == "not_found"
    assert first["missing_tracks"][0]["title"] == "Beta"

    r2 = client.post(f"/api/playlists/{pid}/sync")
    assert r2.status_code == 200
    assert r2.json()["plex"]["admin"]["rating_key"] == first["rating_key"]
    assert len(server.created) == 1  # updated in place, not recreated
    assert server.created[0].live_keys() == [10]


def test_detail_plex_field_redeclares_the_summary_type_exactly() -> None:
    # PlaylistDetail redeclares `plex` ONLY to carry its own description (the
    # detail view holds miss identities; list rows don't). mypy --strict does not
    # notice if the two annotations drift apart (verified: widening the parent
    # to `| None` while the child stays narrow typechecks clean), so pin it here.
    assert (
        PlaylistDetail.model_fields["plex"].annotation == Playlist.model_fields["plex"].annotation
    )
    assert (
        PlaylistDetail.model_fields["plex"].description != Playlist.model_fields["plex"].description
    )  # the redeclaration exists for this difference


def test_miss_identities_are_detail_only_but_the_count_is_everywhere(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """List rows carry the miss COUNT; only the detail view names the tracks.

    A target state holds up to 200 identities, and one wrong `library_path` puts
    every playlist at that cap at once — which would land on `GET /api/playlists`,
    the endpoint the playlists page hits on every navigation and the one place
    nothing renders them.
    """
    t1 = _add_track(beets_library, "Alpha")
    t2 = _add_track(beets_library, "Beta")  # deliberately NOT in Plex
    _fake_plex(monkeypatch, [FakeTrack(10, [_plex_path(beets_library, "Alpha")])])
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})

    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1, t2]})
    synced = client.post(f"/api/playlists/{pid}/sync")
    assert [m["item_id"] for m in synced.json()["plex"]["admin"]["missing_tracks"]] == [t2]

    row = client.get("/api/playlists").json()[0]
    assert row["plex"]["admin"]["missing"] == 1  # the badge still knows
    assert row["plex"]["admin"]["missing_tracks"] == []  # but not who

    detail = client.get(f"/api/playlists/{pid}").json()
    assert detail["plex"]["admin"]["missing"] == 1
    assert [m["item_id"] for m in detail["plex"]["admin"]["missing_tracks"]] == [t2]


def test_sync_unconfigured_409(client: TestClient) -> None:
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    r = client.post(f"/api/playlists/{pid}/sync")
    assert r.status_code == 409


def test_sync_missing_playlist_404(client: TestClient) -> None:
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})
    r = client.post(f"/api/playlists/{'0' * 32}/sync")
    assert r.status_code == 404


def test_sync_connection_error_502(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from requests.exceptions import ConnectionError as ReqConnErr

    def boom(base_url: str, token: str) -> object:
        raise ReqConnErr("no route")

    monkeypatch.setattr(plex_sync.client, "connect", boom)
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    r = client.post(f"/api/playlists/{pid}/sync")
    assert r.status_code == 502


def test_delete_cascades_to_plex(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    t1 = _add_track(beets_library, "Alpha")
    server = _fake_plex(monkeypatch, [FakeTrack(10, [_plex_path(beets_library, "Alpha")])])
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1]})
    client.post(f"/api/playlists/{pid}/sync")  # now record.plex == {"admin": ...}

    captured: list[dict[str, str | None]] = []

    def _record(
        config: object, rating_keys: dict[str, str | None], *, playlist_id: str
    ) -> dict[str, str]:
        captured.append(dict(rating_keys))
        return {}

    monkeypatch.setattr(plex_sync, "delete_playlist_on_targets", _record)
    r = client.delete(f"/api/playlists/{pid}")
    assert r.status_code == 204
    # Deleting the MusicDrop playlist cascades by the ratingKey the sync recorded
    # — the key of the copy it created and would have updated in place.
    assert captured == [{"admin": str(server.created[0].ratingKey)}]


def test_delete_best_effort_when_plex_errors(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    t1 = _add_track(beets_library, "Alpha")
    _fake_plex(monkeypatch, [FakeTrack(10, [_plex_path(beets_library, "Alpha")])])
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1]})
    client.post(f"/api/playlists/{pid}/sync")

    def boom(
        config: object, rating_keys: dict[str, str | None], *, playlist_id: str
    ) -> dict[str, str]:
        raise RuntimeError("plex down")

    monkeypatch.setattr(plex_sync, "delete_playlist_on_targets", boom)
    r = client.delete(f"/api/playlists/{pid}")
    assert r.status_code == 204  # cleanup failure never fails the local delete
    assert client.get(f"/api/playlists/{pid}").status_code == 404  # really gone


def test_delete_unconfigured_skips_plex(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def _mark(
        config: object, rating_keys: dict[str, str | None], *, playlist_id: str
    ) -> dict[str, str]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(plex_sync, "delete_playlist_on_targets", _mark)
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    r = client.delete(f"/api/playlists/{pid}")
    assert r.status_code == 204
    assert called is False  # no Plex configured -> no cleanup attempt


def test_patch_sets_target_users(client: TestClient) -> None:
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    r = client.patch(f"/api/playlists/{pid}", json={"target_plex_users": ["7", "8"]})
    assert r.status_code == 200
    assert r.json()["target_plex_users"] == ["7", "8"]


def test_sync_fans_out_to_targets(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    t1 = _add_track(beets_library, "Alpha")
    server = _fake_plex(monkeypatch, [FakeTrack(9, [_plex_path(beets_library, "Alpha")])])
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})

    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1]})
    client.patch(f"/api/playlists/{pid}", json={"target_plex_users": ["7"]})

    r = client.post(f"/api/playlists/{pid}/sync")
    assert r.status_code == 200
    plex = r.json()["plex"]
    assert set(plex) == {"admin", "7"}
    assert plex["admin"]["status"] == "ok"
    assert plex["7"]["synced_at"]
    # Each account gets its OWN copy: playlist ratingKeys come from one
    # server-global space, so two copies can never share a key.
    assert plex["admin"]["rating_key"] != plex["7"]["rating_key"]
    assert [pl.live_keys() for pl in server.created] == [[9]]
    assert [pl.live_keys() for pl in server.users["7"].created] == [[9]]


def test_sync_cleans_up_detargeted_user(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    t1 = _add_track(beets_library, "Alpha")
    server = _fake_plex(monkeypatch, [FakeTrack(9, [_plex_path(beets_library, "Alpha")])])
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})

    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1]})
    client.patch(f"/api/playlists/{pid}", json={"target_plex_users": ["7"]})
    client.post(f"/api/playlists/{pid}/sync")  # plex == {"admin", "7"}
    user_key = str(server.users["7"].created[0].ratingKey)

    # Untick user 7, then re-sync: user 7's copy must be cleaned up.
    client.patch(f"/api/playlists/{pid}", json={"target_plex_users": []})
    captured: list[dict[str, str | None]] = []

    def _record(
        config: object, rating_keys: dict[str, str | None], *, playlist_id: str
    ) -> dict[str, str]:
        captured.append(dict(rating_keys))
        return {uid: "deleted" for uid in rating_keys}  # delete CONFIRMED

    monkeypatch.setattr(plex_sync, "delete_playlist_on_targets", _record)
    r = client.post(f"/api/playlists/{pid}/sync")
    assert r.status_code == 200
    assert captured == [{"7": user_key}]  # the de-targeted user's recorded ratingKey
    assert set(r.json()["plex"]) == {"admin"}  # confirmed-deleted -> dropped from the map


def test_sync_retains_detargeted_user_when_delete_unconfirmed(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # M24 residual: if the de-target delete does NOT confirm removal (e.g. a
    # transient admin.switchUser failure), the user's entry + recorded ratingKey
    # must be RETAINED so a later sync can retry — dropping it (the whole-map
    # replace) would orphan the still-existing Plex copy forever.
    t1 = _add_track(beets_library, "Alpha")
    t2 = _add_track(beets_library, "Beta")  # deliberately NOT in Plex -> a real miss
    server = _fake_plex(monkeypatch, [FakeTrack(9, [_plex_path(beets_library, "Alpha")])])
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})

    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1, t2]})
    client.patch(f"/api/playlists/{pid}", json={"target_plex_users": ["7"]})
    first = client.post(f"/api/playlists/{pid}/sync")  # plex == {"admin", "7": user_key}
    user_key = str(server.users["7"].created[0].ratingKey)
    assert [m["item_id"] for m in first.json()["plex"]["7"]["missing_tracks"]] == [t2]

    client.patch(f"/api/playlists/{pid}", json={"target_plex_users": []})  # untick 7

    def _failed(
        config: object, rating_keys: dict[str, str | None], *, playlist_id: str
    ) -> dict[str, str]:
        return {uid: "failed" for uid in rating_keys}  # switchUser threw -> NOT confirmed

    monkeypatch.setattr(plex_sync, "delete_playlist_on_targets", _failed)
    r = client.post(f"/api/playlists/{pid}/sync")
    assert r.status_code == 200
    plex = r.json()["plex"]
    assert set(plex) == {"admin", "7"}  # 7 retained (delete unconfirmed) -> retry path
    assert plex["7"]["rating_key"] == user_key  # its ratingKey survives for the retry
    # The entry is kept as a DELETE HANDLE, not as a sync result: the count stays
    # honest, but the identities behind it are not pinned to the record forever
    # for an account nothing syncs any more.
    assert plex["7"]["missing"] == 1
    assert plex["7"]["missing_tracks"] == []


# --- Playlist artwork (cover) endpoints -------------------------------------

_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
_JPG = b"\xff\xd8\xff" + b"0" * 32


def _add_album_track(handle: LibraryHandle, title: str) -> tuple[int, int]:
    """Seed a 1-track album; return (item_id, album_id)."""
    music = os.fsdecode(handle.lib.directory)
    folder = os.path.join(music, "Alb", title)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{title}.flac")
    with open(path, "wb") as fh:
        fh.write(b"\x00")
    item = Item(album=title, albumartist="AA", artist="AA", title=title, track=1)
    item.path = os.fsencode(path)
    album = handle.lib.add_album([item])
    return _require_id(item.id), _require_id(album.id)


def test_playlist_artwork_lifecycle(client: TestClient) -> None:
    pid = client.post("/api/playlists", json={"name": "Art"}).json()["id"]
    # No artwork yet.
    assert client.get(f"/api/playlists/{pid}/artwork").status_code == 404
    # Upload a PNG -> 200, summary carries the artwork hash.
    put = client.put(f"/api/playlists/{pid}/artwork", content=_PNG)
    assert put.status_code == 200
    assert put.json()["artwork_hash"] is not None
    # GET serves the bytes with a revalidating ETag.
    got = client.get(f"/api/playlists/{pid}/artwork")
    assert got.status_code == 200
    assert got.content == _PNG
    etag = got.headers["etag"]
    assert got.headers["cache-control"] == "no-cache"
    # A matching If-None-Match revalidates to a bodiless 304.
    again = client.get(f"/api/playlists/{pid}/artwork", headers={"If-None-Match": etag})
    assert again.status_code == 304
    # DELETE removes it (idempotent), then GET is 404 again.
    assert client.delete(f"/api/playlists/{pid}/artwork").status_code == 204
    assert client.get(f"/api/playlists/{pid}/artwork").status_code == 404


def test_playlist_artwork_put_rejects_garbage_bytes(client: TestClient) -> None:
    pid = client.post("/api/playlists", json={"name": "Art"}).json()["id"]
    r = client.put(f"/api/playlists/{pid}/artwork", content=b"not an image at all")
    assert r.status_code == 415


def test_playlist_artwork_put_accepts_jpeg(client: TestClient) -> None:
    pid = client.post("/api/playlists", json={"name": "Art"}).json()["id"]
    assert client.put(f"/api/playlists/{pid}/artwork", content=_JPG).status_code == 200
    got = client.get(f"/api/playlists/{pid}/artwork")
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/jpeg"


def test_playlist_artwork_put_unknown_id_404(client: TestClient) -> None:
    r = client.put(f"/api/playlists/{'0' * 32}/artwork", content=_PNG)
    assert r.status_code == 404


def test_playlist_artwork_delete_idempotent_and_unknown_404(client: TestClient) -> None:
    pid = client.post("/api/playlists", json={"name": "Art"}).json()["id"]
    # Deleting with no artwork present is a 204 no-op.
    assert client.delete(f"/api/playlists/{pid}/artwork").status_code == 204
    # Unknown playlist is 404.
    assert client.delete(f"/api/playlists/{'0' * 32}/artwork").status_code == 404


def test_list_carries_artwork_and_cover_keys(client: TestClient) -> None:
    client.post("/api/playlists", json={"name": "Art"})
    row = client.get("/api/playlists").json()[0]
    assert row["artwork_hash"] is None
    assert row["cover_album_ids"] == []


def test_detail_cover_album_ids_from_resolved_album_tracks(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    item_id, album_id = _add_album_track(beets_library, "Track")
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [item_id]})
    detail = client.get(f"/api/playlists/{pid}").json()
    assert detail["cover_album_ids"] == [album_id]


def test_sync_uploads_the_poster_once_across_repeat_syncs(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pushed artwork hash round-trips: recorded state -> next sync's prior.

    The reconcile skips the poster upload when the art hash matches the one on
    that target's PRIOR state, so the router has to hand the WHOLE prior state
    back (``priors=dict(record.plex)``) and persist what comes out. Break either
    half and every sync re-uploads the same cover, piling duplicates into Plex's
    poster gallery — which is exactly what a green "it synced" would hide.
    """
    t1 = _add_track(beets_library, "Alpha")
    server = _fake_plex(monkeypatch, [FakeTrack(10, [_plex_path(beets_library, "Alpha")])])
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})

    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1]})
    art = client.put(f"/api/playlists/{pid}/artwork", content=_PNG)
    assert art.status_code == 200
    art_hash = art.json()["artwork_hash"]

    r1 = client.post(f"/api/playlists/{pid}/sync")
    assert r1.status_code == 200
    assert r1.json()["plex"]["admin"]["artwork_hash"] == art_hash
    poster = str(store.artwork_path(_dir(), pid, "png"))
    assert server.created[0].poster_uploads == [poster]

    r2 = client.post(f"/api/playlists/{pid}/sync")
    assert r2.status_code == 200
    assert r2.json()["plex"]["admin"]["artwork_hash"] == art_hash  # still recorded
    # Pinned together on purpose: a second COPY would take its own poster upload
    # and leave created[0]'s list at one, so the count is what makes this an
    # "uploaded once" assertion rather than an "uploaded once per playlist" one.
    assert len(server.created) == 1
    assert server.created[0].poster_uploads == [poster]  # unchanged art -> no re-upload


def test_sync_uploads_the_poster_once_to_a_targeted_user_too(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prior round trip reaches the FAN-OUT targets, not just admin.

    ``priors`` is keyed by target, so a router that hands back only admin's prior
    leaves every targeted user's reconcile with no recorded hash — and re-uploads
    the same cover into that account's poster gallery on EVERY sync, forever.
    Admin's arm of this (``…once_across_repeat_syncs``) cannot see it: the whole
    suite stays green with the user entries filtered out of ``priors``.
    """
    t1 = _add_track(beets_library, "Alpha")
    server = _fake_plex(monkeypatch, [FakeTrack(10, [_plex_path(beets_library, "Alpha")])])
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})

    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1]})
    client.patch(f"/api/playlists/{pid}", json={"target_plex_users": ["7"]})
    art = client.put(f"/api/playlists/{pid}/artwork", content=_PNG)
    assert art.status_code == 200
    art_hash = art.json()["artwork_hash"]

    r1 = client.post(f"/api/playlists/{pid}/sync")
    assert r1.status_code == 200
    assert r1.json()["plex"]["7"]["artwork_hash"] == art_hash
    user_server = server.users["7"]
    poster = str(store.artwork_path(_dir(), pid, "png"))
    assert user_server.created[0].poster_uploads == [poster]

    r2 = client.post(f"/api/playlists/{pid}/sync")
    assert r2.status_code == 200
    assert r2.json()["plex"]["7"]["artwork_hash"] == art_hash  # still recorded
    # Same pairing as the admin test: the count is what makes this "uploaded
    # once" rather than "once per copy" — a second copy would take its own.
    assert len(user_server.created) == 1
    assert user_server.created[0].poster_uploads == [poster]  # unchanged art -> no re-upload


def test_a_copy_whose_marker_never_landed_is_re_found_by_its_recorded_key(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded ``rating_key`` has to round-trip too, or an UNSTAMPED copy is
    duplicated on every sync.

    ``editSummary`` is a separate PUT that can keep failing; the reconcile
    deliberately does not fail over it (``test_stamp_failure_does_not_orphan_the_playlist``),
    which leaves a Plex copy carrying no marker at all. The recorded key is then
    the ONLY identity that finds it — so a router that strips ``rating_key`` out
    of ``priors`` mints a fresh playlist every single sync, and no fake-marker
    test can see it because the fakes always end up stamped.
    """

    def _boom(self: FakePlaylist, summary: str, locked: bool = True) -> FakePlaylist:
        raise RuntimeError("transient stamp failure")

    monkeypatch.setattr(FakePlaylist, "editSummary", _boom)
    t1 = _add_track(beets_library, "Alpha")
    server = _fake_plex(monkeypatch, [FakeTrack(10, [_plex_path(beets_library, "Alpha")])])
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})

    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1]})

    r1 = client.post(f"/api/playlists/{pid}/sync")
    assert r1.status_code == 200
    key = r1.json()["plex"]["admin"]["rating_key"]
    assert server.created[0].live_summary() == ""  # the marker never landed

    r2 = client.post(f"/api/playlists/{pid}/sync")
    assert r2.status_code == 200
    assert len(server.created) == 1, "a duplicate Plex playlist was created"
    assert r2.json()["plex"]["admin"]["rating_key"] == key  # the same copy, updated in place
    assert server.created[0].live_keys() == [10]


def test_sync_reuploads_the_poster_when_the_artwork_changes(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The control arm for the test above: skipping is keyed on the HASH, not on
    # "a poster was already pushed" — replacing the cover must reach Plex.
    t1 = _add_track(beets_library, "Alpha")
    server = _fake_plex(monkeypatch, [FakeTrack(10, [_plex_path(beets_library, "Alpha")])])
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})

    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1]})
    client.put(f"/api/playlists/{pid}/artwork", content=_PNG)
    client.post(f"/api/playlists/{pid}/sync")

    replaced = client.put(f"/api/playlists/{pid}/artwork", content=_JPG)
    assert replaced.status_code == 200
    r = client.post(f"/api/playlists/{pid}/sync")
    assert r.status_code == 200
    assert r.json()["plex"]["admin"]["artwork_hash"] == replaced.json()["artwork_hash"]
    assert len(server.created) == 1  # both uploads land on the ONE copy
    assert server.created[0].poster_uploads == [
        str(store.artwork_path(_dir(), pid, "png")),
        str(store.artwork_path(_dir(), pid, "jpg")),
    ]


def test_cover_album_ids_empty_when_playlist_has_artwork(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    # A playlist with real uploaded art short-circuits the collage on the FE, so
    # the (potentially expensive) album scan is skipped — cover_album_ids is [].
    item_id, _album_id = _add_album_track(beets_library, "Solo")
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [item_id]})
    put = client.put(f"/api/playlists/{pid}/artwork", content=_PNG)
    assert put.status_code == 200
    assert put.json()["cover_album_ids"] == []
    detail = client.get(f"/api/playlists/{pid}").json()
    assert detail["artwork_hash"] is not None
    assert detail["cover_album_ids"] == []


def test_merge_appends_source_tracks_and_reports_the_counts(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    alpha = _add_track(beets_library, "Alpha")
    beta = _add_track(beets_library, "Beta")
    gamma = _add_track(beets_library, "Gamma")
    target = store.create_playlist(
        _dir(),
        name="Keep",
        entries=[StoredEntry(uid="t1", item_id=alpha), StoredEntry(uid="t2", item_id=beta)],
    )
    source = store.create_playlist(
        _dir(),
        name="Fold in",
        entries=[StoredEntry(uid="s1", item_id=beta), StoredEntry(uid="s2", item_id=gamma)],
    )

    r = client.post(f"/api/playlists/{target.id}/merge", json={"source_id": source.id})

    assert r.status_code == 200
    payload = r.json()
    assert payload["added"] == 1
    assert payload["skipped_duplicates"] == 1
    assert payload["source_deleted"] is False
    # Appended in source order; the target's own order is untouched.
    assert [t["id"] for t in payload["playlist"]["tracks"]] == [alpha, beta, gamma]


def test_merge_keeps_a_pending_row_that_matches_a_resolved_target_track_by_text(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """THE ANTI-GUESSING GUARD (API half), at the layer where guessing is possible.

    The target holds a RESOLVED library track really titled "Last Christmas";
    the source holds a PENDING row remembering the same artist and title. They
    need not be the same recording - Wham! and Ariana Grande both have one - and
    the pending row has no library track behind it, so any dedupe here would be
    guessing on text. Both rows must survive. If this ever fails because the
    titles matched, merge has started dropping songs.
    """
    resolved = _add_track(beets_library, "Last Christmas")
    target = store.create_playlist(
        _dir(), name="Keep", entries=[StoredEntry(uid="t1", item_id=resolved)]
    )
    source = store.create_playlist(
        _dir(),
        name="Fold in",
        entries=[
            StoredEntry(
                uid="s1",
                pending=PendingTrack(artist="Art", title="Last Christmas", source="m3u line"),
            )
        ],
    )

    r = client.post(f"/api/playlists/{target.id}/merge", json={"source_id": source.id})

    assert r.status_code == 200
    payload = r.json()
    assert payload["added"] == 1
    assert payload["skipped_duplicates"] == 0
    tracks = payload["playlist"]["tracks"]
    assert len(tracks) == 2
    assert tracks[0]["pending"] is False
    assert tracks[0]["title"] == "Last Christmas"
    assert tracks[1]["pending"] is True
    assert tracks[1]["title"] == "Last Christmas"
    assert tracks[1]["source"] == "m3u line"


def test_merge_into_itself_is_409_and_writes_nothing(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    alpha = _add_track(beets_library, "Alpha")
    record = store.create_playlist(
        _dir(), name="Keep", entries=[StoredEntry(uid="t1", item_id=alpha)]
    )
    before = store.get_playlist(_dir(), record.id)
    assert before is not None

    r = client.post(f"/api/playlists/{record.id}/merge", json={"source_id": record.id})

    assert r.status_code == 409
    after = store.get_playlist(_dir(), record.id)
    assert after is not None
    assert [e.uid for e in after.entries] == ["t1"]  # nothing appended
    assert after.updated_at == before.updated_at  # nothing written at all


def test_merge_unknown_target_or_source_is_404_from_the_route_itself(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """404 carrying THIS route's body, not Starlette's "no such path" 404.

    A status-only assertion here would be vacuous: with no /merge route
    registered, Starlette finds no matching path shape at all and answers 404
    (never 405 - it never reaches method negotiation), so `== 404` passes
    against no implementation. The detail sentence is what only the real
    handler produces; Starlette's is the bare "Not Found".
    """
    real = store.create_playlist(_dir(), name="Keep")
    missing = "0" * 32

    unknown_target = client.post(f"/api/playlists/{missing}/merge", json={"source_id": real.id})
    unknown_source = client.post(f"/api/playlists/{real.id}/merge", json={"source_id": missing})
    # A hostile id is rejected by the store's 32-char-hex guard before any
    # filesystem access (the chunk-1 path-traversal finding). One line: it fits
    # inside ruff's 100 columns, so a wrapped call would be reformatted.
    hostile = client.post(f"/api/playlists/{real.id}/merge", json={"source_id": "../../etc/passwd"})

    assert unknown_target.status_code == 404
    assert unknown_target.json()["detail"] == "Playlist not found"
    assert unknown_source.status_code == 404
    assert unknown_source.json()["detail"] == "Playlist not found"
    assert hostile.status_code == 404
    assert hostile.json()["detail"] == "Playlist not found"


def test_merge_with_a_corrupt_source_record_is_404_and_leaves_the_target_alone(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """An unparseable record reads as MISSING, and is never a 500.

    ``store.get_playlist`` catches the ``ValueError`` from
    ``model_validate_json`` and returns ``None``
    (``backend/app/playlists/store.py:192-195``), so a corrupt file is
    indistinguishable from an absent one: ``merge_playlists`` returns ``None``
    and the router's existing 404 path covers it. The target must come through
    untouched - ``merge_playlists`` reads BOTH records under the lock and
    returns before it writes anything. This is load-bearing and an earlier
    draft of the spec mis-stated it (it claimed the read raises), which is
    exactly why it is pinned here.
    """
    alpha = _add_track(beets_library, "Alpha")
    target = store.create_playlist(
        _dir(), name="Keep", entries=[StoredEntry(uid="t1", item_id=alpha)]
    )
    source = store.create_playlist(_dir(), name="Fold in")
    (_dir() / f"{source.id}.json").write_text("{ not json", encoding="utf-8")
    before = store.get_playlist(_dir(), target.id)
    assert before is not None

    r = client.post(f"/api/playlists/{target.id}/merge", json={"source_id": source.id})

    assert r.status_code == 404
    assert r.json()["detail"] == "Playlist not found"
    after = store.get_playlist(_dir(), target.id)
    assert after is not None
    assert [e.uid for e in after.entries] == ["t1"]
    assert after.updated_at == before.updated_at


def test_merge_rewrites_the_target_export(client: TestClient, beets_library: LibraryHandle) -> None:
    alpha = _add_track(beets_library, "Alpha")
    target = client.post("/api/playlists", json={"name": "Keep"}).json()["id"]
    source = client.post("/api/playlists", json={"name": "Fold in"}).json()["id"]
    client.post(f"/api/playlists/{source}/tracks", json={"track_ids": [alpha]})

    r = client.post(f"/api/playlists/{target}/merge", json={"source_id": source})

    assert r.status_code == 200
    target_m3u = os.path.join(_export_dir(beets_library), f"{target}.m3u8")
    with open(target_m3u, encoding="utf-8") as fh:
        body = fh.read()
    assert "Alpha" in body


def test_merge_with_delete_source_removes_the_source_and_its_export(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    alpha = _add_track(beets_library, "Alpha")
    target = client.post("/api/playlists", json={"name": "Keep"}).json()["id"]
    source = client.post("/api/playlists", json={"name": "Fold in"}).json()["id"]
    client.post(f"/api/playlists/{source}/tracks", json={"track_ids": [alpha]})
    source_m3u = os.path.join(_export_dir(beets_library), f"{source}.m3u8")
    assert os.path.isfile(source_m3u)

    r = client.post(
        f"/api/playlists/{target}/merge", json={"source_id": source, "delete_source": True}
    )

    assert r.status_code == 200
    assert r.json()["source_deleted"] is True
    assert client.get(f"/api/playlists/{source}").status_code == 404
    assert not os.path.exists(source_m3u)
    detail = client.get(f"/api/playlists/{target}").json()
    assert [t["id"] for t in detail["tracks"]] == [alpha]


def test_merge_without_delete_source_leaves_the_source_alone(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    alpha = _add_track(beets_library, "Alpha")
    target = client.post("/api/playlists", json={"name": "Keep"}).json()["id"]
    source = client.post("/api/playlists", json={"name": "Fold in"}).json()["id"]
    client.post(f"/api/playlists/{source}/tracks", json={"track_ids": [alpha]})

    r = client.post(f"/api/playlists/{target}/merge", json={"source_id": source})

    assert r.status_code == 200
    assert r.json()["source_deleted"] is False
    detail = client.get(f"/api/playlists/{source}")
    assert detail.status_code == 200
    assert [t["id"] for t in detail.json()["tracks"]] == [alpha]


def test_merge_delete_source_cascades_to_plex(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ticked "delete afterwards" removes the source's Plex copies too, by the
    ratingKey its own sync recorded.

    The cascade HELPER is the one the DELETE route uses
    (``_best_effort_plex_delete``) and its behaviour is unchanged; the call site
    is new, because a lock-holding merge cannot reach the DELETE path.
    """
    alpha = _add_track(beets_library, "Alpha")
    server = _fake_plex(monkeypatch, [FakeTrack(10, [_plex_path(beets_library, "Alpha")])])
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})
    target = client.post("/api/playlists", json={"name": "Keep"}).json()["id"]
    source = client.post("/api/playlists", json={"name": "Fold in"}).json()["id"]
    client.post(f"/api/playlists/{source}/tracks", json={"track_ids": [alpha]})
    client.post(f"/api/playlists/{source}/sync")  # source record now has plex["admin"]

    captured: list[dict[str, str | None]] = []

    def _record(
        config: object, rating_keys: dict[str, str | None], *, playlist_id: str
    ) -> dict[str, str]:
        captured.append(dict(rating_keys))
        return {}

    monkeypatch.setattr(plex_sync, "delete_playlist_on_targets", _record)

    r = client.post(
        f"/api/playlists/{target}/merge", json={"source_id": source, "delete_source": True}
    )

    assert r.status_code == 200
    assert captured == [{"admin": str(server.created[0].ratingKey)}]


def test_merge_reads_out_of_date_against_the_targets_last_sync(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Merge does not sync; it bumps updated_at so the editor asks for one."""
    alpha = _add_track(beets_library, "Alpha")
    beta = _add_track(beets_library, "Beta")
    _fake_plex(monkeypatch, [FakeTrack(10, [_plex_path(beets_library, "Alpha")])])
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})
    target = client.post("/api/playlists", json={"name": "Keep"}).json()["id"]
    client.post(f"/api/playlists/{target}/tracks", json={"track_ids": [alpha]})
    synced = client.post(f"/api/playlists/{target}/sync").json()
    synced_at = synced["plex"]["admin"]["synced_at"]
    assert synced_at is not None
    source = client.post("/api/playlists", json={"name": "Fold in"}).json()["id"]
    client.post(f"/api/playlists/{source}/tracks", json={"track_ids": [beta]})

    merged = client.post(f"/api/playlists/{target}/merge", json={"source_id": source}).json()

    # updated_at > synced_at is exactly the editor's "Out of date; re-sync" rule.
    assert merged["playlist"]["updated_at"] > synced_at
    assert merged["playlist"]["plex"]["admin"]["synced_at"] == synced_at


# --- Lone-surrogate names: no 500, lossless in the store, scrubbed on the wire ---


def test_create_with_lone_surrogate_name_is_2xx_and_degrades_on_wire(
    client: TestClient,
) -> None:
    """The first-hand repro: POST with a lone surrogate in the name must not 500.

    The client delivers the lone surrogate as a `"\\udce9"` JSON escape without
    sending a single non-UTF-8 byte, so httpx's ``json=`` kwarg cannot even build
    the body (its encoder rejects the lone surrogate) — a raw ``content=`` body is
    required. The store keeps the exact code points; the WIRE degrades them to
    U+FFFD on the way out (see ``app/wire.py``).
    """
    r = client.post(
        "/api/playlists",
        content=b'{"name": "Caf\\udce9"}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 200
    pid = r.json()["id"]
    # The create response degrades the lone surrogate to U+FFFD.
    assert r.json()["name"] == "Caf\ufffd"

    # The list and detail routes also 2xx, carrying the degraded name.
    listed = client.get("/api/playlists")
    assert listed.status_code == 200
    assert [p["name"] for p in listed.json()] == ["Caf\ufffd"]

    detail = client.get(f"/api/playlists/{pid}")
    assert detail.status_code == 200
    assert detail.json()["name"] == "Caf\ufffd"

    # ...while the STORE keeps the exact code points (lossless, not scrubbed).
    loaded = store.get_playlist(_dir(), pid)
    assert loaded is not None
    assert loaded.name == "Caf\udce9"


def test_create_with_leading_surrogate_name_is_2xx(client: TestClient) -> None:
    """A LEADING lone surrogate (``\\ud800``) is equally legal and must not 500.

    The store stays lossless (exact code point); the response carries the WIRE
    degradation, which for a high surrogate (outside ``surrogateescape``'s
    U+DC80..U+DCFF) is what ``app.wire.wire_safe`` produces — so pin against it
    rather than hardcoding a placeholder count.
    """
    from app.wire import wire_safe

    r = client.post(
        "/api/playlists",
        content=b'{"name": "\\ud800bad"}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == wire_safe("\ud800bad")
    loaded = store.get_playlist(_dir(), r.json()["id"])
    assert loaded is not None
    assert loaded.name == "\ud800bad"  # lossless in the store


def test_export_survives_a_high_surrogate_name(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """A stored high surrogate (``\\ud800``) must not kill the ``.m3u8`` export.

    ``write_m3u`` encodes with ``surrogateescape``, whose window is
    U+DC80..U+DCFF — a high surrogate would raise and the best-effort export
    would silently skip the file (the exact silent-failure class #151 fixed for
    paths). The export therefore renders the NAME through ``wire_safe`` first:
    the name is a display label, so lossy is correct there — unlike the track
    paths, which stay byte-exact. The store still keeps the exact code points.
    """
    from app.wire import wire_safe

    r = client.post(
        "/api/playlists",
        content=b'{"name": "\\ud800bad"}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 200
    pid = r.json()["id"]
    m3u = os.path.join(_export_dir(beets_library), f"{pid}.m3u8")
    assert os.path.isfile(m3u)
    degraded = wire_safe("\ud800bad")
    with open(m3u, encoding="utf-8") as fh:
        assert f"#PLAYLIST:{degraded}" in fh.read()


def test_patch_rename_with_lone_surrogate_name_is_2xx(client: TestClient) -> None:
    """A non-create mutation (rename) through the shared sink also succeeds."""
    pid = client.post("/api/playlists", json={"name": "Old"}).json()["id"]
    r = client.patch(
        f"/api/playlists/{pid}",
        content=b'{"name": "New\\udce9"}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "New\ufffd"
    # Store is lossless; the wire is the only place the U+FFFD shows up.
    loaded = store.get_playlist(_dir(), pid)
    assert loaded is not None
    assert loaded.name == "New\udce9"
