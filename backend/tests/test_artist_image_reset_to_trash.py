"""Reset to auto moves an uploaded portrait to Trash; it never unlinks it.

The bytes are a file the user uploaded or linked by hand, so the only two
states this route may leave behind are "inside one Trash entry with an origin
record" and "still served, nothing reset at all". Every test here goes through
the REAL store resolver (``checked_store_dirs``), which is why it needs a real
library handle rather than the stub
``tests/test_artist_image_override_endpoint.py`` resets against.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest
from beets.library import Library
from fastapi.concurrency import run_in_threadpool as _real_run_in_threadpool
from fastapi.testclient import TestClient

import app.api.artists as artists_mod
from app.api.albums import get_library
from app.api.artists import (
    get_artist_image_cache,
    get_artist_image_http_client,
    get_artist_image_service,
)
from app.artwork.cache import ArtistImageCache, CachedImage
from app.beets.library import LibraryHandle
from app.beets.store_layout import StoreLayoutError, checked_store_dirs
from app.beets.trash_origins import read_trash_origin
from app.config import settings as app_settings
from app.main import app
from tests.conftest import beets_dir_for, make_test_handle

PNG = (Path(__file__).parent / "fixtures" / "cover.png").read_bytes()
RESET = "/api/artists/image/reset"


class _OffService:
    """The feature reported OFF, so no background refill runs beside the move."""

    def is_enabled(self) -> bool:
        return False


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "artist-images"


@pytest.fixture
def cache(cache_dir: Path) -> ArtistImageCache:
    return ArtistImageCache(cache_dir)


@pytest.fixture
def handle(edit_lib: Library, tmp_path: Path) -> LibraryHandle:
    return make_test_handle(edit_lib, beets_dir_for(tmp_path))


@pytest.fixture
def store(handle: LibraryHandle) -> tuple[Path, Path]:
    """The pair the route must use: whatever the CHECKED resolver answers.

    Taken from the same function the route resolves through, so a route that
    quietly moved files somewhere else would fail these tests rather than pass
    them against a path this file made up.
    """
    return checked_store_dirs(app_settings, handle)


@pytest.fixture
def client(cache: ArtistImageCache, handle: LibraryHandle) -> Iterator[TestClient]:
    app.dependency_overrides[get_artist_image_cache] = lambda: cache
    app.dependency_overrides[get_artist_image_http_client] = lambda: object()
    app.dependency_overrides[get_artist_image_service] = lambda: _OffService()
    app.dependency_overrides[get_library] = lambda: handle
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_the_uploaded_override_lands_in_one_trash_entry_with_its_origin(
    client: TestClient, cache: ArtistImageCache, cache_dir: Path, store: tuple[Path, Path]
) -> None:
    """THE data-loss fix: the pair moves to Trash instead of being unlinked.

    One entry, both files, and a record naming the cache dir — which is what
    tells a user where to copy them back to.
    """
    trash_dir, origins_dir = store
    cache.write_override("ABBA", PNG, "image/png")

    body = client.post(RESET, params={"name": "ABBA"}).json()

    assert body == {"ok": True, "cleared_override": True, "cleared_auto": False}
    entry = trash_dir / "ABBA - artist image"
    (image,) = list(entry.glob("*.override"))
    (mime,) = list(entry.glob("*.override.mime"))
    assert image.read_bytes() == PNG  # the uploaded bytes themselves
    assert mime.read_text() == "image/png"
    assert mime.name == f"{image.name}.mime"  # one key's pair, kept together
    record = read_trash_origin(origins_dir, entry.name)
    assert record is not None
    assert record.origin == str(cache_dir)
    assert record.moved == "files"
    assert cache.get("ABBA") is None
    assert list(cache_dir.iterdir()) == []  # nothing left behind in the cache


def test_a_reset_with_no_override_resolves_no_store_and_creates_no_trash(
    client: TestClient,
    cache: ArtistImageCache,
    store: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An automatic-only reset pays nothing for this feature and leaves no row.

    The layout walk is a dozen stats and a Trash dir created for an empty
    container would show up in Settings for good.
    """
    trash_dir, _origins = store
    cache.store_positive("ABBA", PNG, "image/png")
    monkeypatch.setattr(
        artists_mod,
        "_checked_art_trash_store",
        Mock(side_effect=AssertionError("no override: the store must not be resolved")),
    )

    body = client.post(RESET, params={"name": "ABBA"}).json()

    assert body == {"ok": True, "cleared_override": False, "cleared_auto": True}
    assert not trash_dir.exists()


def test_a_refused_store_answers_503_with_its_sentence_and_resets_nothing(
    client: TestClient,
    cache: ArtistImageCache,
    cache_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused layout must not degrade into an unlink, and must not reset the
    automatic slot either: the user pressed one button and gets one outcome.

    All four slot files are still in the cache dir afterwards (the key is a hex
    digest, so the suffix after the first dot is the slot).
    """
    cache.store_positive("ABBA", b"auto bytes", "image/png")
    cache.write_override("ABBA", PNG, "image/png")

    def refuse(*_a: object, **_kw: object) -> object:
        raise StoreLayoutError("Trash is inside the music library")

    monkeypatch.setattr(artists_mod, "_checked_art_trash_store", refuse)

    resp = client.post(RESET, params={"name": "ABBA"})

    assert resp.status_code == 503
    assert resp.json()["detail"] == "Trash is inside the music library"
    served = cache.get("ABBA")
    assert isinstance(served, CachedImage)
    assert served.data == PNG  # still the portrait the user uploaded
    assert sorted(p.name.split(".", 1)[1] for p in cache_dir.iterdir()) == [
        "bin",
        "mime",
        "override",
        "override.mime",
    ]


@pytest.mark.skipif(os.getuid() == 0, reason="root writes a read-only directory anyway")
def test_a_store_the_mover_cannot_write_answers_503_and_keeps_the_upload(
    client: TestClient, cache: ArtistImageCache, store: tuple[Path, Path]
) -> None:
    """The other half of the refusal: the layout is fine and the store is not.

    A read-only origins dir is the fault ``require_usable_store`` refuses on,
    and it is refused BEFORE the Trash dir is created — so the 503 names the
    cause and the override is still where it was.
    """
    trash_dir, origins_dir = store
    origins_dir.mkdir(parents=True)
    origins_dir.chmod(0o500)
    cache.write_override("ABBA", PNG, "image/png")

    try:
        resp = client.post(RESET, params={"name": "ABBA"})
    finally:
        origins_dir.chmod(0o700)

    assert resp.status_code == 503
    assert resp.json()["detail"].startswith(
        "The uploaded image could not be moved to Trash, so nothing was reset: "
    )
    assert not trash_dir.exists()
    served = cache.get("ABBA")
    assert isinstance(served, CachedImage)
    assert served.data == PNG


def test_the_move_runs_off_the_event_loop(
    client: TestClient, cache: ArtistImageCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An lstat per file, a layout walk and up-to-10MB of copying (the cache dir
    # and Trash can be on different volumes) — none of it may run on the loop.
    cache.write_override("ABBA", PNG, "image/png")
    spy = Mock(side_effect=lambda fn, *a, **k: _real_run_in_threadpool(fn, *a, **k))
    monkeypatch.setattr(artists_mod, "run_in_threadpool", spy, raising=False)

    assert client.post(RESET, params={"name": "ABBA"}).status_code == 200
    offloaded = [call.args[0] for call in spy.call_args_list]
    assert artists_mod._trash_override_files in offloaded
    assert artists_mod._checked_art_trash_store in offloaded


def test_a_slash_in_the_artist_name_does_not_nest_the_container(
    client: TestClient, cache: ArtistImageCache, store: tuple[Path, Path]
) -> None:
    """The container is named from a display NAME, not a folder name.

    "AC/DC" would otherwise be a container at ``<trash>/AC/DC - artist image``:
    a Trash entry the page never lists (only top-level entries are listed) and
    an origin record keyed on a name that is not the entry's.
    """
    trash_dir, origins_dir = store
    cache.write_override("AC/DC", PNG, "image/png")

    assert client.post(RESET, params={"name": "AC/DC"}).status_code == 200

    assert [p.name for p in trash_dir.iterdir()] == ["AC_DC - artist image"]
    record = read_trash_origin(origins_dir, "AC_DC - artist image")
    assert record is not None
    assert record.moved == "files"


def test_a_dot_leading_artist_name_gets_a_listable_container(
    client: TestClient, cache: ArtistImageCache, store: tuple[Path, Path]
) -> None:
    # A dot-leading top-level entry is skipped by the Trash listing and removed
    # by Empty-all: the upload would be invisible until it was gone.
    trash_dir, _origins = store
    cache.write_override(".hack", PNG, "image/png")

    assert client.post(RESET, params={"name": ".hack"}).status_code == 200

    assert [p.name for p in trash_dir.iterdir()] == ["hack - artist image"]
