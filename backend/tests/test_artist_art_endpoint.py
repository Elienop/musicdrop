from collections.abc import Iterator
from pathlib import Path

import pytest
from beets.library import Library
from fastapi.testclient import TestClient

from app.api.artists import get_artist_art_write_toggle
from app.artwork.toggle import ArtistArtWriteToggle
from app.main import app
from tests.conftest import beets_dir_for, make_test_handle


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


def test_apply_asks_for_a_forcing_run_and_backfill_does_not(
    monkeypatch: pytest.MonkeyPatch, toggle: ArtistArtWriteToggle, edit_lib: Library, tmp_path: Path
) -> None:
    """The two routes differ in exactly one argument, and it is the destructive one.

    Apply is the per-artist REPLACE (June 2026 ruling), so it forces; the library
    backfill fills gaps and must never replace. ``force`` reaches both the slot
    claim and the job, and the job is what moves a replaced file to Trash."""
    import app.api.artists as artists_mod
    from app.artist_art_jobs.registry import ArtistArtBackfillRegistry, get_artist_art_backfill

    class _Recorder(ArtistArtBackfillRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.claims: list[bool] = []

        def start(
            self, *, force: bool, artist: str | None = None, scope_label: str = "library"
        ) -> str:
            self.claims.append(force)
            return "job"

    reg = _Recorder()
    jobs: list[bool] = []
    monkeypatch.setattr(
        artists_mod, "_start", lambda _app, _reg, _lib, *, force, artist: jobs.append(force)
    )
    toggle.set_enabled(True)
    app.dependency_overrides[get_artist_art_write_toggle] = lambda: toggle
    app.dependency_overrides[get_artist_art_backfill] = lambda: reg
    # Both routes read ``app.state.beets_library`` before they start anything.
    monkeypatch.setattr(
        app.state,
        "beets_library",
        make_test_handle(edit_lib, beets_dir_for(tmp_path)),
        raising=False,
    )
    try:
        client = TestClient(app)
        assert client.post("/api/artists/art/apply", params={"name": "ABBA"}).status_code == 200
        assert client.post("/api/artists/art/backfill").status_code == 200
    finally:
        app.dependency_overrides.clear()

    assert reg.claims == [True, False]
    assert jobs == [True, False]


def test_a_forced_job_is_handed_a_resolver_for_the_checked_trash_store(
    monkeypatch: pytest.MonkeyPatch, edit_lib: Library, tmp_path: Path
) -> None:
    """The job thread has no Settings/handle pair, so the start site hands it a
    resolver for the same checked pair every delete path takes. A CALLABLE, not
    a resolved pair: ``checked_store_dirs`` checks at the moment of use and the
    job asks it again per artist. Without it a forced run could not move a
    replaced file aside, and would refuse to write at all."""
    from types import SimpleNamespace
    from typing import Any

    import app.api.artists as artists_mod
    from app.artist_art_jobs.registry import ArtistArtBackfillRegistry
    from app.beets.artist_art import ArtTrashStore
    from app.beets.store_layout import checked_store_dirs
    from app.config import settings

    resolvers: list[Any] = []
    monkeypatch.setattr(
        artists_mod,
        "start_art_backfill",
        lambda _reg, _lib, **kw: resolvers.append(kw["resolve_trash"]),
    )
    handle = make_test_handle(edit_lib, beets_dir_for(tmp_path))
    stub_app = SimpleNamespace(state=SimpleNamespace(settings=None, beets_library=handle))
    reg = ArtistArtBackfillRegistry()

    artists_mod._start(stub_app, reg, edit_lib, force=True, artist="ABBA")
    artists_mod._start(stub_app, reg, edit_lib, force=False, artist=None)

    trash_dir, origins_dir = checked_store_dirs(settings, handle)
    forced, unforced = resolvers
    assert unforced is None  # the skip-existing sweep replaces nothing
    assert forced() == ArtTrashStore(trash_dir=trash_dir, origins_dir=origins_dir)


def test_a_refused_store_layout_still_starts_the_job_with_no_store(
    monkeypatch: pytest.MonkeyPatch, edit_lib: Library, tmp_path: Path
) -> None:
    """A refused layout is not a 503 on this route: the job starts, and the run
    reports every folder whose art it would have replaced as failed rather than
    replacing it. Declaring a new status here would change the contract."""
    from types import SimpleNamespace
    from typing import Any

    import app.api.artists as artists_mod
    from app.artist_art_jobs.registry import ArtistArtBackfillRegistry
    from app.beets.store_layout import StoreLayoutError

    def refuse(*_a: object, **_kw: object) -> tuple[Path, Path]:
        raise StoreLayoutError("Trash is inside the music library")

    resolvers: list[Any] = []
    monkeypatch.setattr(artists_mod, "checked_store_dirs", refuse)
    monkeypatch.setattr(
        artists_mod,
        "start_art_backfill",
        lambda _reg, _lib, **kw: resolvers.append(kw["resolve_trash"]),
    )
    handle = make_test_handle(edit_lib, beets_dir_for(tmp_path))
    stub_app = SimpleNamespace(state=SimpleNamespace(settings=None, beets_library=handle))

    artists_mod._start(stub_app, ArtistArtBackfillRegistry(), edit_lib, force=True, artist="ABBA")

    assert resolvers[0]() is None  # started, with nowhere to put a replaced file
