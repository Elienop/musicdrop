import io
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import pytest
import respx
from fastapi.concurrency import run_in_threadpool as _real_run_in_threadpool
from fastapi.testclient import TestClient
from PIL import Image

from app.api.albums import get_library
from app.api.artists import get_artist_image_cache, get_artist_image_service
from app.artwork.cache import ArtistImageCache
from app.artwork.deezer import DeezerArtistImageSource
from app.artwork.rate_limit import TokenBucketLimiter
from app.artwork.service import ArtistImageService
from app.config import settings as app_settings
from app.main import app


class _StubService:
    """Stub standing in for ArtistImageService (no real Deezer/HTTP)."""

    def __init__(self, result: tuple[bytes, str] | None, *, enabled: bool = True) -> None:
        self._result = result
        self._enabled = enabled
        self.calls: list[str] = []

    def is_enabled(self) -> bool:
        return self._enabled

    async def get_artist_image(
        self, name: str, *, get_mbid: object = None
    ) -> tuple[bytes, str] | None:
        self.calls.append(name)
        return self._result


class _StubHandle:
    """Minimal LibraryHandle stand-in; only ``.lib`` is read by the endpoint."""

    lib = object()


class _EmptyCache:
    """Stub cache with nothing to stat. The byte-stub ``_StubService`` fixtures
    below don't route through a real ``ArtistImageCache``, so this keeps their
    validator lookup a clean miss — exercising the content-hash fallback path,
    same as before the stat-based validator existed."""

    def validator(self, name: str) -> str | None:
        return None

    def has_fresh_negative(self, name: str) -> bool:
        return False


def _png(width: int, height: int, color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _client_with(service: object) -> Iterator[TestClient]:
    app.dependency_overrides[get_artist_image_service] = lambda: service
    app.dependency_overrides[get_library] = lambda: _StubHandle()
    app.dependency_overrides[get_artist_image_cache] = lambda: _EmptyCache()
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def hit_client() -> Iterator[TestClient]:
    yield from _client_with(_StubService((b"JPEGBYTES", "image/jpeg")))


@pytest.fixture
def miss_client() -> Iterator[TestClient]:
    yield from _client_with(_StubService(None))


@pytest.fixture
def artist_image_cache(tmp_path: Path) -> ArtistImageCache:
    return ArtistImageCache(tmp_path)


@pytest.fixture
def client(artist_image_cache: ArtistImageCache) -> Iterator[TestClient]:
    """A client wired to a REAL ArtistImageService + cache (not the byte-stub
    fixtures above), so the stat-based validator has an actual file to stat."""

    class _NoNetworkSource:
        """These tests pre-populate the cache directly; a real resolve() call
        would mean the endpoint skipped the cache — fail loudly instead."""

        async def resolve(self, name: str, *, mbid: str | None) -> None:
            raise AssertionError("resolve() should not run; the cache is pre-populated")

    service = ArtistImageService(
        source=_NoNetworkSource(),  # type: ignore[arg-type]  # stub implements only resolve()
        cache=artist_image_cache,
        limiter=TokenBucketLimiter(rate_per_sec=1000.0, max_concurrency=2),
        is_enabled=lambda: True,
        negative_ttl_seconds=3600,
        transient_ttl_seconds=600,
    )
    app.dependency_overrides[get_artist_image_service] = lambda: service
    app.dependency_overrides[get_artist_image_cache] = lambda: artist_image_cache
    app.dependency_overrides[get_library] = lambda: _StubHandle()
    yield TestClient(app)
    app.dependency_overrides.clear()


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


def test_conditional_get_304_without_reading_image(
    client: TestClient, artist_image_cache: ArtistImageCache
) -> None:
    artist_image_cache.store_positive("ABBA", b"image-bytes", "image/png")
    first = client.get("/api/artists/image", params={"name": "ABBA"})
    assert first.status_code == 200
    etag = first.headers["etag"]
    # Prove the 304 path never opens the image: make read_bytes explode.
    with patch.object(
        Path, "read_bytes", side_effect=AssertionError("304 path must not read the image")
    ):
        second = client.get(
            "/api/artists/image", params={"name": "ABBA"}, headers={"If-None-Match": etag}
        )
    assert second.status_code == 304
    assert second.headers["etag"] == etag
    assert second.content == b""


def test_etag_changes_when_image_changes(
    client: TestClient, artist_image_cache: ArtistImageCache
) -> None:
    artist_image_cache.store_positive("ABBA", b"old", "image/png")
    etag = client.get("/api/artists/image", params={"name": "ABBA"}).headers["etag"]
    artist_image_cache.store_positive("ABBA", b"new-and-longer", "image/png")
    resp = client.get(
        "/api/artists/image", params={"name": "ABBA"}, headers={"If-None-Match": etag}
    )
    assert resp.status_code == 200
    assert resp.headers["etag"] != etag


def test_hash_fallback_runs_off_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    # When there is nothing to stat (e.g. an override raced away underneath us),
    # the endpoint falls back to revalidating_image_response's content hash —
    # that hash must be offloaded, not run on the event loop.
    import app.api.artists as artists_mod

    class _NoValidatorCache:
        def validator(self, name: str) -> str | None:
            return None

        def has_fresh_negative(self, name: str) -> bool:
            return False

    real = _real_run_in_threadpool
    spy = Mock(side_effect=lambda fn, *a, **k: real(fn, *a, **k))
    monkeypatch.setattr(artists_mod, "run_in_threadpool", spy)

    app.dependency_overrides[get_artist_image_service] = lambda: _StubService(
        (b"JPEGBYTES", "image/jpeg")
    )
    app.dependency_overrides[get_library] = lambda: _StubHandle()
    app.dependency_overrides[get_artist_image_cache] = lambda: _NoValidatorCache()
    try:
        resp = TestClient(app).get("/api/artists/image", params={"name": "ABBA"})
        assert resp.status_code == 200
        assert resp.headers["etag"]
    finally:
        app.dependency_overrides.clear()

    from app.api.http_cache import revalidating_image_response

    offloaded = [call.args[0] for call in spy.call_args_list]
    assert revalidating_image_response in offloaded


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
    _pin_settings_at(tmp_path, monkeypatch)
    with TestClient(app) as client:
        resp = client.get("/api/artists/image", params={"name": "ABBA"})
        assert resp.status_code == 404


@respx.mock
def test_integration_real_service_resolves_through_deezer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    _pin_settings_at(tmp_path, monkeypatch)
    cache = ArtistImageCache(tmp_path)
    service = ArtistImageService(
        source=DeezerArtistImageSource(client=client, search_limit=5),
        cache=cache,
        limiter=TokenBucketLimiter(rate_per_sec=1000.0, max_concurrency=2),
        is_enabled=lambda: True,
        negative_ttl_seconds=3600,
        transient_ttl_seconds=600,
    )

    app.dependency_overrides[get_artist_image_service] = lambda: service
    # The endpoint reads the cache DIRECTLY now (a slot that exists is served
    # from disk, never through the service), so leaving this dependency on the
    # lifespan-built cache would let a developer's real cached ABBA answer this
    # request and the respx mock would never be reached.
    app.dependency_overrides[get_artist_image_cache] = lambda: cache
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


def test_every_exit_of_the_image_get_carries_nosniff(
    client: TestClient, artist_image_cache: ArtistImageCache
) -> None:
    """No content-type this endpoint serves is authored by this app.

    They come off a CDN response header, a ``.mime`` sidecar written from one,
    or a media file's embedded picture MIME - and ``image/svg+xml`` is a legal
    answer from all three. An SVG rendered from this app's own origin is script
    execution, not a picture, and this is the EASIER of the two artist-image
    routes to aim at because it is a GET.

    Six exits reach this assertion (stat-validated 200 and 304, thumb 200 and
    304, and the two content-hash fallbacks reachable through ``hit_client``
    below), which is exactly why the header lives on the two response
    constructors rather than at each ``return``.
    """
    artist_image_cache.store_positive("ABBA", _png(1000, 1000), "image/png")
    full = client.get("/api/artists/image", params={"name": "ABBA"})
    thumb = client.get("/api/artists/image", params={"name": "ABBA", "size": "thumb"})
    revalidated = client.get(
        "/api/artists/image",
        params={"name": "ABBA"},
        headers={"If-None-Match": full.headers["etag"]},
    )
    thumb_revalidated = client.get(
        "/api/artists/image",
        params={"name": "ABBA", "size": "thumb"},
        headers={"If-None-Match": thumb.headers["etag"]},
    )
    # Non-vacuity: assert each arm is the response it claims to be, or a 404
    # would satisfy "no sniffable body" for the wrong reason.
    assert [r.status_code for r in (full, thumb, revalidated, thumb_revalidated)] == [
        200,
        200,
        304,
        304,
    ]
    for resp in (full, thumb, revalidated, thumb_revalidated):
        assert resp.headers["x-content-type-options"] == "nosniff"


def test_the_image_get_declares_the_body_its_404_actually_returns() -> None:
    # Same shape the fetch route was fixed for: a description-only entry in
    # `responses={...}` REPLACES the generated response, so the status carries
    # no schema and openapi-typescript renders `content?: never` - a type saying
    # the body cannot exist - for a 404 that returns {"detail": "..."}.
    responses = app.openapi()["paths"]["/api/artists/image"]["get"]["responses"]
    assert (
        responses["404"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ErrorDetail"
    )
    # And 422 must keep the OTHER shape (detail is a list): declaring it at all
    # is what would strip HTTPValidationError.
    assert (
        responses["422"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/HTTPValidationError"
    )


def test_the_content_hash_fallback_exits_carry_nosniff(hit_client: TestClient) -> None:
    # The other two exits: no file to stat, so the endpoint falls back to the
    # content-hash ETag through revalidating_image_response.
    full = hit_client.get("/api/artists/image", params={"name": "ABBA"})
    revalidated = hit_client.get(
        "/api/artists/image",
        params={"name": "ABBA"},
        headers={"If-None-Match": full.headers["etag"]},
    )
    assert (full.status_code, revalidated.status_code) == (200, 304)
    assert full.headers["x-content-type-options"] == "nosniff"
    assert revalidated.headers["x-content-type-options"] == "nosniff"


def test_size_thumb_serves_webp_with_its_own_etag(
    client: TestClient, artist_image_cache: ArtistImageCache
) -> None:
    artist_image_cache.store_positive("ABBA", _png(1000, 1000), "image/png")
    full = client.get("/api/artists/image", params={"name": "ABBA"})
    thumb = client.get("/api/artists/image", params={"name": "ABBA", "size": "thumb"})
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/webp"
    assert len(thumb.content) < len(full.content)
    assert thumb.headers["etag"] != full.headers["etag"]
    # Conditional GET on the thumb tag 304s.
    again = client.get(
        "/api/artists/image",
        params={"name": "ABBA", "size": "thumb"},
        headers={"If-None-Match": thumb.headers["etag"]},
    )
    assert again.status_code == 304


def test_size_thumb_resolves_and_derives_when_uncached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing cached yet: size=thumb must resolve (network) once, then derive
    # and serve the thumb — not 404 just because get_thumb() started as None.
    from app.artwork.rate_limit import TokenBucketLimiter
    from app.artwork.service import ArtistImageService
    from app.artwork.source import ResolvedImage
    from app.beets import library as library_mod

    class _StubSource:
        async def resolve(self, name: str, *, mbid: str | None) -> ResolvedImage:
            return ResolvedImage(data=_png(1000, 1000), content_type="image/png")

    cache = ArtistImageCache(tmp_path)
    service = ArtistImageService(
        source=_StubSource(),  # type: ignore[arg-type]  # stub implements only resolve()
        cache=cache,
        limiter=TokenBucketLimiter(rate_per_sec=1000.0, max_concurrency=2),
        is_enabled=lambda: True,
        negative_ttl_seconds=3600,
        transient_ttl_seconds=600,
    )
    monkeypatch.setattr(library_mod, "get_artist_mbid", lambda lib, name: None)
    app.dependency_overrides[get_artist_image_service] = lambda: service
    app.dependency_overrides[get_artist_image_cache] = lambda: cache
    app.dependency_overrides[get_library] = lambda: _StubHandle()
    try:
        resp = TestClient(app).get("/api/artists/image", params={"name": "ABBA", "size": "thumb"})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/webp"
        assert Image.open(io.BytesIO(resp.content)).size == (320, 320)
        assert resp.headers["etag"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "poison",
    [b"\xff\xfe", "image/日本語".encode(), b"image/png\nX-Injected: yes", b""],
    ids=["invalid-utf8", "non-ascii", "response-splitting", "empty"],
)
def test_corrupt_mime_sidecar_serves_the_image_instead_of_500ing(
    client: TestClient, artist_image_cache: ArtistImageCache, tmp_path: Path, poison: bytes
) -> None:
    """Four ways a ``.mime`` sidecar breaks the response, all of which must end
    as a served image.

    The hole was on the self-heal path the never-500 promise exists for:
    ``get_thumb`` reaches its source through ``get()``, so the sidecar next door
    could abort a ``.thumb.src`` rebuild too. The full image keeps its bytes and
    falls back to the generic content-type; the thumb is derived from those same
    bytes, so it declares WebP as usual.

    ASSERT ON THE CONTENT-TYPE, NOT JUST THE STATUS — the two poison shapes are
    not equally visible here. `image/日本語` is encoded latin-1 inside
    `Response.init_headers`, i.e. within the app, so TestClient DOES surface it.
    `image/png\\nX-Injected: yes` is passed through untouched and only dies at
    h11, so it returns a green 200 through TestClient and fails only on a real
    server. A status assertion therefore covers one shape and nothing at all for
    the other; both were verified separately against a real uvicorn.
    """
    artist_image_cache.store_positive("ABBA", _png(400, 400), "image/png")
    (mime_path,) = tmp_path.glob("*.mime")
    mime_path.write_bytes(poison)

    full = client.get("/api/artists/image", params={"name": "ABBA"})
    assert full.status_code == 200
    assert full.headers["content-type"] == "application/octet-stream"

    thumb = client.get("/api/artists/image", params={"name": "ABBA", "size": "thumb"})
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/webp"


def test_unreadable_cache_bytes_self_heal_instead_of_500ing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable positive slot has to read as "nothing cached", not raise:
    the endpoint then re-resolves and the entry rebuilds, instead of an OSError
    from the ``exists()``-then-``read_bytes()`` window reaching the client.
    """
    from app.artwork.cache import CachedImage
    from app.artwork.source import ResolvedImage
    from app.beets import library as library_mod

    class _StubSource:
        async def resolve(self, name: str, *, mbid: str | None) -> ResolvedImage:
            return ResolvedImage(data=_png(120, 120), content_type="image/png")

    cache = ArtistImageCache(tmp_path)
    cache.store_positive("ABBA", b"unreadable", "image/png")
    service = ArtistImageService(
        source=_StubSource(),  # type: ignore[arg-type]  # stub implements only resolve()
        cache=cache,
        limiter=TokenBucketLimiter(rate_per_sec=1000.0, max_concurrency=2),
        is_enabled=lambda: True,
        negative_ttl_seconds=3600,
        transient_ttl_seconds=600,
    )
    monkeypatch.setattr(library_mod, "get_artist_mbid", lambda lib, name: None)
    app.dependency_overrides[get_artist_image_service] = lambda: service
    app.dependency_overrides[get_artist_image_cache] = lambda: cache
    app.dependency_overrides[get_library] = lambda: _StubHandle()

    real_read_bytes = Path.read_bytes

    def failing_read_bytes(self: Path) -> bytes:
        # Only the cached image slot is unreadable — everything else on the
        # request path must keep working, or the test proves nothing.
        if self.suffix == ".bin":
            raise OSError(5, "simulated I/O error")
        return real_read_bytes(self)

    try:
        with patch.object(Path, "read_bytes", failing_read_bytes):
            resp = TestClient(app).get("/api/artists/image", params={"name": "ABBA"})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        healed = cache.get("ABBA")
        assert isinstance(healed, CachedImage)
        assert healed.data != b"unreadable"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("stored_mime", "served"),
    [("image/webp\n", "image/webp"), ("  image/webp  ", "image/webp")],
    ids=["trailing-newline", "padded"],
)
def test_thumb_endpoint_never_serves_an_unsendable_stored_mime(
    client: TestClient,
    artist_image_cache: ArtistImageCache,
    tmp_path: Path,
    stored_mime: str,
    served: str,
) -> None:
    """What the CALLER serves is the assertion that matters.

    ``.thumb.src`` is the one stored content-type nothing strips on read, and
    both shapes below are a dropped connection on a real server — the newline
    under both uvicorn workers, the padding under h11 (h11 fullmatches
    ``([^\\x00\\s]+(?:[ \\t]+[^\\x00\\s]+)*)?``). Neither is visible as a status
    change through TestClient, which is exactly why this asserts the header
    VALUE. The two are answered differently on purpose: the newline re-derives,
    the padding hits and is trimmed.
    """
    artist_image_cache.store_positive("ABBA", _png(400, 400), "image/png")
    warmed = client.get("/api/artists/image", params={"name": "ABBA", "size": "thumb"})
    assert warmed.status_code == 200
    (src_path,) = tmp_path.glob("*.thumb.src")
    stored_tag, _, _ = src_path.read_text(encoding="utf-8").partition(" ")
    src_path.write_text(f"{stored_tag} {stored_mime}", encoding="utf-8")

    resp = client.get("/api/artists/image", params={"name": "ABBA", "size": "thumb"})

    assert resp.status_code == 200
    assert resp.headers["content-type"] == served


def test_size_thumb_still_serves_a_thumb_when_the_cache_cannot_store_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Losing the cache must cost cache HITS, not the feature.

    With an unwritable cache dir there is nothing to stat, so ``get_thumb``
    bails before it can help — and it genuinely cannot help, because the source
    bytes exist only in the value the endpoint just resolved. Left alone, a
    ``?size=thumb`` request quietly served the 1200px original: the exact
    symptom the degrade log exists to name, reached without ever touching that
    log line, and a loud 500 before the cache-dir guards landed.
    """
    if os.getuid() == 0:
        pytest.skip("running as root: a read-only dir does not deny writes")
    from app.artwork.source import ResolvedImage
    from app.beets import library as library_mod

    class _StubSource:
        async def resolve(self, name: str, *, mbid: str | None) -> ResolvedImage:
            return ResolvedImage(data=_png(1200, 1200), content_type="image/png")

    cache = ArtistImageCache(tmp_path)
    service = ArtistImageService(
        source=_StubSource(),  # type: ignore[arg-type]  # stub implements only resolve()
        cache=cache,
        limiter=TokenBucketLimiter(rate_per_sec=1000.0, max_concurrency=2),
        is_enabled=lambda: True,
        negative_ttl_seconds=3600,
        transient_ttl_seconds=600,
    )
    monkeypatch.setattr(library_mod, "get_artist_mbid", lambda lib, name: None)
    app.dependency_overrides[get_artist_image_service] = lambda: service
    app.dependency_overrides[get_artist_image_cache] = lambda: cache
    app.dependency_overrides[get_library] = lambda: _StubHandle()
    os.chmod(tmp_path, 0o500)
    try:
        resp = TestClient(app).get("/api/artists/image", params={"name": "ABBA", "size": "thumb"})
    finally:
        os.chmod(tmp_path, 0o755)
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/webp"
    assert Image.open(io.BytesIO(resp.content)).size == (320, 320)


def test_size_defaults_to_full(hit_client: TestClient) -> None:
    resp = hit_client.get("/api/artists/image", params={"name": "ABBA"})
    assert resp.status_code == 200
    assert resp.content == b"JPEGBYTES"


def test_invalid_size_is_422(hit_client: TestClient) -> None:
    resp = hit_client.get("/api/artists/image", params={"name": "ABBA", "size": "huge"})
    assert resp.status_code == 422


def test_endpoint_passes_artist_mbid_to_service() -> None:
    # A stub service that records the mbid the endpoint resolves + threads.
    seen: dict[str, str | None] = {}

    class _RecordingService:
        def is_enabled(self) -> bool:
            return True

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
    app.dependency_overrides[get_artist_image_cache] = lambda: _EmptyCache()
    mp = pytest.MonkeyPatch()
    mp.setattr(library_mod, "get_artist_mbid", fake_get_artist_mbid)
    try:
        resp = TestClient(app).get("/api/artists/image", params={"name": "ABBA"})
        assert resp.status_code == 200
        assert seen["mbid"] == "the-mbid"
    finally:
        mp.undo()
        app.dependency_overrides.clear()


def _pin_settings_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the lifespan at a throwaway beets dir and artist-image cache dir.

    ``backend/.env`` aims MUSICDROP_BEETS_DIR at the REAL dev library and the
    lifespan opens it for real, so a ``with TestClient(app)`` test that skips
    this pin runs against the developer's music library. Pinning the cache dir
    keeps a persisted enabled toggle out too.
    """
    music = tmp_path / "music"
    music.mkdir()
    (tmp_path / "config.yaml").write_text(f"directory: {music}\nlibrary: library.db\nplugins: []\n")
    monkeypatch.setattr(app_settings, "beets_dir", str(tmp_path))
    monkeypatch.setattr(app_settings, "artist_image_cache_dir", str(tmp_path / "cache"))


def test_a_slow_uncached_resolve_404s_instead_of_holding_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The wave's whole point: a cold page must not spend ~10s resolving inside
    # its HTTP requests. The fill continues; the portrait arrives via SSE.
    #
    # `with TestClient(app)` is load-bearing: outside the context manager each
    # request gets its OWN event loop, so the background task would be orphaned
    # on a loop that is immediately torn down.
    import asyncio
    import time

    class _SlowService:
        def __init__(self) -> None:
            self.calls = 0

        def is_enabled(self) -> bool:
            return True

        async def get_artist_image(
            self, name: str, *, get_mbid: object = None
        ) -> tuple[bytes, str] | None:
            self.calls += 1
            await asyncio.sleep(0.30)
            return (b"LATE", "image/jpeg")

    service = _SlowService()
    _pin_settings_at(tmp_path, monkeypatch)
    monkeypatch.setattr(app_settings, "artist_image_inline_grace_seconds", 0.02)
    app.dependency_overrides[get_artist_image_service] = lambda: service
    app.dependency_overrides[get_artist_image_cache] = lambda: _EmptyCache()
    app.dependency_overrides[get_library] = lambda: _StubHandle()
    try:
        with TestClient(app) as client:
            started = time.monotonic()
            resp = client.get("/api/artists/image", params={"name": "Slowpoke"})
            elapsed = time.monotonic() - started
            assert resp.status_code == 404
            assert elapsed < 0.25  # returned on the grace, not on the resolve
            assert service.calls == 1  # ...and the resolve really was started
    finally:
        app.dependency_overrides.clear()


def test_a_quick_uncached_resolve_is_still_served_inline(hit_client: TestClient) -> None:
    # A single artist page must not regress into a monogram flash: a source
    # that answers inside the grace window is served in the SAME request.
    resp = hit_client.get("/api/artists/image", params={"name": "ABBA"})
    assert resp.status_code == 200
    assert resp.content == b"JPEGBYTES"


def test_a_fresh_negative_marker_starts_no_resolve(tmp_path: Path) -> None:
    # A confirmed no-match inside its TTL must not be re-fetched, in the
    # foreground OR the background.
    cache = ArtistImageCache(tmp_path)
    cache.store_negative("Nobody", ttl_seconds=3600)
    service = _StubService((b"SHOULD-NOT-BE-USED", "image/jpeg"))
    app.dependency_overrides[get_artist_image_service] = lambda: service
    app.dependency_overrides[get_artist_image_cache] = lambda: cache
    app.dependency_overrides[get_library] = lambda: _StubHandle()
    try:
        resp = TestClient(app).get("/api/artists/image", params={"name": "Nobody"})
        assert resp.status_code == 404
        assert service.calls == []
    finally:
        app.dependency_overrides.clear()


def test_a_cached_image_is_served_without_touching_the_service(tmp_path: Path) -> None:
    cache = ArtistImageCache(tmp_path)
    cache.store_positive("ABBA", b"CACHED", "image/png")
    service = _StubService(None)
    app.dependency_overrides[get_artist_image_service] = lambda: service
    app.dependency_overrides[get_artist_image_cache] = lambda: cache
    app.dependency_overrides[get_library] = lambda: _StubHandle()
    try:
        resp = TestClient(app).get("/api/artists/image", params={"name": "ABBA"})
        assert resp.status_code == 200
        assert resp.content == b"CACHED"
        assert service.calls == []  # no resolve path on a hit
    finally:
        app.dependency_overrides.clear()


def test_a_disabled_feature_starts_no_background_fill() -> None:
    # The toggle has to be checked HERE, not only inside the service: the
    # endpoint now hands misses to a background task, and an off feature must
    # not leave one running (nor answer 200 from a slot it may no longer own).
    service = _StubService((b"JPEGBYTES", "image/jpeg"), enabled=False)
    app.dependency_overrides[get_artist_image_service] = lambda: service
    app.dependency_overrides[get_artist_image_cache] = lambda: _EmptyCache()
    app.dependency_overrides[get_library] = lambda: _StubHandle()
    try:
        resp = TestClient(app).get("/api/artists/image", params={"name": "ABBA"})
        assert resp.status_code == 404
        assert service.calls == []
    finally:
        app.dependency_overrides.clear()


def test_two_concurrent_cold_requests_resolve_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No single-flight existed anywhere before this wave; a background filler
    # without one would multiply the traffic it is meant to reduce.
    #
    # `with TestClient(app)` is what makes this test MEAN anything: outside the
    # context manager every request runs on its own event loop, so the four
    # requests could not share an in-flight entry even if the code were correct
    # (starlette/testclient.py:414-418). Inside it they share one portal loop,
    # and the threads below are real concurrency against it.
    import asyncio
    import threading

    class _CountingService:
        def __init__(self) -> None:
            self.calls = 0
            self.lock = threading.Lock()

        def is_enabled(self) -> bool:
            return True

        async def get_artist_image(
            self, name: str, *, get_mbid: object = None
        ) -> tuple[bytes, str] | None:
            with self.lock:
                self.calls += 1
            await asyncio.sleep(0.30)
            return (b"IMG", "image/jpeg")

    service = _CountingService()
    _pin_settings_at(tmp_path, monkeypatch)
    app.dependency_overrides[get_artist_image_service] = lambda: service
    app.dependency_overrides[get_artist_image_cache] = lambda: _EmptyCache()
    app.dependency_overrides[get_library] = lambda: _StubHandle()
    statuses: list[int] = []
    status_lock = threading.Lock()
    try:
        with TestClient(app) as client:

            def one_request() -> None:
                resp = client.get("/api/artists/image", params={"name": "ABBA"})
                with status_lock:
                    statuses.append(resp.status_code)

            threads = [threading.Thread(target=one_request) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert service.calls == 1
            # Non-vacuity: one call could also mean three requests failed early.
            assert statuses == [200, 200, 200, 200]
    finally:
        app.dependency_overrides.clear()
