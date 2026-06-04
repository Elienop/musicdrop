from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

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

    async def get_artist_image(self, name: str) -> tuple[bytes, str] | None:
        self.calls.append(name)
        return self._result


def _client_with(service: object) -> Iterator[TestClient]:
    app.dependency_overrides[get_artist_image_service] = lambda: service
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


def test_hit_sets_cache_control_header(hit_client: TestClient) -> None:
    resp = hit_client.get("/api/artists/image", params={"name": "ABBA"})
    assert "public" in resp.headers["cache-control"]
    assert "max-age=" in resp.headers["cache-control"]


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


def test_default_settings_disabled_endpoint_404s() -> None:
    # No dependency override: the real wired service is constructed from default
    # settings (artist_images_enabled=False), so it returns None -> 404.
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
