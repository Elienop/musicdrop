from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import respx
from fastapi.concurrency import run_in_threadpool as _real_run_in_threadpool
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.api.artists import get_artist_image_service
from app.artwork.cache import ArtistImageCache
from app.artwork.deezer import DeezerArtistImageSource
from app.artwork.rate_limit import TokenBucketLimiter
from app.artwork.service import ArtistImageService
from app.main import app


class _StubService:
    """Stub standing in for ArtistImageService (no real Deezer/HTTP)."""

    def __init__(self, result: tuple[bytes, str] | None) -> None:
        self._result = result
        self.calls: list[str] = []

    async def get_artist_image(
        self, name: str, *, get_mbid: object = None
    ) -> tuple[bytes, str] | None:
        self.calls.append(name)
        return self._result


class _StubHandle:
    """Minimal LibraryHandle stand-in; only ``.lib`` is read by the endpoint."""

    lib = object()


def _client_with(service: object) -> Iterator[TestClient]:
    app.dependency_overrides[get_artist_image_service] = lambda: service
    app.dependency_overrides[get_library] = lambda: _StubHandle()
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def hit_client() -> Iterator[TestClient]:
    yield from _client_with(_StubService((b"JPEGBYTES", "image/jpeg")))


@pytest.fixture
def miss_client() -> Iterator[TestClient]:
    yield from _client_with(_StubService(None))


def test_hit_returns_image_bytes_and_media_type(hit_client: TestClient) -> None:
    resp = hit_client.get("/api/artists/image", params={"name": "ABBA"})
    assert resp.status_code == 200
    assert resp.content == b"JPEGBYTES"
    assert resp.headers["content-type"] == "image/jpeg"


def test_hit_revalidates_with_etag(hit_client: TestClient) -> None:
    # The portrait must revalidate so a freshly-set override shows up without a
    # hard refresh; the content-derived ETag keeps that revalidation cheap.
    resp = hit_client.get("/api/artists/image", params={"name": "ABBA"})
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["etag"]


def test_matching_if_none_match_returns_304(hit_client: TestClient) -> None:
    first = hit_client.get("/api/artists/image", params={"name": "ABBA"})
    etag = first.headers["etag"]
    second = hit_client.get(
        "/api/artists/image",
        params={"name": "ABBA"},
        headers={"If-None-Match": etag},
    )
    assert second.status_code == 304
    assert second.content == b""
    # The 304 re-asserts the validator + cache policy so the cache entry refreshes.
    assert second.headers["etag"] == etag
    assert second.headers["cache-control"] == "no-cache"


def test_weak_if_none_match_returns_304(hit_client: TestClient) -> None:
    # A gzip-enabling reverse proxy (nginx) weakens our strong ETag to W/"...".
    # The browser echoes that back, and RFC 9110 weak comparison must still match
    # — otherwise every view re-downloads the image instead of getting a 304.
    first = hit_client.get("/api/artists/image", params={"name": "ABBA"})
    weak = "W/" + first.headers["etag"]
    second = hit_client.get(
        "/api/artists/image",
        params={"name": "ABBA"},
        headers={"If-None-Match": weak},
    )
    assert second.status_code == 304


def test_stale_if_none_match_returns_fresh_bytes(hit_client: TestClient) -> None:
    # A non-matching validator (e.g. a recycled ?v= URL pointing at new bytes)
    # must return the current image, not a 304 — this is the hard-refresh bug.
    resp = hit_client.get(
        "/api/artists/image",
        params={"name": "ABBA"},
        headers={"If-None-Match": '"stale-etag"'},
    )
    assert resp.status_code == 200
    assert resp.content == b"JPEGBYTES"


def test_slash_in_name_works_as_query_param(hit_client: TestClient) -> None:
    # "AC/DC" must round-trip via the query param (no %2F-in-path footgun).
    resp = hit_client.get("/api/artists/image", params={"name": "AC/DC"})
    assert resp.status_code == 200
    assert resp.content == b"JPEGBYTES"


def test_miss_returns_404(miss_client: TestClient) -> None:
    resp = miss_client.get("/api/artists/image", params={"name": "Nobody"})
    assert resp.status_code == 404


def test_missing_name_param_is_422(hit_client: TestClient) -> None:
    resp = hit_client.get("/api/artists/image")
    assert resp.status_code == 422


def test_default_settings_disabled_endpoint_404s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point the artist-image cache dir at a fresh tmp so a developer's persisted
    # enabled toggle (data/cache/artist-images/_enabled.json) can't leak in and
    # flip the feature on — the env default (artist_images_enabled=False) then
    # governs. No dependency override: the real wired service is constructed from
    # default settings, so it returns None -> 404 (no outbound call when off).
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artist_image_cache_dir", str(tmp_path))
    with TestClient(app) as client:
        resp = client.get("/api/artists/image", params={"name": "ABBA"})
        assert resp.status_code == 404


@respx.mock
def test_integration_real_service_resolves_through_deezer(tmp_path: Path) -> None:
    # End-to-end through the real wired stack (source -> cache -> service ->
    # endpoint), with Deezer mocked by respx. Proves the wiring resolves.
    respx.get("https://api.deezer.com/search/artist").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": "ABBA",
                        "nb_fan": 100,
                        "nb_album": 8,
                        "picture_xl": "https://img/abba.jpg",
                        "picture_big": "",
                    }
                ]
            },
        )
    )
    respx.get("https://img/abba.jpg").mock(
        return_value=httpx.Response(200, content=b"REALIMG", headers={"content-type": "image/jpeg"})
    )

    client = httpx.AsyncClient()
    service = ArtistImageService(
        source=DeezerArtistImageSource(client=client, search_limit=5),
        cache=ArtistImageCache(tmp_path),
        limiter=TokenBucketLimiter(rate_per_sec=1000.0, max_concurrency=2),
        is_enabled=lambda: True,
        negative_ttl_seconds=3600,
        transient_ttl_seconds=600,
    )

    app.dependency_overrides[get_artist_image_service] = lambda: service
    try:
        with TestClient(app) as test_client:
            resp = test_client.get("/api/artists/image", params={"name": "ABBA"})
        assert resp.status_code == 200
        assert resp.content == b"REALIMG"
        assert resp.headers["content-type"] == "image/jpeg"
    finally:
        app.dependency_overrides.clear()


def test_service_offloads_blocking_work_to_threadpool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # get_artist_image is async so the network resolve stays on the loop, but its
    # SYNCHRONOUS blocking parts — the cache disk read (up to 10 MB) and the beets
    # mbid query — must be offloaded. Spy on the service-module run_in_threadpool
    # (real passthrough) and assert both blocking calls went through it.
    import asyncio

    import app.artwork.service as service_mod
    from app.artwork.service import ArtistImageService

    real = _real_run_in_threadpool
    spy = Mock(side_effect=lambda fn, *a, **k: real(fn, *a, **k))
    monkeypatch.setattr(service_mod, "run_in_threadpool", spy, raising=False)

    def get_mbid() -> str | None:
        return None

    class _StubSource:
        async def resolve(self, name: str, *, mbid: str | None) -> None:
            return None  # confirmed no-match → store negative → endpoint 404s

    cache = ArtistImageCache(tmp_path)  # empty dir → cache miss → mbid + resolve run
    service = ArtistImageService(
        source=_StubSource(),  # type: ignore[arg-type]  # stub implements only resolve()
        cache=cache,
        limiter=TokenBucketLimiter(rate_per_sec=1000.0, max_concurrency=2),
        is_enabled=lambda: True,
        negative_ttl_seconds=3600,
        transient_ttl_seconds=600,
    )

    result = asyncio.run(service.get_artist_image("ABBA", get_mbid=get_mbid))
    assert result is None

    offloaded = [call.args[0] for call in spy.call_args_list]
    assert cache.get in offloaded  # the cache disk read
    assert get_mbid in offloaded  # the beets mbid query


def test_endpoint_passes_artist_mbid_to_service() -> None:
    # A stub service that records the mbid the endpoint resolves + threads.
    seen: dict[str, str | None] = {}

    class _RecordingService:
        async def get_artist_image(
            self, name: str, *, get_mbid: Callable[[], str | None] | None = None
        ) -> tuple[bytes, str] | None:
            seen["mbid"] = get_mbid() if get_mbid is not None else None
            return (b"JPEGBYTES", "image/jpeg")

    class _Handle:  # only `.lib` is read; the lookup is stubbed below
        lib = object()

    from app.api.albums import get_library
    from app.beets import library as library_mod

    def fake_get_artist_mbid(lib: object, name: str) -> str | None:
        return "the-mbid" if name == "ABBA" else None

    app.dependency_overrides[get_artist_image_service] = lambda: _RecordingService()
    app.dependency_overrides[get_library] = lambda: _Handle()
    mp = pytest.MonkeyPatch()
    mp.setattr(library_mod, "get_artist_mbid", fake_get_artist_mbid)
    try:
        resp = TestClient(app).get("/api/artists/image", params={"name": "ABBA"})
        assert resp.status_code == 200
        assert seen["mbid"] == "the-mbid"
    finally:
        mp.undo()
        app.dependency_overrides.clear()
