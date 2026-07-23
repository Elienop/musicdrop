from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.artists import get_artist_art_write_toggle
from app.artwork.toggle import ArtistArtWriteToggle
from app.main import app


@pytest.fixture
def toggle(tmp_path: Path) -> ArtistArtWriteToggle:
    return ArtistArtWriteToggle(tmp_path / "art.json", default=False)


@pytest.fixture
def client(toggle: ArtistArtWriteToggle) -> Iterator[TestClient]:
    app.dependency_overrides[get_artist_art_write_toggle] = lambda: toggle
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_settings_get_put(client: TestClient) -> None:
    assert client.get("/api/artists/art/settings").json() == {"enabled": False}
    put = client.put("/api/artists/art/settings", json={"enabled": True})
    assert put.json() == {"enabled": True}
    assert client.get("/api/artists/art/settings").json() == {"enabled": True}


def test_apply_403_when_disabled(client: TestClient) -> None:
    r = client.post("/api/artists/art/apply", params={"name": "ABBA"})
    assert r.status_code == 403


def test_backfill_status_idle(client: TestClient) -> None:
    assert client.get("/api/artists/art/backfill").json()["phase"] == "idle"


def test_artist_art_runs_emit_an_unscoped_art_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artist-art runs emit an UNSCOPED completion event — both the sweep (it
    repaints many artists) and a single-artist run (a raw display name is not a
    reliable identity for the NORMALIZED-name-keyed image; see api/artists.py)."""
    from types import SimpleNamespace

    import app.api.artists as artists_mod
    from app.artist_art_jobs.registry import ArtistArtBackfillRegistry

    captured: dict[str, object] = {}
    emitted: list[str | None] = []

    def fake_start(reg: object, lib: object, **kwargs: object) -> None:
        captured["on_complete"] = kwargs["on_complete"]

    monkeypatch.setattr(artists_mod, "start_art_backfill", fake_start)
    monkeypatch.setattr(
        artists_mod, "emit_art_changed", lambda app, scope=None: emitted.append(scope)
    )
    stub_app = SimpleNamespace(state=SimpleNamespace(settings=None))
    reg = ArtistArtBackfillRegistry()

    artists_mod._start(stub_app, reg, object(), force=True, artist="ABBA")
    captured["on_complete"]()  # type: ignore[operator]  # captured callback is callable
    assert emitted == [None]  # single-artist run -> still global

    emitted.clear()
    artists_mod._start(stub_app, reg, object(), force=False, artist=None)
    captured["on_complete"]()  # type: ignore[operator]  # captured callback is callable
    assert emitted == [None]  # full sweep -> global
