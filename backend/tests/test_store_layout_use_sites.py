"""The containment check at the DESTRUCTIVE call sites, not only at boot.

Most of the rule's paths come from the environment, so the implementer's first
pass checked once at startup and stopped. That is true of the configured STRINGS
and false of what they resolve to: ``resolve_trash_dir``
calls ``Path.resolve()`` on every request, so a symlink dropped at the Trash path
after startup re-arms the whole loss. Measured in the review round — a good boot,
then ``rmdir <M>/.trash; ln -s <M> <M>/.trash``, then ``DELETE /api/trash/all``
answered ``200 {"removed": 3}`` and the music library was empty.

Every test here therefore does the same thing: boot a layout the gate accepts,
change the filesystem under it, and ask a route that deletes or moves. Each has a
control on the same fixture, because a 503 from a check that refused everything
would satisfy the refusal half on its own.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.beets.library import LibraryHandle
from tests.conftest import beets_dir_for, build_library, make_test_handle


@pytest.fixture
def trash_inside_music(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """``MUSICDROP_TRASH_DIR=<M>/.trash`` — the layout README and compose recommend.

    Returns the Trash dir, with one album in the library and one entry already in
    Trash, so a route that removed everything and a route that removed nothing
    are told apart by what is left on disk rather than by the status alone.

    The album is a real DB ROW, not just files: the album-scoped routes check
    that the album exists BEFORE they take the pair, so against an empty library
    every one of them answers 404 and a layout assertion on them proves nothing.
    """
    import os

    from beets.library import Item

    music = Path(beets_library.lib.directory.decode())
    (music / "Artist A" / "Album").mkdir(parents=True)
    (music / "Artist A" / "Album" / "01.flac").write_bytes(b"\x00")
    item = Item(album="Album", albumartist="Artist A", artist="Artist A", title="Track 1", track=1)
    item.path = os.fsencode(str(music / "Artist A" / "Album" / "01.flac"))
    beets_library.lib.add_album([item])
    trash = music / ".trash"
    (trash / "Old Entry").mkdir(parents=True)
    (trash / "Old Entry" / "cover.jpg").write_bytes(b"\x00")
    monkeypatch.setattr("app.config.settings.trash_dir", str(trash))
    # OUT of the library: a Trash inside ``M`` is allowed, an origin store inside
    # it is not, so the sibling ``origins_for`` would build is itself refused.
    monkeypatch.setattr("app.config.settings.trash_origins_dir", str(music.parent / "records"))
    return trash


def _point_trash_at_the_library(trash: Path, music: Path) -> None:
    """Replace the Trash directory with a symlink to the music library.

    Needs write access to the music share and nothing else — which on a NAS is
    what the SMB/NFS users have, and what beets itself has.
    """
    shutil.rmtree(trash)
    trash.symlink_to(music)


def test_empty_all_refuses_after_the_trash_dir_becomes_the_library(
    client: TestClient, beets_library: LibraryHandle, trash_inside_music: Path
) -> None:
    """The measured loss: 200 and an empty music library. Now a 503, and the album stays."""
    music = Path(beets_library.lib.directory.decode())
    _point_trash_at_the_library(trash_inside_music, music)

    r = client.delete("/api/trash/all")

    assert r.status_code == 503, r.text
    assert "The Trash directory is the music library" in r.json()["detail"]
    assert (music / "Artist A" / "Album" / "01.flac").is_file()


def test_empty_all_still_clears_a_trash_that_is_where_it_belongs(
    client: TestClient, beets_library: LibraryHandle, trash_inside_music: Path
) -> None:
    """The control. ``<M>/.trash`` is ALLOWED, and Empty Trash still empties it."""
    music = Path(beets_library.lib.directory.decode())

    r = client.delete("/api/trash/all")

    assert r.status_code == 200, r.text
    assert r.json()["removed"] == 1
    assert list(trash_inside_music.iterdir()) == []
    assert (music / "Artist A" / "Album" / "01.flac").is_file()


def test_empty_one_refuses_after_the_swap(
    client: TestClient, beets_library: LibraryHandle, trash_inside_music: Path
) -> None:
    """The per-entry route resolves the same root, so it loses the same way.

    Measured in the review round: after the swap ``DELETE /api/trash?folder=Artist A``
    deleted ``Artist A`` from the LIBRARY, because ``resolve_trash_child``'s guard
    covers the child and not the root it is resolved against.
    """
    music = Path(beets_library.lib.directory.decode())
    _point_trash_at_the_library(trash_inside_music, music)

    r = client.delete("/api/trash", params={"folder": "Artist A"})

    assert r.status_code == 503, r.text
    # The SENTENCE, not just the tier: this route can 503 for an unusable origin
    # store too, and a status-only assertion is satisfied by either.
    assert "The Trash directory is the music library" in r.json()["detail"]
    assert (music / "Artist A" / "Album" / "01.flac").is_file()


def test_listing_the_trash_refuses_after_the_swap(
    client: TestClient, beets_library: LibraryHandle, trash_inside_music: Path
) -> None:
    """Read-only, and still refused: after the swap the listing offers every
    ARTIST as a trashed album, each with a Delete button beside it."""
    _point_trash_at_the_library(trash_inside_music, Path(beets_library.lib.directory.decode()))

    r = client.get("/api/trash")

    assert r.status_code == 503, r.text
    assert "The Trash directory is the music library" in r.json()["detail"]


def test_the_reorganize_preview_refuses_after_the_swap(
    client: TestClient, beets_library: LibraryHandle, trash_inside_music: Path
) -> None:
    """The sweep moves husks INTO the Trash root, so it asks the same question."""
    _point_trash_at_the_library(trash_inside_music, Path(beets_library.lib.directory.decode()))

    r = client.get("/api/reorganize/preview")

    assert r.status_code == 503, r.text
    assert "The Trash directory is the music library" in r.json()["detail"]


def test_the_reorganize_preview_still_works_on_the_allowed_layout(
    client: TestClient, trash_inside_music: Path
) -> None:
    """The control for the route above."""
    r = client.get("/api/reorganize/preview")
    assert r.status_code == 200, r.text


def test_delete_album_refuses_after_the_swap(
    client: TestClient, beets_library: LibraryHandle, trash_inside_music: Path
) -> None:
    """The delete path takes the pair from the same helper, so it refuses too.

    Asserted through the HTTP route rather than the op, because the op's 503
    only matters if the route declares and returns it.
    """
    music = Path(beets_library.lib.directory.decode())
    _point_trash_at_the_library(trash_inside_music, music)

    r = client.delete("/api/albums/1")

    assert r.status_code == 503, r.text
    assert "Nothing has been deleted." in r.json()["detail"]
    assert (music / "Artist A" / "Album" / "01.flac").is_file()


def test_the_orphan_sweep_skips_with_a_warning_rather_than_failing_the_job(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The runner's own arm: the sweep runs minutes later on a worker thread.

    WARNING and skip, not a failed job — the same shape the unusable-store guard
    above it uses, and for the same reason: the move phase has already relocated
    real files, and failing here would cost the run its ``.m3u8`` re-export tail
    for a fault about the Trash.
    """
    from app.reorganize_jobs.registry import ReorganizeRegistry
    from app.reorganize_jobs.runner import _sweep_orphans

    music = tmp_path / "music"
    (music / "Old Artist").mkdir(parents=True)
    (music / "Old Artist" / "poster.jpg").write_bytes(b"\x00")
    beets_dir = beets_dir_for(tmp_path)
    handle = make_test_handle(build_library(str(beets_dir / "library.db"), str(music)), beets_dir)
    trash = music / ".trash"
    origins = tmp_path / "records"  # out of the library, or the store's own rule fires
    origins.mkdir(parents=True)
    trash.symlink_to(music)  # the Trash root now IS the library
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")

    with caplog.at_level(logging.WARNING):
        stopped = _sweep_orphans(
            reg,
            handle,
            scope="library",
            music_dir=music,
            trash_dir=trash,
            trash_origins_dir=origins,
            vacated=[],
            ignore_dirs=(),
            protected_dirs=(),
        )

    assert stopped is False
    assert reg.state().orphans_trashed == 0
    assert (music / "Old Artist" / "poster.jpg").is_file()
    assert any("the store layout is refused" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_the_orphan_sweep_still_moves_a_husk_on_the_allowed_layout(
    tmp_path: Path,
) -> None:
    """The control for the runner arm: without the symlink the husk is swept."""
    from app.reorganize_jobs.registry import ReorganizeRegistry
    from app.reorganize_jobs.runner import _sweep_orphans

    music = tmp_path / "music"
    (music / "Old Artist").mkdir(parents=True)
    (music / "Old Artist" / "poster.jpg").write_bytes(b"\x00")
    beets_dir = beets_dir_for(tmp_path)
    handle = make_test_handle(build_library(str(beets_dir / "library.db"), str(music)), beets_dir)
    trash = music / ".trash"
    origins = tmp_path / "records"  # out of the library, or the store's own rule fires
    origins.mkdir(parents=True)
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")

    stopped = _sweep_orphans(
        reg,
        handle,
        scope="library",
        music_dir=music,
        trash_dir=trash,
        trash_origins_dir=origins,
        vacated=[],
        ignore_dirs=(),
        protected_dirs=(),
    )

    assert stopped is False
    assert reg.state().orphans_trashed == 1
    assert not (music / "Old Artist").exists()


def test_a_trash_dir_that_stops_resolving_is_a_503_not_a_500(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink loop at the Trash path: ``resolve()`` raises ``RuntimeError``.

    ``resolve_trash_dir`` raises it on every request, outside every
    ``except StoreLayoutError`` the app had, so the route answered 500. Same
    refusal, same tier, one message shape.
    """
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    monkeypatch.setattr("app.config.settings.trash_dir", str(loop))

    r = client.delete("/api/trash/all")

    assert r.status_code == 503, r.text
    assert "MUSICDROP_TRASH_DIR could not be resolved" in r.json()["detail"]


def test_the_duplicates_resolve_route_refuses_after_the_swap(
    client: TestClient, beets_library: LibraryHandle, trash_inside_music: Path
) -> None:
    """Duplicates move the losing copies into Trash, so they take the same pair.

    The request body names ids that need not exist: the layout check runs before
    the group is looked up, which is the ordering being pinned.
    """
    app: Any = client.app
    assert app.state.beets_library is beets_library
    _point_trash_at_the_library(trash_inside_music, Path(beets_library.lib.directory.decode()))

    r = client.post(
        "/api/duplicates/resolve",
        json={"mode": "strict", "keep_album_id": 1, "remove_album_ids": [2]},
    )

    assert r.status_code == 503, r.text
    assert "No copies have been moved." in r.json()["detail"]


# --------------------------------------------------------------------------
# EVERY route that takes the pair, on one fixture.
# --------------------------------------------------------------------------

#: ``(method, path, body-or-None)`` for every route that resolves the Trash /
#: origin-store pair. Written as one list because the gap this closes was not a
#: missing assertion but a missing ROW: six of these had no test that booted a
#: good layout, swapped the Trash and asked them, and the pair that included both
#: Reorganize starts is where the wedged-job-slot defect lived. A new route that
#: takes the pair and forgets the check belongs here on the day it is written.
_ROUTES_THAT_TAKE_THE_PAIR: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/api/trash", None),
    ("POST", "/api/trash/restore", {"folder": "Old Entry"}),
    ("DELETE", "/api/trash?folder=Old%20Entry", None),
    ("DELETE", "/api/trash/all", None),
    ("DELETE", "/api/albums/1", None),
    ("DELETE", "/api/artists?name=Artist%20A", None),
    (
        "POST",
        "/api/duplicates/resolve",
        {"mode": "strict", "keep_album_id": 1, "remove_album_ids": [2]},
    ),
    (
        "POST",
        "/api/duplicates/resolve-all",
        {"mode": "strict", "groups": [{"keep_album_id": 1, "remove_album_ids": [2]}]},
    ),
    ("GET", "/api/reorganize/preview", None),
    ("GET", "/api/albums/1/reorganize/preview", None),
    ("POST", "/api/reorganize", None),
    ("POST", "/api/albums/1/reorganize", None),
]


@pytest.fixture
def no_background_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive a started reorganize to ``done`` synchronously.

    The real ``start_backfill`` spawns a daemon thread that calls into beets;
    left running past teardown it reads a torn-down config and, measured here,
    segfaulted the interpreter inside SQLite when the library closed under it.
    Same seam ``test_reorganize_api._fake_sweep`` uses, and it sits BELOW what
    these tests are about: the layout check runs before ``start_backfill`` on
    both routes now, so a fake sweep cannot make a refused start look accepted.
    """
    import app.api.reorganize as reorganize_api

    def fake_start_backfill(reg: Any, handle: Any, **kwargs: Any) -> None:
        reg.set_total(0)
        reg.finish("done")

    monkeypatch.setattr(reorganize_api, "start_backfill", fake_start_backfill)


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES_THAT_TAKE_THE_PAIR)
def test_every_route_that_takes_the_pair_refuses_after_the_swap(
    client: TestClient,
    beets_library: LibraryHandle,
    trash_inside_music: Path,
    no_background_sweep: None,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """503 with the refusal's own sentence, and the library still on disk.

    The sentence and not only the tier: several of these routes have a second
    503 (an unmounted share, an unusable origin store), so a status-only
    assertion is satisfied by a guard that is not this one.
    """
    music = Path(beets_library.lib.directory.decode())
    _point_trash_at_the_library(trash_inside_music, music)

    r = client.request(method, path, json=body)

    assert r.status_code == 503, (path, r.status_code, r.text)
    assert "The Trash directory is the music library" in r.json()["detail"], (path, r.text)
    assert (music / "Artist A" / "Album" / "01.flac").is_file(), path


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES_THAT_TAKE_THE_PAIR)
def test_every_route_that_takes_the_pair_is_reachable_on_the_allowed_layout(
    client: TestClient,
    beets_library: LibraryHandle,
    trash_inside_music: Path,
    no_background_sweep: None,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """The control for the sweep above, one row per row.

    Not "answers 200": ``DELETE /api/trash?folder=`` on an entry another case
    already removed is a legitimate 404, and the duplicates routes can 404 on
    ids this fixture does not have. What is asserted is that the layout gate is
    not what answered — no 503 and no layout sentence anywhere in the body.
    """
    r = client.request(method, path, json=body)

    assert r.status_code != 503, (path, r.text)
    assert "The Trash directory" not in r.text, (path, r.text)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/trash/restore", {"folder": "Old Entry"}),
        ("DELETE", "/api/trash?folder=Old%20Entry", None),
    ],
)
def test_the_two_child_routes_check_the_layout_exactly_once(
    client: TestClient,
    beets_library: LibraryHandle,
    trash_inside_music: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """One check per request, so the pair validated is the pair acted on.

    Both of these resolved the trashed child from a FIRST check and then ran a
    SECOND one before touching it — two instants where the route can only
    honestly claim one, and the ``rmtree`` target came from the earlier of them.
    Counting is what pins it: the two pairs are equal today (``resolve_trash_dir``
    returns the same value across an Apply), so no assertion on the RESULT can
    tell one check from two.

    It is also what the duplicate cost was: a bare ``checked_store_dirs`` was
    measured at 130 stats over the rule's 36 rows, and these two paid it twice.
    """
    import app.api.trash as trash_api

    calls = 0
    real_store = trash_api._store

    def counting_store(app: Any) -> Any:
        nonlocal calls
        calls += 1
        return real_store(app)

    monkeypatch.setattr(trash_api, "_store", counting_store)

    r = client.request(method, path, json=body)

    assert r.status_code == 200, (path, r.text)
    assert calls == 1, (path, calls)


@pytest.mark.parametrize("path", ["/api/reorganize", "/api/albums/1/reorganize"])
def test_a_refused_reorganize_start_does_not_claim_the_job_slot(
    client: TestClient,
    beets_library: LibraryHandle,
    trash_inside_music: Path,
    no_background_sweep: None,
    path: str,
) -> None:
    """The 503 must leave the single job slot free.

    ``reg.start`` used to run before the layout check, and nothing after the
    claim calls ``reg.fail`` — the only guard that does is ``spawn_worker``,
    which the 503 skips. Measured: the status stayed ``running`` with no worker,
    ``stop`` was a no-op, ``dismiss`` refuses a running job, every
    library-mutating write answered 409, and repairing the layout did not clear
    it — a restart was the only way out.

    Three assertions, because the first alone would pass on a registry that had
    claimed and released: idle after the refusal, every other library write
    still accepted, and a start on the healed layout taken.
    """
    music = Path(beets_library.lib.directory.decode())
    _point_trash_at_the_library(trash_inside_music, music)

    assert client.post(path).status_code == 503

    assert client.get("/api/reorganize/status").json()["phase"] == "idle"
    trash_inside_music.unlink()
    trash_inside_music.mkdir()
    # The slot is takeable, and so is every other library write. Order matters:
    # the delete removes the album this path names.
    assert client.post(path).status_code == 200, client.get("/api/reorganize/status").text
    assert client.delete("/api/albums/1").status_code != 409
