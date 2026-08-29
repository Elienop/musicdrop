from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.beets.library import LibraryHandle, _require_id
from app.models.playlist_import import ParsedPlaylist, SourceEntry
from app.playlists import store
from app.playlists.store import get_playlists_dir


def _dir() -> Path:
    """The owned-playlist store dir the API resolves from settings (same dir the
    ``client``/``beets_library`` fixtures point at via the tmp ``beets_dir``)."""
    return get_playlists_dir()


def _seed(
    lib: Any,
    *,
    title: str,
    artist: str,
    album: str = "Album",
    length: float = 200.0,
    filename: str | None = None,
) -> int:
    from beets.library import Item

    item = Item(
        title=title,
        artist=artist,
        albumartist=artist,
        album=album,
        length=length,
        path=(f"/lib/{artist}/{filename or title}.mp3").encode(),
    )
    item.add(lib)
    return _require_id(item.id)


def test_preview_from_files_matches_and_counts(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    item_id = _seed(
        beets_library.lib,
        title="Around the World",
        artist="Daft Punk",
        filename="01 Around the World",
    )
    body = {
        "files": [
            {
                "name": "Road.m3u8",
                "content": (
                    "#EXTM3U\n#EXTINF:213,Daft Punk - Around the World\n"
                    "/old/01 Around the World.mp3\n/old/unknown.mp3\n"
                ),
            }
        ]
    }
    r = client.post("/api/playlists/import/preview", json=body)
    assert r.status_code == 200
    (pl,) = r.json()["playlists"]
    assert pl["name"] == "Road"
    assert pl["matched_count"] == 1
    assert pl["unmatched_count"] == 1
    assert pl["entries"][0]["item_id"] == item_id
    assert pl["entries"][1]["status"] == "unmatched"


def test_preview_mints_distinct_preview_ids(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    body = {
        "files": [
            {"name": "Road.m3u8", "content": "#EXTM3U\nA - B\n"},
            {"name": "Chill.m3u8", "content": "#EXTM3U\nA - B\n"},
        ]
    }
    r = client.post("/api/playlists/import/preview", json=body)
    assert r.status_code == 200
    ids = [p["preview_id"] for p in r.json()["playlists"]]
    assert len(ids) == 2
    assert len(set(ids)) == 2
    assert all(ids)


def test_preview_requires_exactly_one_source(client: TestClient) -> None:
    assert client.post("/api/playlists/import/preview", json={}).status_code == 422
    assert (
        client.post(
            "/api/playlists/import/preview",
            json={"files": [{"name": "a", "content": ""}], "plex_rating_keys": ["x"]},
        ).status_code
        == 422
    )


def test_preview_from_plex_uses_the_puller(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    parsed = ParsedPlaylist(
        name="Road", entries=[SourceEntry(position=0, title="T", source="plex:Road")]
    )
    seen: list[list[str]] = []

    def fake_pull(config: Any, rating_keys: list[str]) -> list[ParsedPlaylist]:
        seen.append(rating_keys)
        return [parsed]

    monkeypatch.setattr("app.api.playlists.playlists_pull.pull_playlist_entries", fake_pull)
    r = client.post("/api/playlists/import/preview", json={"plex_rating_keys": ["77"]})
    assert r.status_code == 200
    assert r.json()["playlists"][0]["name"] == "Road"
    # The puller is addressed by ratingKey — titles aren't unique on Plex.
    assert seen == [["77"]]


def test_commit_creates_playlists_with_pending_and_suffixes_collisions(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    item_id = _seed(beets_library.lib, title="Real", artist="A")
    store.create_playlist(_dir(), name="Road")  # collision
    body = {
        "playlists": [
            {
                "name": "Road",
                "entries": [
                    {"item_id": item_id},
                    {"pending": {"artist": "X", "title": "Lost", "source": "/old/x.mp3"}},
                ],
            }
        ]
    }
    r = client.post("/api/playlists/import", json=body)
    assert r.status_code == 200
    (created,) = r.json()["created"]
    assert created["name"] == "Road (2)"
    assert created["track_count"] == 1
    assert created["pending_count"] == 1
    record = store.get_playlist(_dir(), created["id"])
    assert record is not None
    assert record.entries[0].item_id == item_id
    assert record.entries[1].pending is not None
    assert record.entries[1].pending.title == "Lost"


def test_commit_partial_success_when_one_playlist_fails(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-loop store failure must not strand the already-created playlists nor
    500 the request — the first is created and the second is reported failed."""
    item_id = _seed(beets_library.lib, title="Real", artist="A")
    real_create = store.create_playlist

    def flaky_create(playlists_dir: Path, **kwargs: Any) -> Any:
        if kwargs.get("name") == "Second":
            raise OSError("disk full")
        return real_create(playlists_dir, **kwargs)

    monkeypatch.setattr("app.api.playlists.store.create_playlist", flaky_create)
    body = {
        "playlists": [
            {"name": "First", "entries": [{"item_id": item_id}]},
            {"name": "Second", "entries": [{"item_id": item_id}]},
        ]
    }
    r = client.post("/api/playlists/import", json=body)
    assert r.status_code == 200
    payload = r.json()
    assert [p["name"] for p in payload["created"]] == ["First"]
    assert [f["name"] for f in payload["failed"]] == ["Second"]
    # the first playlist really persisted despite the second failing
    assert any(rec.name == "First" for rec in store.list_playlists(_dir()))


def test_commit_pulls_plex_poster_by_rating_key(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Plex-sourced playlist seeds its cover from the source playlist resolved
    by ratingKey — the title rides along only as the legacy fallback."""
    item_id = _seed(beets_library.lib, title="Real", artist="A")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    seen: list[tuple[str | None, str | None]] = []

    def fake_download(
        config: Any, rating_key: str | None, title: str | None = None
    ) -> tuple[bytes, str]:
        seen.append((rating_key, title))
        return png, "png"

    monkeypatch.setattr("app.api.playlists.playlists_pull.download_poster", fake_download)
    body = {
        "playlists": [
            {
                "name": "Road",
                "plex_source": "Road",
                "plex_rating_key": "22",
                "entries": [{"item_id": item_id}],
            },
        ]
    }
    r = client.post("/api/playlists/import", json=body)
    assert r.status_code == 200
    (created,) = r.json()["created"]
    assert created["artwork_hash"] is not None
    assert seen == [("22", "Road")]
    record = store.get_playlist(_dir(), created["id"])
    assert record is not None
    assert record.artwork is not None
    assert record.artwork.format == "png"


def test_commit_pulls_plex_poster_from_a_legacy_title_only_playlist(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Back-compat: a body minted before rating keys carries only ``plex_source``
    — the pull still runs, with no key, and the puller falls back to the title."""
    item_id = _seed(beets_library.lib, title="Real", artist="A")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    seen: list[tuple[str | None, str | None]] = []

    def fake_download(
        config: Any, rating_key: str | None, title: str | None = None
    ) -> tuple[bytes, str]:
        seen.append((rating_key, title))
        return png, "png"

    monkeypatch.setattr("app.api.playlists.playlists_pull.download_poster", fake_download)
    body = {
        "playlists": [
            {"name": "Road", "plex_source": "Road", "entries": [{"item_id": item_id}]},
        ]
    }
    r = client.post("/api/playlists/import", json=body)
    assert r.status_code == 200
    (created,) = r.json()["created"]
    assert created["artwork_hash"] is not None
    assert seen == [(None, "Road")]


def test_commit_skips_poster_pull_without_any_plex_source(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither ``plex_rating_key`` nor ``plex_source`` -> no pull, no cover."""
    item_id = _seed(beets_library.lib, title="Real", artist="A")

    def fail_download(
        config: Any, rating_key: str | None, title: str | None = None
    ) -> tuple[bytes, str]:
        raise AssertionError("download_poster must not be called for a non-Plex import")

    monkeypatch.setattr("app.api.playlists.playlists_pull.download_poster", fail_download)
    body = {"playlists": [{"name": "Road", "entries": [{"item_id": item_id}]}]}
    r = client.post("/api/playlists/import", json=body)
    assert r.status_code == 200
    (created,) = r.json()["created"]
    assert created["artwork_hash"] is None


def test_commit_survives_poster_download_failure(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poster pull that raises must not fail the import — the playlist is still
    created, just without a cover (best-effort art)."""
    item_id = _seed(beets_library.lib, title="Real", artist="A")

    def boom(config: Any, rating_key: str | None, title: str | None = None) -> tuple[bytes, str]:
        raise RuntimeError("plex unreachable")

    monkeypatch.setattr("app.api.playlists.playlists_pull.download_poster", boom)
    body = {
        "playlists": [
            {
                "name": "Road",
                "plex_source": "Road",
                "plex_rating_key": "22",
                "entries": [{"item_id": item_id}],
            },
        ]
    }
    r = client.post("/api/playlists/import", json=body)
    assert r.status_code == 200
    (created,) = r.json()["created"]
    assert created["artwork_hash"] is None
    record = store.get_playlist(_dir(), created["id"])
    assert record is not None
    assert record.artwork is None


def test_commit_rejects_unknown_item_ids(client: TestClient, beets_library: LibraryHandle) -> None:
    body = {"playlists": [{"name": "P", "entries": [{"item_id": 987654}]}]}
    r = client.post("/api/playlists/import", json=body)
    assert r.status_code == 422
    assert "unknown item ids: 987654" in r.json()["detail"]


def test_commit_entry_requires_exactly_one_of(client: TestClient) -> None:
    body = {"playlists": [{"name": "P", "entries": [{}]}]}
    assert client.post("/api/playlists/import", json=body).status_code == 422
