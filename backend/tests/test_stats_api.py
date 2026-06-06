from fastapi.testclient import TestClient


def test_get_stats_empty_library(client: TestClient) -> None:
    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["stats"] == {
        "track_count": 0,
        "album_count": 0,
        "artist_count": 0,
        "total_seconds": 0.0,
        "total_bytes": 0,
    }
    assert body["recently_added"] == []
    assert body["size_is_estimate"] is True
