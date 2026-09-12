"""Reset to auto moves an uploaded portrait to Trash; it never unlinks it.

The bytes are a file the user uploaded or pasted by hand, so every state this
route may leave behind has them "inside one Trash entry with an origin record"
or "still served, nothing reset at all" - a move that fails part-way is the
first of those with the slots left alone. Every test here goes through
the REAL store resolver (``checked_store_dirs``), which is why it needs a real
library handle rather than the stub
``tests/test_artist_image_override_endpoint.py`` resets against.
"""

from __future__ import annotations

import asyncio
import errno
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
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
from app.beets.artist_art import ArtTrashStore
from app.beets.library import LibraryHandle
from app.beets.store_layout import StoreLayoutError, checked_store_dirs
from app.beets.trash_origins import read_trash_origin
from app.config import settings as app_settings
from app.main import app
from tests.conftest import beets_dir_for, make_test_handle

PNG = (Path(__file__).parent / "fixtures" / "cover.png").read_bytes()
RESET = "/api/artists/image/reset"
#: The hand copy of ``artists_mod._MOVE_FAILED``, so an edit to the wording has
#: to be made twice and cannot pass unnoticed. ``ArtistImageEditPanel.test.tsx``
#: keeps a third copy for the panel's error branch.
MOVE_FAILED = (
    "The uploaded image could not be fully moved to Trash, so the reset stopped;"
    " check Trash before retrying."
)


class _Locked(asyncio.Lock):
    """A REAL ``asyncio.Lock`` that reports itself held.

    ``tests/test_trash_api.py`` can use a bare stub because its routes never get
    past the gate; this route's own ``async with _swap_lock(...)`` asserts the
    object is an ``asyncio.Lock``, so a stub would kill the mutant on that
    assert instead of on the status. Acquiring still works, which is the point:
    drop the pre-check and the reset proceeds to 200 rather than hanging.
    """

    def locked(self) -> bool:
        return True


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
    assert resp.json()["detail"].startswith(MOVE_FAILED)
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


def test_a_move_that_fails_part_way_says_the_reset_stopped_not_that_nothing_moved(
    client: TestClient,
    cache: ArtistImageCache,
    cache_dir: Path,
    store: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 503 has to be true of a PARTIAL move, which is a supported outcome.

    ``cache.override_files`` returns bytes then sidecar and
    ``trash_replaced_files`` writes the origin record for whatever landed, so a
    failure on the second move leaves the portrait itself in Trash with a
    record. "Nothing was reset" sent the user back to the panel without looking
    there. Both slots are untouched, which is the half that IS all-or-nothing.
    """
    trash_dir, origins_dir = store
    cache.store_positive("ABBA", b"auto bytes", "image/png")
    cache.write_override("ABBA", PNG, "image/png")
    real_rename = os.rename
    moves = 0

    def fail_on_the_second(*args: Any, **kwargs: Any) -> None:
        nonlocal moves
        if "dst_dir_fd" not in kwargs:  # the mover's own move, not the app's others
            real_rename(*args, **kwargs)
            return
        moves += 1
        if moves == 2:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), str(args[1]))
        real_rename(*args, **kwargs)

    # ``os`` is one shared module object, so patching it here is what the mover
    # sees. (Reaching through ``app.beets.trash.os`` fails mypy strict: the
    # module does not explicitly export the name.)
    monkeypatch.setattr(os, "rename", fail_on_the_second)

    resp = client.post(RESET, params={"name": "ABBA"})

    assert resp.status_code == 503
    assert moves == 2, "the mover's own renames were not the ones spied on"
    assert resp.json()["detail"] == f"{MOVE_FAILED} No space left on device"
    entry = trash_dir / "ABBA - artist image"
    (image,) = list(entry.glob("*.override"))
    assert image.read_bytes() == PNG  # the portrait IS in Trash, so say so
    record = read_trash_origin(origins_dir, entry.name)
    assert record is not None
    assert record.origin == str(cache_dir)
    assert record.moved == "files"
    # The reset itself stopped: the automatic slot and the orphan sidecar are
    # exactly as they were.
    assert sorted(p.name.split(".", 1)[1] for p in cache_dir.iterdir()) == [
        "bin",
        "mime",
        "override.mime",
    ]
    # A retry clears the AUTOMATIC slot and answers ``cleared_override: False``:
    # ``override_files`` is keyed on the bytes, which are in Trash already. No
    # reset removes the orphan sidecar; it stays until the next upload's
    # ``write_override`` overwrites it or a rename purges the key.
    monkeypatch.setattr(os, "rename", real_rename)
    retry = client.post(RESET, params={"name": "ABBA"})
    assert retry.status_code == 200
    assert retry.json() == {"ok": True, "cleared_override": False, "cleared_auto": True}
    assert [p.name.split(".", 1)[1] for p in cache_dir.iterdir()] == ["override.mime"]


def test_the_503_carries_the_oserrors_strerror_and_no_server_path(
    client: TestClient,
    cache: ArtistImageCache,
    store: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wire gets ``strerror``; the absolute path is logged instead.

    The same split ``trash_origins._store_unusable`` makes, and the sibling arm
    of this very ``except`` raises a deliberately path-free sentence — relaying
    ``str(OSError)`` put the Trash dir in the response body beside it.
    """
    trash_dir, _origins = store
    cache.write_override("ABBA", PNG, "image/png")

    real_rename = os.rename
    refused: list[bool] = []

    def refuse(*args: Any, **kwargs: Any) -> None:
        if "dst_dir_fd" not in kwargs:
            real_rename(*args, **kwargs)
            return
        refused.append(True)
        raise OSError(errno.EACCES, os.strerror(errno.EACCES), str(args[1]))

    monkeypatch.setattr(os, "rename", refuse)

    resp = client.post(RESET, params={"name": "ABBA"})

    assert resp.status_code == 503
    assert refused, "the mover's own rename was not the one spied on"
    detail = resp.json()["detail"]
    assert detail == f"{MOVE_FAILED} Permission denied"
    assert str(trash_dir) not in detail


def test_a_trash_root_swapped_after_the_check_answers_503_and_keeps_the_upload(
    client: TestClient,
    cache: ArtistImageCache,
    store: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Trash root the mover opens is the directory THIS request examined.

    ``checked_store_dirs`` resolves a path and ``checked_protected_trees``
    stats it; the swap goes in between that pair and the move, which is the
    window a party who can write the Trash's parent has. Measured before the
    identity compare: the container and the portrait landed in
    ``somewhere-else`` and the origin record named an entry that does not exist.
    """
    trash_dir, _origins = store
    trash_dir.mkdir(parents=True)  # there at check time, so it HAS an identity
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    cache.write_override("ABBA", PNG, "image/png")
    real_store = artists_mod._checked_art_trash_store

    def check_then_swap(handle: LibraryHandle, settings: Any) -> ArtTrashStore:
        checked = real_store(handle, settings)
        os.rename(checked.trash_dir, tmp_path / "real-trash")
        os.symlink(elsewhere, checked.trash_dir)
        return checked

    monkeypatch.setattr(artists_mod, "_checked_art_trash_store", check_then_swap)

    resp = client.post(RESET, params={"name": "ABBA"})

    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail == f"{MOVE_FAILED} the Trash directory changed after it was checked"
    assert str(trash_dir) not in detail
    assert list(elsewhere.iterdir()) == [], "nothing landed outside the checked Trash"
    assert list((tmp_path / "real-trash").iterdir()) == []
    served = cache.get("ABBA")
    assert isinstance(served, CachedImage)
    assert served.data == PNG, "the upload is still served, nothing was reset"


def test_the_move_and_the_clear_run_under_the_beets_swap_lock(
    client: TestClient, cache: ArtistImageCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This route is a Trash MUTATOR, so it holds what the other three hold.

    ``app/api/trash.py``'s restore, empty-one and empty-all all take the beets
    swap lock, and their own gate refuses while it is held. Without it an
    Empty-all landing between the container's ``mkdir`` and the move rmtree'd
    the upload this feature exists to protect — and a config Apply landing
    there moved it into the Trash dir that is no longer the app's. Measured the
    way ``test_trash_api.test_empty_one_holds_swap_lock_during_removal``
    measures it: the blocking steps read the lock themselves.
    """
    from app.main import app

    seen: dict[str, bool] = {}

    def held() -> bool:
        lock = getattr(app.state, "beets_swap_lock", None)
        return lock is not None and bool(lock.locked())

    real_trash = artists_mod._trash_override_files
    real_slots = artists_mod._clear_auto_slot

    def move_spy(files: list[Path], name: str, store: ArtTrashStore) -> None:
        seen["move"] = held()
        real_trash(files, name, store)

    def slots_spy(cache_: ArtistImageCache, name: str) -> bool:
        seen["clear"] = held()
        return real_slots(cache_, name)

    monkeypatch.setattr(artists_mod, "_trash_override_files", move_spy)
    monkeypatch.setattr(artists_mod, "_clear_auto_slot", slots_spy)
    cache.write_override("ABBA", PNG, "image/png")

    assert client.post(RESET, params={"name": "ABBA"}).status_code == 200

    assert seen == {"move": True, "clear": True}


def test_a_held_swap_lock_refuses_the_reset_instead_of_queueing_behind_it(
    client: TestClient, cache: ArtistImageCache, store: tuple[Path, Path]
) -> None:
    """409 with the Trash routes' own sentence, rather than waiting on the lock.

    No holder is bounded - a restore re-imports, a duplicates merge runs a whole
    batch - and ``apiFetch`` sets no timeout, so waiting pins the confirm dialog
    with Cancel disabled for as long as the holder runs. The LOCK half only: an
    import never holds the lock, so the full library-busy union would refuse a
    portrait reset for the length of one.
    """
    from app.main import app

    trash_dir, _origins = store
    cache.write_override("ABBA", PNG, "image/png")
    app.state.beets_swap_lock = _Locked()
    try:
        resp = client.post(RESET, params={"name": "ABBA"})
    finally:
        del app.state.beets_swap_lock

    assert resp.status_code == 409
    assert resp.json()["detail"] == "A library operation is in progress; try again when it finishes"
    assert not trash_dir.exists()  # nothing moved
    served = cache.get("ABBA")
    assert isinstance(served, CachedImage)
    assert served.data == PNG  # the upload is still what the artist serves


def test_the_art_sweep_gate_is_asked_again_with_the_lock_held(
    client: TestClient,
    cache: ArtistImageCache,
    cache_dir: Path,
    store: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sweep that starts between the first gate and the lock must still win.

    The pre-lock gate reads a flag, and its own 409 window plus the acquire can
    outlive that read; a sweep starting in there re-stores the slot the clear is
    about to empty, so the user presses Reset and watches nothing change. The
    flag is flipped on the gate's SECOND read, which is the only call the inner
    check makes - drop that check and this answers 200.

    The LOCK STATE at each read, not a count of them: two reads before the
    ``async with`` answer 409 with the same ``len(reads) == 2``, so a count
    cannot tell the fix from that mutant. Measured the way
    ``test_the_move_and_the_clear_run_under_the_beets_swap_lock`` measures it.
    """
    trash_dir, _origins = store
    reads: list[bool] = []

    def held() -> bool:
        lock = getattr(app.state, "beets_swap_lock", None)
        return lock is not None and bool(lock.locked())

    def sweep_starts_on_the_second_read() -> bool:
        reads.append(held())
        return len(reads) >= 2

    monkeypatch.setattr(artists_mod, "artist_art_backfill_active", sweep_starts_on_the_second_read)
    cache.write_override("ABBA", PNG, "image/png")

    resp = client.post(RESET, params={"name": "ABBA"})

    assert resp.status_code == 409
    assert reads == [False, True]  # asked before the lock AND with it held
    assert not trash_dir.exists()
    key = cache._key("ABBA")
    assert (cache_dir / f"{key}.override").read_bytes() == PNG  # nothing cleared


def test_an_upload_that_lands_after_the_move_survives_the_reset(
    client: TestClient,
    cache: ArtistImageCache,
    cache_dir: Path,
    store: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reset removes exactly the files it moved, never "whatever is there".

    The override upload route takes no swap lock, so a pair can land in the
    cache dir between the move and the clear. A slot-scoped unlink of the
    override pair would remove THAT pair - bytes that never reached Trash and
    are gone for good. Simulated by writing the fresh pair from inside the
    mover, which is the window itself.
    """
    trash_dir, _origins = store
    fresh = b"a second upload, still wanted"
    cache.write_override("ABBA", PNG, "image/png")
    real_move = artists_mod._trash_override_files

    def move_then_upload(files: list[Path], name: str, art_store: ArtTrashStore) -> None:
        real_move(files, name, art_store)
        cache.write_override("ABBA", fresh, "image/jpeg")

    monkeypatch.setattr(artists_mod, "_trash_override_files", move_then_upload)

    body = client.post(RESET, params={"name": "ABBA"}).json()

    # Honest either way: a pair WAS moved, which is what cleared_override means.
    assert body == {"ok": True, "cleared_override": True, "cleared_auto": False}
    key = cache._key("ABBA")
    assert (cache_dir / f"{key}.override").read_bytes() == fresh
    assert (cache_dir / f"{key}.override.mime").read_text() == "image/jpeg"
    served = cache.get("ABBA")
    assert isinstance(served, CachedImage)
    assert served.data == fresh
    assert served.content_type == "image/jpeg"
    # The OLD pair is the one in Trash, alone.
    (image,) = list((trash_dir / "ABBA - artist image").glob("*.override"))
    assert image.read_bytes() == PNG
