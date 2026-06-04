from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.artists import get_artist_image_cache, get_artist_image_service
from app.artwork.cache import ArtistImageCache, CachedImage
from app.artwork.service import ArtistImageService
from app.main import app

PNG = Path(__file__).parent / "fixtures" / "cover.png"


class _NeverSource:
    """A source that must never be called (the override short-circuits resolve)."""

    async def resolve(self, name: str) -> None:  # pragma: no cover - guard
        raise AssertionError("resolve() should not be called when an override exists")


@pytest.fixture
def cache(tmp_path: Path) -> ArtistImageCache:
    return ArtistImageCache(tmp_path)


@pytest.fixture
def client(cache: ArtistImageCache) -> Iterator[TestClient]:
    app.dependency_overrides[get_artist_image_cache] = lambda: cache
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_upload_writes_override(client: TestClient, cache: ArtistImageCache) -> None:
    resp = client.post(
        "/api/artists/image/override",
        params={"name": "ABBA"},
        files={"file": ("p.png", PNG.read_bytes(), "image/png")},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "content_type": "image/png"}
    cached = cache.get("ABBA")
    assert isinstance(cached, CachedImage)
    assert cached.data == PNG.read_bytes()


def test_upload_rejects_non_image(client: TestClient) -> None:
    resp = client.post(
        "/api/artists/image/override",
        params={"name": "ABBA"},
        files={"file": ("x.txt", b"not an image", "text/plain")},
    )
    assert resp.status_code == 422


def test_upload_rejects_oversize_via_content_length(client: TestClient) -> None:
    from app.artwork.images import MAX_IMAGE_BYTES

    oversize = b"\xff\xd8\xff" + b"\x00" * MAX_IMAGE_BYTES
    resp = client.post(
        "/api/artists/image/override",
        params={"name": "ABBA"},
        files={"file": ("big.jpg", oversize, "image/jpeg")},
    )
    assert resp.status_code == 422
    assert "too large" in resp.json()["detail"].lower()


def test_missing_name_is_422(client: TestClient) -> None:
    resp = client.post(
        "/api/artists/image/override",
        files={"file": ("p.png", PNG.read_bytes(), "image/png")},
    )
    assert resp.status_code == 422


def test_delete_clears_override(client: TestClient, cache: ArtistImageCache) -> None:
    cache.write_override("ABBA", b"manual", "image/png")
    resp = client.delete("/api/artists/image/override", params={"name": "ABBA"})
    assert resp.status_code == 204
    assert cache.get("ABBA") is None


def test_uploaded_override_is_served_by_image_get(
    cache: ArtistImageCache, client: TestClient
) -> None:
    # Wire a real (enabled) service over the SAME cache, so the GET image
    # endpoint serves what the override endpoint just wrote.
    from app.artwork.rate_limit import TokenBucketLimiter

    service = ArtistImageService(
        source=_NeverSource(),
        cache=cache,
        limiter=TokenBucketLimiter(rate_per_sec=1000.0, max_concurrency=2),
        is_enabled=lambda: True,
        negative_ttl_seconds=3600,
        transient_ttl_seconds=600,
    )
    app.dependency_overrides[get_artist_image_service] = lambda: service
    try:
        client.post(
            "/api/artists/image/override",
            params={"name": "ABBA"},
            files={"file": ("p.png", PNG.read_bytes(), "image/png")},
        )
        resp = client.get("/api/artists/image", params={"name": "ABBA"})
        assert resp.status_code == 200
        assert resp.content == PNG.read_bytes()
        assert resp.headers["content-type"] == "image/png"
    finally:
        app.dependency_overrides.pop(get_artist_image_service, None)
