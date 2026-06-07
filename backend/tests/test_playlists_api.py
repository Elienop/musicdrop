import os

import pytest
from beets.library import Item
from fastapi.testclient import TestClient

from app.beets.library import LibraryHandle


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


def _add_track(handle: LibraryHandle, title: str) -> int:
    music = handle.lib.directory  # bytes
    folder = os.path.join(os.fsdecode(music), "Seed")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{title}.flac")
    with open(path, "wb") as fh:
        fh.write(b"\x00")
    item = Item(album="Seed", albumartist="Art", artist="Art", title=title, track=1)
    item.path = os.fsencode(path)
    handle.lib.add(item)
    return int(item.id)


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
    r = client.delete(f"/api/playlists/{pid}/tracks/{t1}")
    assert r.status_code == 200
    assert [t["id"] for t in r.json()["tracks"]] == [t2]


def test_reorder_tracks(client: TestClient, beets_library: LibraryHandle) -> None:
    t1 = _add_track(beets_library, "Alpha")
    t2 = _add_track(beets_library, "Beta")
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1, t2]})
    r = client.put(f"/api/playlists/{pid}/tracks", json={"track_ids": [t2, t1]})
    assert r.status_code == 200
    assert [t["id"] for t in r.json()["tracks"]] == [t2, t1]


def test_reorder_missing_playlist_404(client: TestClient) -> None:
    r = client.put(f"/api/playlists/{'0' * 32}/tracks", json={"track_ids": [1]})
    assert r.status_code == 404


def test_remove_track_missing_playlist_404(client: TestClient) -> None:
    r = client.delete(f"/api/playlists/{'0' * 32}/tracks/1")
    assert r.status_code == 404


def test_reorder_empty_clears_playlist(client: TestClient, beets_library: LibraryHandle) -> None:
    t1 = _add_track(beets_library, "Alpha")
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1]})
    # Reorder is a full replacement; an empty list is a valid "clear".
    r = client.put(f"/api/playlists/{pid}/tracks", json={"track_ids": []})
    assert r.status_code == 200
    assert r.json()["tracks"] == []


def test_add_too_many_tracks_422(client: TestClient) -> None:
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    r = client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": list(range(10_001))})
    assert r.status_code == 422


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


class _SyncTrack:
    def __init__(self, rating_key: int, locations: list[str]) -> None:
        self.ratingKey = rating_key
        self.locations = locations


class _SyncSection:
    TYPE = "artist"

    def __init__(self, tracks: list[_SyncTrack]) -> None:
        self._tracks = tracks

    def searchTracks(self) -> list[_SyncTrack]:
        return self._tracks


class _SyncPlaylist:
    def __init__(self, title: str, items: list[_SyncTrack]) -> None:
        self.title = title
        self.ratingKey = 777
        self._items = list(items)

    def items(self) -> list[_SyncTrack]:
        return list(self._items)

    def addItems(self, tracks: list[_SyncTrack]) -> None:
        self._items.extend(tracks)

    def removeItems(self, tracks: list[_SyncTrack]) -> None:
        self._items = []

    def delete(self) -> None:
        self._items = []


class _SyncServer:
    def __init__(self, tracks: list[_SyncTrack]) -> None:
        section = _SyncSection(tracks)
        self.library = type("L", (), {"sections": lambda _s: [section]})()
        self._created: list[_SyncPlaylist] = []

    def playlists(self) -> list[_SyncPlaylist]:
        return []

    def createPlaylist(self, title: str, items: list[_SyncTrack]) -> _SyncPlaylist:
        pl = _SyncPlaylist(title, items)
        self._created.append(pl)
        return pl


def test_sync_pushes_to_plex(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    t1 = _add_track(beets_library, "Alpha")
    plex_path = os.path.join(os.fsdecode(beets_library.lib.directory), "Seed", "Alpha.flac")
    from app.plex import sync as plex_sync

    monkeypatch.setattr(
        plex_sync.client,
        "connect",
        lambda base_url, token: _SyncServer([_SyncTrack(10, [plex_path])]),
    )
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})

    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1]})

    r = client.post(f"/api/playlists/{pid}/sync")
    assert r.status_code == 200
    state = r.json()["plex"]["admin"]
    assert state["status"] == "ok"
    assert state["rating_key"] == "777"
    assert state["synced_at"]


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

    from app.plex import sync as plex_sync

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
    from app.plex import sync as plex_sync

    t1 = _add_track(beets_library, "Alpha")
    plex_path = os.path.join(os.fsdecode(beets_library.lib.directory), "Seed", "Alpha.flac")
    monkeypatch.setattr(
        plex_sync.client,
        "connect",
        lambda base_url, token: _SyncServer([_SyncTrack(10, [plex_path])]),
    )
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1]})
    client.post(f"/api/playlists/{pid}/sync")  # now record.plex == {"admin": ...}

    calls: list[tuple[str, list[str]]] = []

    def _record(config: object, title: str, targets: list[str]) -> dict[str, str]:
        calls.append((title, list(targets)))
        return {}

    monkeypatch.setattr(plex_sync, "delete_playlist_on_targets", _record)
    r = client.delete(f"/api/playlists/{pid}")
    assert r.status_code == 204
    assert calls == [("Mix", ["admin"])]


def test_delete_best_effort_when_plex_errors(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.plex import sync as plex_sync

    t1 = _add_track(beets_library, "Alpha")
    plex_path = os.path.join(os.fsdecode(beets_library.lib.directory), "Seed", "Alpha.flac")
    monkeypatch.setattr(
        plex_sync.client,
        "connect",
        lambda base_url, token: _SyncServer([_SyncTrack(10, [plex_path])]),
    )
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})
    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1]})
    client.post(f"/api/playlists/{pid}/sync")

    def boom(config: object, title: str, targets: list[str]) -> dict[str, str]:
        raise RuntimeError("plex down")

    monkeypatch.setattr(plex_sync, "delete_playlist_on_targets", boom)
    r = client.delete(f"/api/playlists/{pid}")
    assert r.status_code == 204  # cleanup failure never fails the local delete
    assert client.get(f"/api/playlists/{pid}").status_code == 404  # really gone


def test_delete_unconfigured_skips_plex(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.plex import sync as plex_sync

    called = False

    def _mark(config: object, title: str, targets: list[str]) -> dict[str, str]:
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
    plex_path = os.path.join(os.fsdecode(beets_library.lib.directory), "Seed", "Alpha.flac")

    from app.plex import sync as plex_sync

    class _FakePL:
        def __init__(self) -> None:
            self.title = "Mix"
            self.ratingKey = 1
            self._i: list[object] = []

        def items(self) -> list[object]:
            return self._i

        def addItems(self, t: list[object]) -> None:
            self._i.extend(t)

        def removeItems(self, t: list[object]) -> None:
            self._i = []

        def delete(self) -> None:
            self._i = []

    class _Sec:
        TYPE = "artist"

        def searchTracks(self) -> list[object]:
            tr = type("T", (), {"ratingKey": 9, "locations": [plex_path]})()
            return [tr]

    class _Srv:
        def __init__(self) -> None:
            self.library = type("L", (), {"sections": lambda _s: [_Sec()]})()

        def playlists(self) -> list[object]:
            return []

        def createPlaylist(self, title: str, items: list[object]) -> _FakePL:
            return _FakePL()

        def switchUser(self, uid: str) -> "_Srv":
            return _Srv()

    monkeypatch.setattr(plex_sync.client, "connect", lambda base_url, token: _Srv())
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


def test_sync_cleans_up_detargeted_user(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.plex import sync as plex_sync

    t1 = _add_track(beets_library, "Alpha")
    plex_path = os.path.join(os.fsdecode(beets_library.lib.directory), "Seed", "Alpha.flac")

    class _Sec:
        TYPE = "artist"

        def searchTracks(self) -> list[object]:
            return [type("T", (), {"ratingKey": 9, "locations": [plex_path]})()]

    class _Srv:
        def __init__(self) -> None:
            self.library = type("L", (), {"sections": lambda _s: [_Sec()]})()

        def playlists(self) -> list[object]:
            return []

        def createPlaylist(self, title: str, items: list[object]) -> object:
            return type("PL", (), {"title": title, "ratingKey": 1, "items": lambda _s: items})()

        def switchUser(self, uid: str) -> "_Srv":
            return _Srv()

    monkeypatch.setattr(plex_sync.client, "connect", lambda base_url, token: _Srv())
    client.put("/api/plex/settings", json={"base_url": "http://plex:32400", "token": "t"})

    pid = client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [t1]})
    client.patch(f"/api/playlists/{pid}", json={"target_plex_users": ["7"]})
    client.post(f"/api/playlists/{pid}/sync")  # plex == {"admin", "7"}

    # Untick user 7, then re-sync: user 7's copy must be cleaned up.
    client.patch(f"/api/playlists/{pid}", json={"target_plex_users": []})
    calls: list[tuple[str, list[str]]] = []

    def _record(config: object, title: str, targets: list[str]) -> dict[str, str]:
        calls.append((title, list(targets)))
        return {}

    monkeypatch.setattr(plex_sync, "delete_playlist_on_targets", _record)
    r = client.post(f"/api/playlists/{pid}/sync")
    assert r.status_code == 200
    assert calls == [("Mix", ["7"])]  # the de-targeted user gets cleaned up
    assert set(r.json()["plex"]) == {"admin"}  # state map no longer lists 7
