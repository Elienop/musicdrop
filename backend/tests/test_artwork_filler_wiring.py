"""App-level wiring for the background portrait filler.

Task 11 built ``ArtistImageFiller`` deliberately app-free. This file pins the
seam that makes it real: one filler per process, built in the lifespan, CLOSED
on shutdown, and announcing its fills through the same UNSCOPED ``art:changed``
emit every other artist-image mutation uses.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import app.main as main_mod
from app.api.artists import get_artist_image_filler
from app.artwork.filler import ArtistImageFiller
from app.artwork.service import ArtistImageService
from app.config import Settings
from app.config import settings as app_settings


def _pin_settings_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the lifespan at a throwaway beets dir and cache dir.

    ``backend/.env`` aims MUSICDROP_BEETS_DIR at the REAL dev library and the
    lifespan opens it for real, so a test that skips this pin runs against the
    developer's music library.
    """
    music = tmp_path / "music"
    music.mkdir()
    # A SIBLING of the music dir, not its parent: app.beets.store_layout refuses
    # a beets data directory that contains the music library, and this lifespan
    # runs that check.
    beets = tmp_path / "beets"
    beets.mkdir()
    (beets / "config.yaml").write_text(f"directory: {music}\nlibrary: library.db\nplugins: []\n")
    monkeypatch.setattr(app_settings, "beets_dir", str(beets))
    monkeypatch.setattr(app_settings, "artist_image_cache_dir", str(tmp_path / "cache"))


class _NeverResolvingService:
    """Stand-in for ArtistImageService whose resolve never finishes, so a fill
    started through it stays in flight until something cancels it."""

    async def get_artist_image(
        self, name: str, *, get_mbid: Callable[[], str | None] | None = None
    ) -> tuple[bytes, str] | None:
        await asyncio.Event().wait()  # nothing ever sets it; only cancellation ends this
        return None


async def _start_one_never_ending_fill(filler: ArtistImageFiller) -> None:
    """Leave exactly one resolve in flight on the app's own event loop."""
    service = cast(ArtistImageService, _NeverResolvingService())  # only get_artist_image is used
    # grace 0 means "do not wait": fill() returns at once and the task runs on.
    await filler.fill(service, "Nobody", get_mbid=lambda: None, grace_seconds=0.0)


def _request_for(app: FastAPI) -> Request:
    """The minimum ASGI scope ``get_artist_image_filler`` reads (``request.app``)."""
    return Request({"type": "http", "app": app, "headers": []})


def test_grace_default_is_a_short_positive_window() -> None:
    # Long enough that a single artist page still paints its portrait inline;
    # short enough that a cold 48-artist page does not sit on 48 open requests.
    assert Settings().artist_image_inline_grace_seconds == 1.5


def test_lifespan_builds_a_filler_and_closes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One filler per app, and the lifespan drains it on the way out.

    "inflight_count() == 0 after the block" is NOT the assertion, however
    obvious it looks: the portal's event loop cancels every straggler task as it
    tears down, so ``_run``'s ``finally`` empties the map whether or not anyone
    closed the filler (verified - dropping the close() call keeps that check
    green). What distinguishes a wired teardown is that the drain happens INSIDE
    the lifespan, while a fill is still running, and before the httpx client
    those fills fetch through is closed. So record the call itself.
    """
    drains: list[tuple[ArtistImageFiller, int, int]] = []
    original_close = ArtistImageFiller.close

    async def _recording_close(self: ArtistImageFiller) -> None:
        before = self.inflight_count()
        await original_close(self)
        drains.append((self, before, self.inflight_count()))

    monkeypatch.setattr(ArtistImageFiller, "close", _recording_close)
    _pin_settings_at(tmp_path, monkeypatch)
    with TestClient(main_mod.app) as client:
        filler = main_mod.app.state.artist_image_filler
        assert isinstance(filler, ArtistImageFiller)
        # A real resolve in flight, on the app's own loop, so the teardown has
        # something to drain rather than an already-empty map.
        portal = client.portal
        assert portal is not None
        portal.call(_start_one_never_ending_fill, filler)
        assert filler.inflight_count() == 1
        assert drains == []  # not before shutdown
    # Shut down cleanly: the filler the app published, closed once, and it took
    # the outstanding fill (1) down to none (0).
    assert drains == [(filler, 1, 0)]
    assert filler.inflight_count() == 0


def test_the_filler_notification_publishes_an_art_changed_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The filler's callback is what makes a background fill visible; a filler
    # wired to a no-op would leave every backfilled portrait on its monogram
    # until the user reloaded.
    published: list[str | None] = []

    class _Broker:
        def publish_art_changed(self, scope: str | None = None) -> None:
            published.append(scope)

    _pin_settings_at(tmp_path, monkeypatch)
    with TestClient(main_mod.app):
        monkeypatch.setattr(main_mod.app.state, "event_broker", _Broker(), raising=False)
        main_mod.app.state.artist_image_filler._notify()
    assert published == [None]  # UNSCOPED: the asset is keyed by normalized name


def test_the_dependency_caches_the_filler_it_falls_back_to() -> None:
    """Without a lifespan the dependency still yields a filler - the SAME one.

    ``TestClient(app)`` skips the lifespan, so a hard ``app.state`` read would
    make every route that wants a filler lifespan-dependent. But a fallback that
    returned a FRESH filler per request would be worse than no filler at all:
    each of a page's N requests for one artist would start its own resolve, and
    the single-flight this whole design rests on would be gone silently. So the
    identity, not the type, is the assertion.
    """
    app = FastAPI()
    first = get_artist_image_filler(_request_for(app))
    assert isinstance(first, ArtistImageFiller)
    assert get_artist_image_filler(_request_for(app)) is first
