"""The artist-IMAGE mutations refuse while the artist-art sweep is running.

Three routes write the artist-image cache directory - upload, set-from-URL and
reset - and exactly ONE background job touches those same files: the artist-art
sweep, which builds its own ``ArtistImageCache`` over the same dir.

The gate is deliberately NARROWER than ``_gate_library_busy``. An import or a
reorganize can run for many minutes and cannot touch a single one of these
files, so 409ing a portrait upload for its whole length would be a cost with no
matching risk. ``test_an_unrelated_library_job_does_not_block_an_image_edit``
pins that choice so nobody "fixes" it into the full union later.

It is a COHERENCE guard, not a corruption guard: cache writes are atomic (tmp +
``os.replace``) and the override slot always beats the positive one, so nothing
here can be read half-written. What it prevents is a nonsense OUTCOME - a reset
that clears the automatic slot while the sweep is mid-resolve for that same
artist is immediately undone by the sweep's own ``store_positive``, and
``POST /artists/art/apply`` copies whatever the cache holds into the music
folder, so an image changing under it makes the file it writes nondeterministic.

The FETCH route is deliberately NOT gated - it writes nothing. It shares the
service's rate limiter with the sweep, which paces it; it does not conflict with
it, and gating it would remove the user's only way to LOOK at a candidate.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.api.artists as artists_mod
from app.api.albums import get_library
from app.api.artists import (
    get_artist_image_cache,
    get_artist_image_http_client,
    get_artist_image_service,
    get_artist_image_sources,
)
from app.artist_art_jobs.registry import get_artist_art_backfill
from app.artwork.cache import ArtistImageCache
from app.artwork.factory import DEEZER, ArtistImageSources
from app.artwork.rate_limit import TokenBucketLimiter
from app.artwork.source import ResolvedImage
from app.beets import library as library_mod
from app.main import app

PNG = Path(__file__).parent / "fixtures" / "cover.png"


class _StubHandle:
    """Minimal LibraryHandle stand-in; ``.lib`` only reaches the patched mbid lookup."""

    lib = object()


class _StubService:
    """Only what these routes read: the toggle and a rate/concurrency slot.

    Reported ENABLED so the fetch route runs its real body - an ``is_enabled()``
    of False would 403 before the gate's absence could be observed, and the
    not-gated test would then pass for the wrong reason.
    """

    def __init__(self) -> None:
        self._limiter = TokenBucketLimiter(rate_per_sec=1000.0, max_concurrency=2)

    def is_enabled(self) -> bool:
        return True

    def limiter_slot(self) -> object:
        return self._limiter.slot()


class _StubSource:
    """Records that the fetch route really reached a source."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def resolve(self, name: str, *, mbid: str | None = None) -> ResolvedImage | None:
        self.calls.append(name)
        return ResolvedImage(data=PNG.read_bytes(), content_type="image/png")


@pytest.fixture
def cache(tmp_path: Path) -> ArtistImageCache:
    return ArtistImageCache(tmp_path)


@pytest.fixture
def source() -> _StubSource:
    return _StubSource()


@pytest.fixture
def client(
    cache: ArtistImageCache, source: _StubSource, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """``TestClient(app)`` skips the lifespan, so every ``app.state`` read is overridden.

    The reset route resolves a service and a library handle (it kicks the
    background refill), and the fetch route resolves a source registry too - all
    three would explode on an unset ``app.state`` rather than reach the gate.
    """
    app.dependency_overrides[get_artist_image_cache] = lambda: cache
    app.dependency_overrides[get_artist_image_http_client] = lambda: object()
    app.dependency_overrides[get_artist_image_service] = lambda: _StubService()
    app.dependency_overrides[get_artist_image_sources] = lambda: ArtistImageSources(
        ordered=((DEEZER, source),)
    )
    app.dependency_overrides[get_library] = lambda: _StubHandle()
    monkeypatch.setattr(library_mod, "get_artist_mbid", lambda lib, name: None)
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def sweeping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artists_mod, "artist_art_backfill_active", lambda: True)


def test_upload_is_409_while_the_artist_art_sweep_runs(
    client: TestClient, cache: ArtistImageCache, sweeping: None
) -> None:
    resp = client.post(
        "/api/artists/image/override",
        params={"name": "ABBA"},
        files={"file": ("p.png", PNG.read_bytes(), "image/png")},
    )
    assert resp.status_code == 409
    assert "artist art" in resp.json()["detail"].lower()
    assert cache.get("ABBA") is None  # refused BEFORE the write


def test_from_url_is_409_while_the_artist_art_sweep_runs(
    client: TestClient, sweeping: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(http_client: object, url: str) -> bytes:
        raise AssertionError("the gate must refuse before any outbound fetch")

    monkeypatch.setattr(artists_mod, "fetch_image_bytes", fake_fetch)
    resp = client.post(
        "/api/artists/image/override/from-url",
        params={"name": "ABBA"},
        json={"url": "https://example.test/a.png"},
    )
    assert resp.status_code == 409


def test_reset_is_409_while_the_artist_art_sweep_runs(
    client: TestClient, cache: ArtistImageCache, sweeping: None
) -> None:
    cache.write_override("ABBA", b"manual", "image/png")
    resp = client.post("/api/artists/image/reset", params={"name": "ABBA"})
    assert resp.status_code == 409
    # The reset was refused, not half-applied.
    assert cache.get("ABBA") is not None


def test_the_gate_reads_the_REAL_registry_not_just_a_patched_name(
    client: TestClient, cache: ArtistImageCache
) -> None:
    """The three tests above patch a module name; this one starts a real job.

    Without it, the gate could consult a name nothing ever sets and every one of
    those tests would still be green. The autouse ``reset_artist_art_backfill``
    fixture in conftest clears the slot afterwards.
    """
    get_artist_art_backfill().start(force=False)
    resp = client.post(
        "/api/artists/image/override",
        params={"name": "ABBA"},
        files={"file": ("p.png", PNG.read_bytes(), "image/png")},
    )
    assert resp.status_code == 409
    assert cache.get("ABBA") is None


def test_fetch_is_not_gated_because_it_writes_nothing(
    client: TestClient, source: _StubSource, sweeping: None
) -> None:
    # A preview mutates no slot, so it has nothing to collide with; it only
    # shares the rate limiter, which paces it. Asserted as a 200 that really
    # reached the source rather than as "not 409" - this route has two 409s of
    # its very own (unconfigured source, unstorable format), so a bare
    # inequality would go green on an unrelated refusal.
    resp = client.post("/api/artists/image/fetch", params={"name": "ABBA", "source": "deezer"})
    assert resp.status_code == 200
    assert source.calls == ["ABBA"]


def test_mutations_are_allowed_when_no_sweep_is_running(
    client: TestClient, cache: ArtistImageCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(artists_mod, "artist_art_backfill_active", lambda: False)
    resp = client.post(
        "/api/artists/image/override",
        params={"name": "ABBA"},
        files={"file": ("p.png", PNG.read_bytes(), "image/png")},
    )
    assert resp.status_code == 200


def test_an_unrelated_library_job_does_not_block_an_image_edit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Deliberately NARROWER than _gate_library_busy: an import can run for many
    # minutes and touches none of these files.
    import app.library_busy as library_busy

    monkeypatch.setattr(library_busy, "library_job_active", lambda **kwargs: True)
    monkeypatch.setattr(artists_mod, "artist_art_backfill_active", lambda: False)
    resp = client.post(
        "/api/artists/image/override",
        params={"name": "ABBA"},
        files={"file": ("p.png", PNG.read_bytes(), "image/png")},
    )
    assert resp.status_code == 200
