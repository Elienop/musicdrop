from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.concurrency import run_in_threadpool as _real_run_in_threadpool
from fastapi.testclient import TestClient

import app.api.artists as artists_mod
from app.api.artists import (
    get_artist_image_cache,
    get_artist_image_http_client,
    get_artist_image_service,
)
from app.artwork.cache import NEGATIVE, ArtistImageCache, CachedImage
from app.artwork.service import ArtistImageService
from app.main import app

PNG = Path(__file__).parent / "fixtures" / "cover.png"


class _NeverSource:
    """A source that must never be called (the override short-circuits resolve)."""

    async def resolve(
        self, name: str, *, mbid: str | None = None
    ) -> None:  # pragma: no cover - guard
        raise AssertionError("resolve() should not be called when an override exists")


@pytest.fixture
def cache(tmp_path: Path) -> ArtistImageCache:
    return ArtistImageCache(tmp_path)


@pytest.fixture
def client(cache: ArtistImageCache) -> Iterator[TestClient]:
    app.dependency_overrides[get_artist_image_cache] = lambda: cache
    # The from-url tests monkeypatch fetch_image_bytes, so this client is unused;
    # override the dep so it doesn't reach into app.state (lifespan doesn't run).
    app.dependency_overrides[get_artist_image_http_client] = lambda: object()
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


def test_upload_offloads_the_cache_write_to_the_threadpool(
    client: TestClient, cache: ArtistImageCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    # write_override is a blocking mkdir + up-to-10MB disk write (cache dir may be
    # on slow HDD/NAS) — it must not run on the event loop.
    spy = Mock(side_effect=lambda fn, *a, **k: _real_run_in_threadpool(fn, *a, **k))
    monkeypatch.setattr(artists_mod, "run_in_threadpool", spy, raising=False)
    resp = client.post(
        "/api/artists/image/override",
        params={"name": "ABBA"},
        files={"file": ("p.png", PNG.read_bytes(), "image/png")},
    )
    assert resp.status_code == 200
    assert cache.write_override in [call.args[0] for call in spy.call_args_list]


def test_cross_origin_upload_rejected(client: TestClient) -> None:
    # A cross-origin browser POST (multipart is preflight-exempt) must not be able
    # to overwrite an artist image via CSRF — a foreign Origin is rejected 403.
    resp = client.post(
        "/api/artists/image/override",
        params={"name": "ABBA"},
        files={"file": ("p.png", PNG.read_bytes(), "image/png")},
        headers={"Origin": "http://evil.test"},
    )
    assert resp.status_code == 403


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


def test_reset_clears_both_slots_and_reports_what_it_did(
    client: TestClient, cache: ArtistImageCache
) -> None:
    cache.store_positive("ABBA", b"auto", "image/png")
    cache.write_override("ABBA", b"manual", "image/png")
    resp = client.post("/api/artists/image/reset", params={"name": "ABBA"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "cleared_override": True, "cleared_auto": True}
    # BOTH gone: the next lookup re-resolves instead of falling back to the
    # automatic image the user just rejected.
    assert cache.get("ABBA") is None


def test_reset_clears_an_auto_only_image(client: TestClient, cache: ArtistImageCache) -> None:
    # THE bug this route exists for: with no override in play, the DELETE it
    # replaced answered 204 while changing nothing, so the next paint served the
    # very image the user pressed Reset to get rid of.
    cache.store_positive("ABBA", b"auto", "image/png")
    resp = client.post("/api/artists/image/reset", params={"name": "ABBA"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "cleared_override": False, "cleared_auto": True}
    assert cache.get("ABBA") is None


def test_reset_reports_honestly_when_there_was_nothing_to_clear(client: TestClient) -> None:
    resp = client.post("/api/artists/image/reset", params={"name": "Nobody"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "cleared_override": False, "cleared_auto": False}


def test_reset_distinguishes_an_override_only_state(
    client: TestClient, cache: ArtistImageCache
) -> None:
    cache.write_override("ABBA", b"manual", "image/png")
    body = client.post("/api/artists/image/reset", params={"name": "ABBA"}).json()
    assert body == {"ok": True, "cleared_override": True, "cleared_auto": False}


def test_reset_drops_a_fresh_negative_marker(client: TestClient, cache: ArtistImageCache) -> None:
    cache.store_negative("Nobody", ttl_seconds=3600)
    assert cache.get("Nobody") is NEGATIVE
    client.post("/api/artists/image/reset", params={"name": "Nobody"})
    assert cache.get("Nobody") is None


def test_reset_offloads_the_cache_work_to_the_threadpool(
    client: TestClient, cache: ArtistImageCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache.write_override("ABBA", b"manual", "image/png")
    spy = Mock(side_effect=lambda fn, *a, **k: _real_run_in_threadpool(fn, *a, **k))
    monkeypatch.setattr(artists_mod, "run_in_threadpool", spy, raising=False)
    resp = client.post("/api/artists/image/reset", params={"name": "ABBA"})
    assert resp.status_code == 200
    assert artists_mod._reset_slots in [call.args[0] for call in spy.call_args_list]


def test_cross_origin_reset_is_rejected(client: TestClient) -> None:
    # A body-less POST is a CORS-simple request (no preflight), unlike the
    # DELETE this replaced - so the Origin guard is load-bearing here.
    resp = client.post(
        "/api/artists/image/reset",
        params={"name": "ABBA"},
        headers={"Origin": "http://evil.test"},
    )
    assert resp.status_code == 403


def test_the_reset_declares_the_403_its_own_guard_returns() -> None:
    """A status the route really returns must be in the spec with its body.

    The test above proves the 403 is real; this proves the generated client is
    told about it. An `Origin` guard is a `dependencies=[...]` entry, which
    emits no security scheme, so nothing but this declaration puts the status in
    the schema - and `openapi-typescript` would otherwise type the branch as
    impossible while the route takes it. 422 stays undeclared: declaring it
    would replace `HTTPValidationError`, whose `detail` is a list, not a
    sentence.
    """
    from app.main import app

    responses = app.openapi()["paths"]["/api/artists/image/reset"]["post"]["responses"]
    assert sorted(responses) == ["200", "403", "422"]
    forbidden = responses["403"]["content"]
    assert set(forbidden) == {"application/json"}
    assert forbidden["application/json"]["schema"]["$ref"] == "#/components/schemas/ErrorDetail"
    assert responses["422"]["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/HTTPValidationError"
    )


def test_reset_requires_a_name(client: TestClient) -> None:
    assert client.post("/api/artists/image/reset").status_code == 422


def test_the_override_delete_route_is_gone(client: TestClient) -> None:
    assert client.delete("/api/artists/image/override", params={"name": "ABBA"}).status_code == 405


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


def test_from_url_writes_override(
    client: TestClient, cache: ArtistImageCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.api.artists as artists_api

    async def fake_fetch(http_client: object, url: str) -> bytes:
        return PNG.read_bytes()

    monkeypatch.setattr(artists_api, "fetch_image_bytes", fake_fetch)
    resp = client.post(
        "/api/artists/image/override/from-url",
        params={"name": "ABBA"},
        json={"url": "https://example.test/a.png"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "content_type": "image/png"}
    assert isinstance(cache.get("ABBA"), CachedImage)


def test_from_url_rejects_non_image_bytes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.api.artists as artists_api

    async def fake_fetch(http_client: object, url: str) -> bytes:
        return b"<html>not an image</html>"

    monkeypatch.setattr(artists_api, "fetch_image_bytes", fake_fetch)
    resp = client.post(
        "/api/artists/image/override/from-url",
        params={"name": "ABBA"},
        json={"url": "https://example.test/x"},
    )
    assert resp.status_code == 422
    assert "not a supported image" in resp.json()["detail"].lower()


def test_from_url_fetch_failure_is_422(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.api.artists as artists_api

    async def fake_fetch(http_client: object, url: str) -> bytes:
        raise ValueError("could not fetch image: boom")

    monkeypatch.setattr(artists_api, "fetch_image_bytes", fake_fetch)
    resp = client.post(
        "/api/artists/image/override/from-url",
        params={"name": "ABBA"},
        json={"url": "https://example.test/dead"},
    )
    assert resp.status_code == 422
    assert "could not fetch" in resp.json()["detail"]


def test_from_url_malformed_url_is_422(client: TestClient) -> None:
    # HttpUrl rejects a non-URL before the handler body runs.
    resp = client.post(
        "/api/artists/image/override/from-url",
        params={"name": "ABBA"},
        json={"url": "not-a-url"},
    )
    assert resp.status_code == 422
