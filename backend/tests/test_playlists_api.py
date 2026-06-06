import os

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
