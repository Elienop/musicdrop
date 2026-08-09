"""POST /api/artists/image/fetch - ONE named source, preview only.

Three things here are worth more than the happy path:

* **The fetch must take the SERVICE's rate/concurrency slot**, not one of its
  own. ``sources.get()`` hands back a bare source object with no limiter
  attached, so resolving it directly is the easy thing to write and doubles the
  real outbound rate. A peak-concurrency measurement cannot tell one shared
  bucket from two private ones with identical numbers - only CONTENTION between
  this route and the automatic chain can, which is what
  ``test_the_fetch_waits_on_the_automatic_chains_limiter`` builds.
* **A source outage is 502, not 404.** "Deezer failed" and "Deezer has no photo
  of ABBA" are different answers and the UI says different things about them.
* **Nothing client-supplied reaches a response header.** ``label_for`` echoes an
  unknown id back verbatim into ``X-Art-Source``, so the ``Literal`` on
  ``source`` is a security gate. Pinning it needs a hostile id that IS in the
  registry - an unregistered one would 422 as "not configured" even with the
  gate removed, and the test would pass for the wrong reason.
"""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.api.artists import (
    get_artist_image_cache,
    get_artist_image_service,
    get_artist_image_sources,
)
from app.artwork.cache import ArtistImageCache
from app.artwork.factory import DEEZER, ArtistImageSources
from app.artwork.rate_limit import TokenBucketLimiter
from app.artwork.service import ArtistImageService
from app.artwork.source import ResolvedImage, TransientSourceError
from app.beets import library as library_mod
from app.main import app

_URL = "/api/artists/image/fetch"

#: A source id that would be an HTTP response-splitting payload if it ever
#: reached ``X-Art-Source``. It is REGISTERED below so the Literal is the only
#: thing standing between it and the header.
_HOSTILE_ID = "deezer\r\nX-Injected: yes"


class _StubHandle:
    """Minimal LibraryHandle stand-in; ``.lib`` is only handed to the patched
    ``get_artist_mbid``."""

    lib = object()


class _RecordingSource:
    """A source stand-in that records what the endpoint asked it for."""

    def __init__(self, result: ResolvedImage | None = None, boom: bool = False) -> None:
        self.result = result
        self.boom = boom
        self.calls: list[tuple[str, str | None]] = []

    async def resolve(self, name: str, *, mbid: str | None = None) -> ResolvedImage | None:
        self.calls.append((name, mbid))
        if self.boom:
            raise TransientSourceError("upstream said no")
        return self.result


class _StubService:
    """Only what the fetch route reads: the toggle and a rate/concurrency slot.

    Deliberately NOT the place the limiter contract is pinned - a stub can only
    show the route awaits SOMETHING. The shared-bucket guarantee is tested
    against a real :class:`ArtistImageService` further down.
    """

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled
        self._limiter = TokenBucketLimiter(rate_per_sec=1000.0, max_concurrency=2)

    def is_enabled(self) -> bool:
        return self._enabled

    def limiter_slot(self) -> object:
        return self._limiter.slot()


@pytest.fixture(autouse=True)
def _drop_overrides() -> Iterator[None]:
    """Clear dependency overrides AFTER every test, pass or fail.

    Clearing at the end of each test body instead would leak every override
    into the rest of the suite the moment one assertion fails - and the failure
    would surface as unrelated tests breaking.
    """
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def stub_mbid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(library_mod, "get_artist_mbid", lambda lib, name: "the-mbid")


def _client(source: _RecordingSource, *, enabled: bool = True) -> TestClient:
    # The hostile id is a REAL entry: with the Literal removed, `sources.get`
    # would find it and the handler would put it in a response header.
    # _RecordingSource needs no cast - it satisfies the ArtistImageSource
    # Protocol structurally, which is the whole point of the Protocol.
    registry = ArtistImageSources(ordered=((DEEZER, source), (_HOSTILE_ID, source)))
    app.dependency_overrides[get_artist_image_sources] = lambda: registry
    app.dependency_overrides[get_artist_image_service] = lambda: _StubService(enabled)
    app.dependency_overrides[get_library] = lambda: _StubHandle()
    return TestClient(app)


def test_hit_returns_the_bytes_with_provenance_and_no_store() -> None:
    source = _RecordingSource(ResolvedImage(data=b"PORTRAIT", content_type="image/jpeg"))
    resp = _client(source).post(_URL, params={"name": "ABBA", "source": "deezer"})
    assert resp.status_code == 200
    assert resp.content == b"PORTRAIT"
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["x-art-source"] == "Deezer"
    assert resp.headers["cache-control"] == "no-store"


def test_the_fetch_writes_nothing_to_the_cache(tmp_path: Path) -> None:
    """A FORWARD guard: the route takes no cache dependency today.

    So this cannot fail on the current code - it fails the day someone adds a
    cache write, by either route in: the DI override below, or a direct
    ``request.app.state`` read. Both are wired at the same tmp dir. The 200
    assertion is what keeps it from passing on a route that does not exist -
    the un-asserted version of this test passed against an empty router.
    """
    cache = ArtistImageCache(tmp_path)
    source = _RecordingSource(ResolvedImage(data=b"PORTRAIT", content_type="image/jpeg"))
    client = _client(source)
    app.dependency_overrides[get_artist_image_cache] = lambda: cache
    app.state.artist_image_cache = cache
    try:
        resp = client.post(_URL, params={"name": "ABBA", "source": "deezer"})
    finally:
        del app.state.artist_image_cache
    assert resp.status_code == 200  # the bytes WERE fetched; nothing was stored
    assert resp.content == b"PORTRAIT"
    assert list(tmp_path.iterdir()) == []
    assert cache.get("ABBA") is None


def test_the_mbid_is_passed_to_the_source() -> None:
    source = _RecordingSource(ResolvedImage(data=b"X", content_type="image/png"))
    resp = _client(source).post(_URL, params={"name": "ABBA", "source": "deezer"})
    assert resp.status_code == 200
    assert source.calls == [("ABBA", "the-mbid")]


def test_the_mbid_lookup_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    # get_artist_mbid is a blocking sqlite read over every album of the artist.
    # asyncio.get_running_loop() answers for the CALLING thread: it raises in a
    # threadpool worker and returns the loop if the handler dropped the hop.
    on_loop: list[bool] = []

    def probing(lib: object, name: str) -> str | None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_loop.append(False)
        else:
            on_loop.append(True)
        return "the-mbid"

    monkeypatch.setattr(library_mod, "get_artist_mbid", probing)
    source = _RecordingSource(ResolvedImage(data=b"X", content_type="image/png"))
    resp = _client(source).post(_URL, params={"name": "ABBA", "source": "deezer"})
    assert resp.status_code == 200
    assert on_loop == [False]


def test_confirmed_no_match_is_404_naming_the_source() -> None:
    resp = _client(_RecordingSource(None)).post(_URL, params={"name": "ABBA", "source": "deezer"})
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert "Deezer" in detail
    assert detail.isascii()


def test_transient_failure_is_502_not_404() -> None:
    # A source outage must not read as "this source has no photo of ABBA".
    resp = _client(_RecordingSource(boom=True)).post(
        _URL, params={"name": "ABBA", "source": "deezer"}
    )
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "Deezer" in detail
    assert detail.isascii()


def test_unconfigured_source_is_422_with_a_sentence() -> None:
    source = _RecordingSource(None)  # the registry holds deezer only
    resp = _client(source).post(_URL, params={"name": "ABBA", "source": "spotify"})
    assert resp.status_code == 422
    assert "not configured" in resp.json()["detail"]
    assert source.calls == []


def test_an_id_outside_the_literal_never_reaches_the_handler() -> None:
    """The gate in front of ``label_for`` -> ``X-Art-Source``.

    ``_HOSTILE_ID`` is in the registry, so widening ``source`` to ``str`` would
    make this a 200 carrying an injected header rather than a 422. Asserting on
    an UNREGISTERED id instead (say "lastfm") would pass either way - the
    handler's own "not configured" branch answers 422 too.
    """
    source = _RecordingSource(ResolvedImage(data=b"X", content_type="image/png"))
    resp = _client(source).post(_URL, params={"name": "ABBA", "source": _HOSTILE_ID})
    assert resp.status_code == 422
    # A VALIDATION error (a list of loc/msg objects), not the handler's sentence.
    detail = resp.json()["detail"]
    assert isinstance(detail, list)
    assert ["query", "source"] in [item["loc"] for item in detail]
    assert source.calls == []
    assert "x-injected" not in resp.headers
    assert "x-art-source" not in resp.headers


def test_disabled_feature_is_403_and_never_calls_the_source() -> None:
    source = _RecordingSource(ResolvedImage(data=b"X", content_type="image/png"))
    resp = _client(source, enabled=False).post(_URL, params={"name": "ABBA", "source": "deezer"})
    assert resp.status_code == 403
    assert source.calls == []


def test_slash_in_name_works_as_a_query_param() -> None:
    source = _RecordingSource(ResolvedImage(data=b"X", content_type="image/png"))
    resp = _client(source).post(_URL, params={"name": "AC/DC", "source": "deezer"})
    assert resp.status_code == 200
    assert source.calls == [("AC/DC", "the-mbid")]


def test_blank_name_is_422() -> None:
    source = _RecordingSource(ResolvedImage(data=b"X", content_type="image/png"))
    resp = _client(source).post(_URL, params={"name": "", "source": "deezer"})
    assert resp.status_code == 422
    assert source.calls == []


@pytest.mark.parametrize(
    ("declared", "served"),
    [
        # Non-ASCII: Starlette latin-1 encodes header values, so an unguarded
        # media_type raises INSIDE the app - TestClient surfaces this one.
        ("image/日本語", "application/octet-stream"),
        # The next two are FORWARD guards: h11 refuses these on the wire but
        # TestClient passes them through green, so the assertion is on the VALUE
        # that gets served, never on the status.
        ("image/png\tx", "application/octet-stream"),
        (" image/png ", "image/png"),
    ],
)
def test_a_header_hostile_content_type_from_a_source_is_never_sent_verbatim(
    declared: str, served: str
) -> None:
    """The source's OWN answer is the third content-type sink in this app.

    Every source in the tree downloads through app/artwork/download.py, which
    already guards this - but ``ArtistImageSource`` is a Protocol, so the
    guarantee is a convention, not a type.
    """
    source = _RecordingSource(ResolvedImage(data=b"X", content_type=declared))
    resp = _client(source).post(_URL, params={"name": "ABBA", "source": "deezer"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == served
    assert resp.content == b"X"


@pytest.mark.anyio
async def test_the_fetch_waits_on_the_automatic_chains_limiter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-path contention: ONE bucket, or two with the same numbers?

    The automatic chain (``service.get_artist_image``) takes the service's only
    concurrency slot and parks there. If the route shares that bucket, its
    resolve CANNOT start until the automatic call lets go. A private limiter
    inside the handler - even one built with identical parameters - would let it
    start immediately, and so would dropping the ``async with`` altogether;
    peak-concurrency measurement on one path cannot distinguish any of these.

    ``mbid_calls`` is what makes the negative assertion mean "parked at the
    limiter" rather than "still on its way there": the handler resolves the MBID
    BEFORE it takes the slot, so seeing that call complete while the source
    stays untouched puts the handler exactly between the two.
    """
    auto_started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingSource:
        async def resolve(self, name: str, *, mbid: str | None = None) -> ResolvedImage:
            auto_started.set()
            await release.wait()
            return ResolvedImage(data=b"AUTO", content_type="image/png")

    limiter = TokenBucketLimiter(rate_per_sec=1000.0, max_concurrency=1)
    service = ArtistImageService(
        source=_BlockingSource(),
        cache=ArtistImageCache(tmp_path),
        limiter=limiter,
        is_enabled=lambda: True,
        negative_ttl_seconds=1.0,
        transient_ttl_seconds=1.0,
    )
    manual = _RecordingSource(ResolvedImage(data=b"MANUAL", content_type="image/png"))
    registry = ArtistImageSources(ordered=((DEEZER, manual),))
    app.dependency_overrides[get_artist_image_sources] = lambda: registry
    app.dependency_overrides[get_artist_image_service] = lambda: service
    app.dependency_overrides[get_library] = lambda: _StubHandle()

    mbid_calls: list[str] = []

    def recording(lib: object, name: str) -> str | None:
        mbid_calls.append(name)
        return "the-mbid"

    monkeypatch.setattr(library_mod, "get_artist_mbid", recording)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        auto = asyncio.create_task(service.get_artist_image("Some Other Artist"))
        try:
            # Ordered, not raced: the automatic call must HOLD the single slot
            # before the request is fired, or the request could take it first
            # and this would deadlock on a correct implementation.
            await asyncio.wait_for(auto_started.wait(), timeout=5.0)
            request = asyncio.create_task(
                client.post(_URL, params={"name": "ABBA", "source": DEEZER})
            )
            try:
                await asyncio.sleep(0.1)
                assert mbid_calls == ["ABBA"], "the request never got as far as the limiter"
                assert manual.calls == [], "the manual fetch resolved without the shared slot"
                assert not request.done()
                release.set()
                # Positive control: once the slot frees, the same call goes through.
                resp = await asyncio.wait_for(request, timeout=5.0)
                assert resp.status_code == 200
                assert resp.content == b"MANUAL"
                assert manual.calls == [("ABBA", "the-mbid")]
            finally:
                if not request.done():
                    request.cancel()
                await asyncio.gather(request, return_exceptions=True)
        finally:
            release.set()
            await asyncio.gather(auto, return_exceptions=True)
