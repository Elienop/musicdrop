"""GET /api/artists/image/sources - which sources THIS artist can be fetched from.

Availability is a per-artist question, not a per-install one: fanart.tv answers
only by MusicBrainz id, so it can be fully configured and still be unusable for
one particular artist. The frontend cannot decide that (the MBID lives in
beets), which is the whole reason this endpoint exists.
"""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.api.artists import _source_option, get_artist_image_sources
from app.artwork.factory import (
    DEEZER,
    FANARTTV,
    ArtistImageSources,
    build_artist_image_sources,
)
from app.beets import library as library_mod
from app.config import Settings
from app.main import app

_URL = "/api/artists/image/sources"


class _StubHandle:
    """Minimal LibraryHandle stand-in; only ``.lib`` is read, and only to be
    handed straight to the patched ``get_artist_mbid``."""

    lib = object()


@dataclass(frozen=True)
class _Env:
    """A client PLUS the very registry the route resolves through.

    The ordering test compares the response against ``sources.ids()`` from this
    exact object rather than a restated ``["fanarttv", "spotify", "deezer"]``:
    the operative contract is "the endpoint preserves the factory's chain
    order", and the factory's absolute order is pinned separately in
    tests/test_artwork_factory.py and tests/test_artist_models.py.
    """

    client: TestClient
    sources: ArtistImageSources


def _env(*, fanart: bool, spotify: bool) -> Iterator[_Env]:
    # Every credential is passed explicitly so the dev .env cannot change which
    # sources the factory builds.
    settings = Settings(
        artist_image_fanarttv_api_key="K" if fanart else "",
        artist_image_spotify_client_id="ID" if spotify else "",
        artist_image_spotify_client_secret="SEC" if spotify else "",
    )
    sources = build_artist_image_sources(httpx.AsyncClient(), settings)
    app.dependency_overrides[get_artist_image_sources] = lambda: sources
    app.dependency_overrides[get_library] = lambda: _StubHandle()
    yield _Env(client=TestClient(app), sources=sources)
    app.dependency_overrides.clear()


@pytest.fixture
def keyless_env() -> Iterator[_Env]:
    yield from _env(fanart=False, spotify=False)


@pytest.fixture
def full_env() -> Iterator[_Env]:
    yield from _env(fanart=True, spotify=True)


@pytest.fixture(autouse=True)
def no_mbid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(library_mod, "get_artist_mbid", lambda lib, name: None)


def test_keyless_install_offers_deezer_only(keyless_env: _Env) -> None:
    resp = keyless_env.client.get(_URL, params={"name": "ABBA"})
    assert resp.status_code == 200
    assert resp.json() == {
        "sources": [{"id": "deezer", "label": "Deezer", "available": True, "reason": None}]
    }


def test_configured_sources_are_listed_in_the_factorys_chain_order(full_env: _Env) -> None:
    body = full_env.client.get(_URL, params={"name": "ABBA"}).json()
    # Not vacuous: a one-element list would make any ordering claim trivially true.
    assert len(full_env.sources.ids()) == 3
    assert [s["id"] for s in body["sources"]] == list(full_env.sources.ids())


def test_fanart_is_listed_unavailable_with_a_reason_when_the_artist_has_no_mbid(
    full_env: _Env,
) -> None:
    body = full_env.client.get(_URL, params={"name": "ABBA"}).json()
    fanart = next(s for s in body["sources"] if s["id"] == FANARTTV)
    assert fanart["available"] is False
    assert "MusicBrainz" in (fanart["reason"] or "")
    # Everything else answers by name, so it stays available.
    assert all(s["available"] for s in body["sources"] if s["id"] != FANARTTV)


def test_fanart_is_available_when_the_artist_has_an_mbid(
    full_env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(library_mod, "get_artist_mbid", lambda lib, name: "the-mbid")
    body = full_env.client.get(_URL, params={"name": "ABBA"}).json()
    fanart = next(s for s in body["sources"] if s["id"] == FANARTTV)
    assert fanart == {"id": "fanarttv", "label": "fanart.tv", "available": True, "reason": None}


@pytest.mark.parametrize(("mbid", "expect_blocked"), [(None, True), ("the-mbid", False)])
def test_an_unavailable_source_always_explains_itself(
    full_env: _Env, monkeypatch: pytest.MonkeyPatch, mbid: str | None, expect_blocked: bool
) -> None:
    """The correlation the models do NOT enforce.

    ``ArtistImageSourceOption`` carries no validator tying ``available`` to
    ``reason`` (one was rejected - it would not survive into the generated TS),
    so ``available: false, reason: null`` is a representable response and would
    render an empty explanation exactly where the user needs a sentence. This
    is where that guarantee lives.
    """
    monkeypatch.setattr(library_mod, "get_artist_mbid", lambda lib, name: mbid)
    body = full_env.client.get(_URL, params={"name": "ABBA"}).json()
    blocked = [s for s in body["sources"] if not s["available"]]
    # Non-vacuity: the no-MBID arm MUST actually produce a blocked source, or
    # the loop below asserts over nothing.
    assert bool(blocked) is expect_blocked
    for source in body["sources"]:
        if source["available"]:
            assert source["reason"] is None
        else:
            reason = source["reason"]
            assert isinstance(reason, str)
            assert reason.strip(), f"{source['id']} is unavailable with no reason"
            assert reason.isascii(), f"{source['id']} reason is not ASCII: {reason!r}"


def test_the_mbid_lookup_is_skipped_when_fanart_is_not_configured(
    keyless_env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def counting(lib: object, name: str) -> str | None:
        calls.append(name)
        return None

    monkeypatch.setattr(library_mod, "get_artist_mbid", counting)
    resp = keyless_env.client.get(_URL, params={"name": "ABBA"})
    # Assert the route actually answered: a 404/422 would leave the counter at
    # zero for the wrong reason.
    assert resp.status_code == 200
    assert calls == []  # no fanart source -> no beets query


def test_the_mbid_lookup_runs_once_with_the_artist_name_when_fanart_is_configured(
    full_env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Control arm for the test above: it proves the counter CAN move, so a
    # zero there means "not called" rather than "patched the wrong name".
    calls: list[str] = []

    def counting(lib: object, name: str) -> str | None:
        calls.append(name)
        return None

    monkeypatch.setattr(library_mod, "get_artist_mbid", counting)
    assert full_env.client.get(_URL, params={"name": "AC/DC"}).status_code == 200
    assert calls == ["AC/DC"]


def test_the_mbid_lookup_runs_off_the_event_loop(
    full_env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # get_artist_mbid is a blocking sqlite read; on a big library it would stall
    # every other request if it ran inline. asyncio.get_running_loop() answers
    # for the CALLING thread, so it raises in a threadpool worker and returns
    # the loop if the handler dropped the run_in_threadpool hop.
    on_loop: list[bool] = []

    def probing(lib: object, name: str) -> str | None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_loop.append(False)
        else:
            on_loop.append(True)
        return None

    monkeypatch.setattr(library_mod, "get_artist_mbid", probing)
    assert full_env.client.get(_URL, params={"name": "ABBA"}).status_code == 200
    assert on_loop == [False]


def test_slash_in_name_works_as_a_query_param(keyless_env: _Env) -> None:
    resp = keyless_env.client.get(_URL, params={"name": "AC/DC"})
    assert resp.status_code == 200
    assert [s["id"] for s in resp.json()["sources"]] == [DEEZER]


def test_missing_name_is_422(keyless_env: _Env) -> None:
    assert keyless_env.client.get(_URL).status_code == 422


def test_blank_name_is_422(keyless_env: _Env) -> None:
    assert keyless_env.client.get(_URL, params={"name": ""}).status_code == 422


def test_a_blocked_source_derives_available_from_its_reason() -> None:
    # The option builder is the ONLY place these are constructed, and it derives
    # `available` from the reason so the inconsistent pair is unrepresentable.
    option = _source_option(FANARTTV, "because reasons")
    assert option.available is False
    assert option.reason == "because reasons"
    assert option.label == "fanart.tv"


def test_an_unblocked_source_is_available_with_no_reason() -> None:
    option = _source_option(DEEZER, None)
    assert option.available is True
    assert option.reason is None
    assert option.label == "Deezer"
