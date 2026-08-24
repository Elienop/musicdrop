"""Route tests for POST /api/artists/rename(/preview)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from beets.library import Library
from fastapi.testclient import TestClient

from app.api.artists import get_artist_art_write_toggle, get_artist_image_cache
from app.api.playlists import get_playlists_dir
from app.artist_art_jobs.registry import get_artist_art_backfill
from app.artwork.cache import ArtistImageCache, CachedImage
from app.main import app
from tests.conftest import make_test_handle


class _ToggleOff:
    def is_enabled(self) -> bool:
        return False


class _ToggleOn:
    def is_enabled(self) -> bool:
        return True


class _RegistryUnused:
    """A registry stub the not_needed paths never touch; start() failing loudly
    pins that the art job is NOT kicked when nothing moved / the toggle is off."""

    def start(self, **kwargs: object) -> None:
        raise AssertionError("art job must not be started")


class _RegistryRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def start(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


class _RegistryBusy:
    def start(self, **kwargs: object) -> None:
        raise RuntimeError("an artist-art backfill is already running")


@pytest.fixture
def rename_client(
    rename_lib: Library, tmp_path: Path
) -> Iterator[tuple[TestClient, ArtistImageCache, Path]]:
    handle = make_test_handle(rename_lib, tmp_path)
    cache = ArtistImageCache(tmp_path / "artcache")
    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()
    app.state.beets_library = handle
    app.dependency_overrides[get_artist_image_cache] = lambda: cache
    app.dependency_overrides[get_artist_art_write_toggle] = lambda: _ToggleOff()
    app.dependency_overrides[get_artist_art_backfill] = lambda: _RegistryUnused()
    app.dependency_overrides[get_playlists_dir] = lambda: playlists_dir
    try:
        yield TestClient(app), cache, playlists_dir
    finally:
        app.dependency_overrides.clear()


def test_preview_returns_albums_and_merge(
    rename_client: tuple[TestClient, ArtistImageCache, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import beets.ui

    monkeypatch.setattr(beets.ui, "should_move", lambda _opt: True)
    client, _, _ = rename_client
    r = client.post("/api/artists/rename/preview", json={"name": "Fayrouz", "new_name": "Fairuz"})
    assert r.status_code == 200
    body = r.json()
    assert sorted(a["title"] for a in body["albums"]) == ["Best Of", "Live"]
    assert body["merge"] == {"existing_album_count": 1}
    assert body["move_enabled"] is True


def test_preview_404_for_unknown_artist(
    rename_client: tuple[TestClient, ArtistImageCache, Path],
) -> None:
    client, _, _ = rename_client
    r = client.post("/api/artists/rename/preview", json={"name": "Nobody", "new_name": "Somebody"})
    assert r.status_code == 404


def test_422_for_same_name(
    rename_client: tuple[TestClient, ArtistImageCache, Path],
) -> None:
    client, _, _ = rename_client
    r = client.post("/api/artists/rename/preview", json={"name": "Fairuz", "new_name": " Fairuz "})
    assert r.status_code == 422


def test_apply_renames_rekeys_portrait_and_reexports(
    rename_client: tuple[TestClient, ArtistImageCache, Path],
    rename_lib: Library,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import beets.ui

    from app.beets.library import _require_id
    from app.playlists import store
    from app.playlists.store import StoredEntry

    monkeypatch.setattr(beets.ui, "should_move", lambda _opt: True)
    monkeypatch.setattr(beets.ui, "should_write", lambda _opt: True)
    client, cache, playlists_dir = rename_client

    cache.store_positive("Fayrouz", b"portrait", "image/jpeg")
    item = next(i for i in rename_lib.items() if str(i.albumartist) == "Fayrouz")
    record = store.create_playlist(
        playlists_dir,
        name="Mix",
        entries=[StoredEntry(uid="u1", item_id=_require_id(item.id))],
    )

    r = client.post("/api/artists/rename", json={"name": "Fayrouz", "new_name": "Fairuz"})
    assert r.status_code == 200
    body = r.json()
    assert [a["outcome"] for a in body["albums"]] == ["renamed", "renamed"]
    assert body["old_name_remaining_albums"] == 0
    assert body["portrait"] == "moved"
    assert body["playlists_reexported"] == 1
    assert body["artist_art_job"] == "not_needed"  # toggle is off

    got = cache.get("Fairuz")
    assert isinstance(got, CachedImage) and got.data == b"portrait"
    import os

    export = Path(os.fsdecode(rename_lib.directory)) / ".playlists" / f"{record.id}.m3u8"
    assert export.exists()
    assert "Fairuz/" in export.read_text(encoding="utf-8", errors="surrogateescape")


def test_apply_409_while_a_library_job_runs(
    rename_client: tuple[TestClient, ArtistImageCache, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.library_busy

    monkeypatch.setattr(app.library_busy, "library_job_active", lambda **kw: True)
    client, _, _ = rename_client
    r = client.post("/api/artists/rename", json={"name": "Fayrouz", "new_name": "Fairuz"})
    assert r.status_code == 409


def test_apply_emits_library_changed(
    rename_client: tuple[TestClient, ArtistImageCache, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.artists as artists_mod

    calls: list[str] = []
    monkeypatch.setattr(artists_mod, "emit_library_changed", lambda _app: calls.append("lib"))
    monkeypatch.setattr(
        artists_mod, "emit_art_changed", lambda _app, scope=None: calls.append("art")
    )
    client, cache, _ = rename_client
    cache.store_positive("Fayrouz", b"x", "image/jpeg")
    r = client.post("/api/artists/rename", json={"name": "Fayrouz", "new_name": "Fairuz"})
    assert r.status_code == 200
    assert "lib" in calls and "art" in calls


def test_apply_kicks_the_art_job_when_files_moved_and_toggle_on(
    rename_client: tuple[TestClient, ArtistImageCache, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import beets.ui

    import app.api.artists as artists_mod

    monkeypatch.setattr(beets.ui, "should_move", lambda _opt: True)
    monkeypatch.setattr(beets.ui, "should_write", lambda _opt: True)
    reg = _RegistryRecorder()
    app.dependency_overrides[get_artist_art_write_toggle] = lambda: _ToggleOn()
    app.dependency_overrides[get_artist_art_backfill] = lambda: reg
    started: list[str] = []
    monkeypatch.setattr(
        artists_mod,
        "_start",
        lambda _app, _reg, _lib, *, force, artist: started.append(str(artist)),
    )
    client, _, _ = rename_client
    r = client.post("/api/artists/rename", json={"name": "Fayrouz", "new_name": "Fairuz"})
    assert r.status_code == 200
    assert r.json()["artist_art_job"] == "started"
    assert reg.calls and reg.calls[0]["artist"] == "Fairuz"
    assert started == ["Fairuz"]


def test_all_drifted_batch_does_not_rekey_the_portrait(
    rename_client: tuple[TestClient, ArtistImageCache, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An all-skipped_drifted batch empties the old name without renaming
    anything, so the old key is still live: the portrait must NOT be re-keyed."""
    import app.api.artists as artists_mod
    from app.beets.rename import ArtistRenameApplyOutcome
    from app.models.rename import ArtistRenameAlbumResult

    fake = ArtistRenameApplyOutcome(
        albums=[
            ArtistRenameAlbumResult(album_id=1, title="Best Of", outcome="skipped_drifted"),
            ArtistRenameAlbumResult(album_id=2, title="Live", outcome="skipped_drifted"),
        ],
        old_name_remaining_albums=0,
        moved_item_ids=[],
    )

    async def _fake_op(_req: object, _payload: object) -> object:
        return fake

    calls: list[str] = []
    monkeypatch.setattr(artists_mod, "emit_library_changed", lambda _app: calls.append("lib"))
    monkeypatch.setattr(
        artists_mod, "emit_art_changed", lambda _app, scope=None: calls.append("art")
    )
    monkeypatch.setattr(artists_mod, "apply_artist_rename_op", _fake_op)
    client, cache, _ = rename_client
    cache.store_positive("Fayrouz", b"portrait", "image/jpeg")

    r = client.post("/api/artists/rename", json={"name": "Fayrouz", "new_name": "Fairuz"})
    assert r.status_code == 200
    body = r.json()
    assert body["portrait"] == "not_rekeyed"
    # The old name's cache entry is untouched; nothing was moved to the new key.
    got = cache.get("Fayrouz")
    assert isinstance(got, CachedImage) and got.data == b"portrait"
    assert cache.get("Fairuz") is None
    # Nothing moved, so the art job is not kicked either (its stub would raise).
    assert body["artist_art_job"] == "not_needed"
    assert body["playlists_reexported"] == 0
    assert calls == ["lib"]  # art:changed must NOT fire when nothing was re-keyed


def test_apply_reports_skipped_busy_and_never_starts_the_runner(
    rename_client: tuple[TestClient, ArtistImageCache, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost slot claim = skipped_busy, and _start must NOT run for a claim we did not win."""
    import beets.ui

    import app.api.artists as artists_mod

    monkeypatch.setattr(beets.ui, "should_move", lambda _opt: True)
    monkeypatch.setattr(beets.ui, "should_write", lambda _opt: True)
    app.dependency_overrides[get_artist_art_write_toggle] = lambda: _ToggleOn()
    app.dependency_overrides[get_artist_art_backfill] = lambda: _RegistryBusy()
    started: list[str] = []
    monkeypatch.setattr(
        artists_mod,
        "_start",
        lambda _app, _reg, _lib, *, force, artist: started.append(str(artist)),
    )
    client, _, _ = rename_client
    r = client.post("/api/artists/rename", json={"name": "Fayrouz", "new_name": "Fairuz"})
    assert r.status_code == 200
    assert r.json()["artist_art_job"] == "skipped_busy"
    assert started == []
