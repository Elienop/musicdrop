"""What ``POST /api/import`` refuses, and what it maps, before a job exists.

Three defects measured on this tree, each through the real route and the real
``BeetsImportRunner``:

* a NUL in the posted path reached ``os.path.realpath`` inside the copy-mode
  guard and 500'd; under the default operation it started a job that failed with
  ``lstat: embedded null character in path``;
* a folder whose name is not valid UTF-8 is DISPLAYED with U+FFFD, and posting
  that string back named a path that does not exist, so the import ended
  ``done`` having imported nothing;
* with the music root missing or a bare mountpoint, beets re-created the root
  and filed the album onto it — and under ``move`` the download was emptied.

The library-root arm of the last one is also what the two background producers
now WAIT on, so the bank row is never claimed and the inbox folder is never
marked.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import anyio
import beets.importer.tasks as beets_tasks
import httpx
import pytest
from beets import config
from beets.autotag import AlbumInfo, AlbumMatch, TrackInfo
from beets.autotag.distance import distance
from beets.autotag.match import Proposal, assign_items
from beets.autotag.match import Recommendation as BeetsRec
from beets.library import Library
from fastapi.testclient import TestClient

from app.auth.session import SESSION_COOKIE_NAME
from app.import_jobs.registry import ImportJobRegistry, reset_registry
from app.main import app
from app.wire import display_path
from tests.conftest import (
    beets_dir_for,
    build_library,
    make_test_handle,
    session_cookie_value,
)

_ARTIST = "Radiohead"
_ALBUM = "OK Computer"
#: A folder name carrying one byte UTF-8 cannot decode. It reaches the wire as
#: ``"Caf�"`` and that is the only spelling a browser can post back.
_BAD_NAME = b"Caf\xe9"


def _canned_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin beets' lookup to one STRONG canned match, so a run auto-applies."""

    def fake_tag_album(items: Any, search_ids: Any = None) -> tuple[str, str, Proposal]:
        item_list = list(items)
        tracks = [
            TrackInfo(title=f"Airbag {i}", track_id=f"t{i}", index=i, length=1.0)
            for i in range(1, len(item_list) + 1)
        ]
        info = AlbumInfo(
            tracks=tracks,
            album=_ALBUM,
            artist=_ARTIST,
            album_id="mb-okc",
            data_source="MusicBrainz",
            data_url="https://mb/okc",
            year=1997,
            va=False,
        )
        pairs, extra_items, extra_tracks = assign_items(item_list, info.tracks)
        match = AlbumMatch(
            distance(item_list, info, pairs), info, dict(pairs), extra_items, extra_tracks
        )
        return (_ARTIST, _ALBUM, Proposal([match], BeetsRec.strong))

    def fake_tag_item(item: Any, search_ids: Any = None) -> Proposal:
        return Proposal([], BeetsRec.none)

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)
    monkeypatch.setattr(beets_tasks, "tag_item", fake_tag_item)


def _album_folder(parent: Path, raw_name: bytes) -> Path:
    """Two tagged FLACs in a folder whose NAME is ``raw_name`` (bytes)."""
    from mediafile import MediaFile

    sample = Path(__file__).parent / "fixtures" / "silent.flac"
    folder = Path(os.fsdecode(os.fsencode(str(parent)) + b"/" + raw_name))
    folder.mkdir(parents=True)
    for i in (1, 2):
        dst = folder / f"{i:02d} Track {i}.flac"
        shutil.copyfile(sample, dst)
        mf = MediaFile(str(dst))
        mf.artist = _ARTIST
        mf.albumartist = _ARTIST
        mf.album = _ALBUM
        mf.title = f"Airbag {i}"
        mf.track = i
        mf.save()
    return folder


def _real_registry(tmp_path: Path) -> tuple[ImportJobRegistry, Library]:
    """A registry on the REAL runner, over a real library under ``tmp_path``."""
    config["statefile"] = str(tmp_path / "state.pickle")
    music = tmp_path / "music"
    music.mkdir(exist_ok=True)
    (music / ".keep").write_bytes(b"")  # a real root has entries
    lib = build_library(str(tmp_path / "library.db"), str(music))
    reg = reset_registry(runner=None)
    reg.attach_library(lib)
    return reg, lib


def _bank_status(bank_dir: Path, item_id: str) -> str:
    """The row's status, asserting the row is still there."""
    from app.bank import store as bank_store

    row = bank_store.get_item(bank_dir, item_id)
    assert row is not None
    return row.status


def _drive(client: TestClient, job_id: str, attempts: int = 600) -> dict[str, Any]:
    for _ in range(attempts):
        state: dict[str, Any] = client.get(f"/api/import/{job_id}").json()
        if state["phase"] in ("done", "failed"):
            return state
        time.sleep(0.01)
    return client.get(f"/api/import/{job_id}").json()  # type: ignore[no-any-return]


# ----- 9: a NUL in the posted path -----


def test_a_nul_in_the_posted_path_is_refused_before_any_job(tmp_path: Path) -> None:
    """Copy mode reached ``os.path.realpath`` and raised ValueError -> 500."""
    _real_registry(tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/import",
        json={"path": "/downloads/a\x00b", "options": {"operation": "copy"}},
    )
    assert resp.status_code == 422
    assert "null" in resp.text.lower()


def test_a_nul_under_the_default_operation_is_refused_too(tmp_path: Path) -> None:
    """The default operation skipped the guard and started a job that failed."""
    _real_registry(tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/import", json={"path": "/downloads/a\x00b"})
    assert resp.status_code == 422
    assert "null" in resp.text.lower()


def test_an_ordinary_path_still_starts(tmp_path: Path) -> None:
    """The control: the refusal is about the NUL, not about every path."""
    _real_registry(tmp_path)
    client = TestClient(app)
    source = tmp_path / "dl"
    source.mkdir()  # past the source-existence guard
    resp = client.post("/api/import", json={"path": str(source)})
    assert resp.status_code == 202
    # Drained before returning: a worker thread outliving the test reads beets'
    # config after the autouse reset and raises confuse.NotFoundError elsewhere.
    _drive(client, resp.json()["job_id"])


# ----- the posted path is bounded (the resolver is quadratic, on the event loop) -----


def test_a_path_longer_than_PATH_MAX_is_refused(tmp_path: Path) -> None:
    """4097 characters can name no folder, and the resolver is quadratic.

    Measured unbounded by the security seat: 80 KB of path stalled the whole
    API for 210 s, on the event loop, uncancellable. At the bound the densest
    path (2048 placeholder components, U+FFFD being one character) costs ~331 ms
    (re-measured 2026-09-19; the resolve now runs off the loop, which leaves
    175-334 ms of stall — see
    ``test_the_posted_path_resolve_runs_off_the_event_loop``).
    """
    _real_registry(tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/import", json={"path": "/" + "x" * 4096})
    assert resp.status_code == 422
    assert "4096" in resp.text


def _directory_whose_path_is(root: Path, length: int) -> Path:
    """A directory that EXISTS and whose absolute path is ``length`` characters.

    Components of 100 characters, well inside NAME_MAX (255), and the whole
    path inside PATH_MAX (4096) so every ``mkdir`` along the way succeeds.
    """
    deficit = length - len(str(root))
    assert deficit >= 2, "the root already reaches that length"
    path = root
    while deficit > 102:
        path = path / ("x" * 100)
        deficit -= 101
    path = path / ("x" * (deficit - 1))
    assert len(str(path)) == length
    path.mkdir(parents=True)
    return path


def test_a_path_at_PATH_MAX_still_starts(tmp_path: Path) -> None:
    """The control: the bound refuses nothing the feature can do.

    A real directory 4095 characters deep, so this proves the start, not merely
    that the length refusal was skipped.
    """
    _real_registry(tmp_path)
    client = TestClient(app)
    source = _directory_whose_path_is(tmp_path, 4095)
    resp = client.post("/api/import", json={"path": str(source)})
    assert resp.status_code == 202, resp.text
    _drive(client, resp.json()["job_id"])


# ----- 11: a displayed path round-trips -----


def test_a_displayed_folder_posts_back_and_imports_that_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The string the app served is the only one a browser can send back."""
    _canned_lookup(monkeypatch)
    _reg, lib = _real_registry(tmp_path)
    folder = _album_folder(tmp_path / "downloads", _BAD_NAME)
    displayed = str(tmp_path / "downloads" / "Caf�")
    assert not Path(displayed).exists()  # the literal path is not the folder

    client = TestClient(app)
    resp = client.post(
        "/api/import",
        json={"path": displayed, "options": {"operation": "copy", "unattended": True}},
    )
    assert resp.status_code == 202
    state = _drive(client, resp.json()["job_id"])
    assert state["phase"] == "done", state
    assert len(list(lib.albums())) == 1
    assert folder.exists()


def test_two_folders_that_display_alike_are_refused_not_guessed(tmp_path: Path) -> None:
    """Picking one of two real folders to import is worse than refusing."""
    _real_registry(tmp_path)
    downloads = tmp_path / "downloads"
    _album_folder(downloads, b"Caf\xe9")
    _album_folder(downloads, b"Caf\xea")
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/import", json={"path": str(downloads / "Caf�")})
    assert resp.status_code == 409
    assert "not valid UTF-8" in resp.json()["detail"]


def test_a_path_with_no_placeholder_is_not_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary path costs no directory listing.

    ``resolve_posted_path`` returns its argument untouched when there is no
    placeholder; a scan here would be per-import waste on every ordinary start.
    """
    _real_registry(tmp_path)
    scans: list[str] = []
    real_scandir = os.scandir

    def counting_scandir(path: Any = ".") -> Any:
        scans.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", counting_scandir)
    client = TestClient(app)
    plain = tmp_path / "downloads" / "plain"
    plain.mkdir(parents=True)
    ordinary = str(plain)
    resp = client.post("/api/import", json={"path": ordinary})
    assert resp.status_code == 202
    assert [s for s in scans if s.startswith(str(tmp_path / "downloads"))] == []
    _drive(client, resp.json()["job_id"])


@pytest.mark.parametrize("spelling", ["/music/incoming/", "/music//incoming", "/music/./incoming"])
def test_a_path_with_no_placeholder_reaches_beets_byte_identical(spelling: str) -> None:
    """Untouched means the exact string, not an equivalent one.

    Routing every start through the resolver's pathlib join would normalise the
    spelling the user typed before beets ever saw it.
    """
    from app.wire import resolve_posted_path

    assert resolve_posted_path(spelling) == spelling


def test_the_folder_the_album_page_serves_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 5's notice names a folder and says to import it again.

    The album detail serves the folder through the wire scrub, so an undecodable
    download folder reaches the reader as U+FFFD. That exact string is what the
    reader retypes.
    """
    from beets.library import Item

    from app.api.albums import get_library
    from app.beets.library import _require_id
    from tests.conftest import beets_dir_for, make_test_handle

    _canned_lookup(monkeypatch)
    _reg, lib = _real_registry(tmp_path)
    folder = _album_folder(tmp_path / "downloads", _BAD_NAME)
    # Deliberately NOT the album the canned lookup returns: the round-tripped
    # import must add a second album, not collide with this row.
    item = Item(album="Half Placed", albumartist="Someone", title="T1", track=1)
    item.path = os.fsencode(str(folder / "01 Track 1.flac"))
    album_id = _require_id(lib.add_album([item]).id)

    handle = make_test_handle(lib, beets_dir_for(tmp_path))
    app.dependency_overrides[get_library] = lambda: handle
    try:
        client = TestClient(app)
        served = client.get(f"/api/albums/{album_id}").json()["outside_library"]["folder"]
        assert served == str(tmp_path / "downloads" / "Caf�")
        resp = client.post(
            "/api/import",
            json={"path": served, "options": {"operation": "copy", "unattended": True}},
        )
        assert resp.status_code == 202
        state = _drive(client, resp.json()["job_id"])
        assert state["phase"] == "done", state
    finally:
        app.dependency_overrides.clear()
    # ONE album, not two: beets' own ``remove_replaced`` drops a library row whose
    # path an imported file already held, so the half-placed row is replaced by the
    # album the round-tripped import tagged. That it is THAT album is the proof the
    # displayed string reached the real folder.
    albums = list(lib.albums())
    assert [(a.albumartist, a.album) for a in albums] == [(_ARTIST, _ALBUM)]


# ----- the library root at import start -----


# ----- the shared display-path resolver: what each of its three routes costs -----


#: What ``_BAD_NAME`` looks like once it has crossed the wire.
_BAD_DISPLAY = "Caf\ufffd"


def test_the_posted_path_resolve_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the 4096-character cap the resolve costs ~331 ms, mostly pure Python.

    The cost is ``pathlib``/``posixpath.join`` work holding the GIL, not the
    filesystem: ``os.scandir`` profiled at ~0.5% of it. Running it in a
    threadpool therefore REDUCES the stall rather than removing it — the loop
    still serves nobody for 175-334 ms across five runs (security seat, measured
    2026-09-19). On the loop the whole ~331 ms is time in which no other request
    is served: the health check, the SSE feed, every other route. Pinned by
    asking the resolve itself whether a loop is running where it executes.
    """
    import asyncio

    _real_registry(tmp_path)
    folder = _album_folder(tmp_path / "downloads", b"okc")
    from app.wire import resolve_posted_path as real

    on_loop: list[bool] = []

    def spy(path: str) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_loop.append(False)
        else:
            on_loop.append(True)
        return str(real(path))

    monkeypatch.setattr("app.api.import_.resolve_posted_path", spy)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/import", json={"path": str(folder)})

    assert resp.status_code == 202, resp.text
    assert on_loop == [False]


def test_a_posted_path_with_a_dotdot_segment_is_not_mapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The amplifier, through the route: ``..`` re-scans one directory per repeat.

    Measured at the cap over a 20 000-entry directory: 18.8 s of whole-API
    freeze, paid before the refusal the route eventually returns. Returned
    unchanged means no directory was read — a mapped path is a scanned one.
    """
    _real_registry(tmp_path)
    downloads = tmp_path / "downloads"
    _album_folder(downloads, _BAD_NAME)  # the real folder the amplifier matches
    amplifier = str(downloads) + f"/{_BAD_DISPLAY}/.." * 3

    from app.wire import resolve_posted_path as real

    seen: list[tuple[str, str]] = []

    def spy(path: str) -> str:
        out = str(real(path))
        seen.append((path, out))
        return out

    monkeypatch.setattr("app.api.import_.resolve_posted_path", spy)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/import", json={"path": amplifier})

    assert seen == [(amplifier, amplifier)]
    assert resp.status_code != 500, resp.text


def test_a_posted_path_without_a_dotdot_segment_is_still_mapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the refusal must not cost the route its ordinary mapping."""
    _real_registry(tmp_path)
    downloads = tmp_path / "downloads"
    real_folder = _album_folder(downloads, _BAD_NAME)
    displayed = str(downloads) + f"/{_BAD_DISPLAY}"

    from app.wire import resolve_posted_path as real

    seen: list[tuple[str, str]] = []

    def spy(path: str) -> str:
        out = str(real(path))
        seen.append((path, out))
        return out

    monkeypatch.setattr("app.api.import_.resolve_posted_path", spy)
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/api/import", json={"path": displayed})

    assert seen == [(displayed, str(real_folder))]


@pytest.mark.parametrize(
    ("method", "url", "field", "in_query"),
    [
        ("post", "/api/acquisition/inbox/items/import", "name", False),
        ("post", "/api/trash/restore", "folder", False),
        ("delete", "/api/trash", "folder", True),
    ],
)
def test_the_sibling_name_fields_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    method: str,
    url: str,
    field: str,
    in_query: bool,
) -> None:
    """All three take a RELATIVE PATH, and unbounded each re-scanned a directory
    per component.

    Not one entry name: ``resolve_display_path`` iterates ``Path(rel).parts`` and
    ``resolve_trash_child`` splits on ``/``, so a multi-disc entry is genuinely
    ``Album/Disc 1`` and 255 characters admit 128 components — ``"x/" * 127 + "x"``
    is 255 characters and resolves to 128 components below the base (counted
    through ``resolve_display_path``, 2026-09-19). The cap was reasoned about as a
    NAME cap (255 is NAME_MAX) and applied to a PATH.

    Measured on a 20 000-entry directory before the bound: 75.9 s from a 64 KB
    body, paid before the 404 the route eventually returns. At 255 characters
    the same worst case costs 3.5 ms. The DELETE route carried no bound at all
    until 2026-09-19: the security seat measured 3600 components at 2091 ms of
    request time and 2083 ms of event-loop stall, against a 0.39 ms health
    baseline. A query parameter needs its own case shape, which is why this is
    parametrised over the SHAPE as well as the route.

    The library handle is attached because the CONTROL needs a real answer.
    Without it the two Trash routes 500 at 255 characters (``'State' object has
    no attribute beets_library``, measured 2026-09-19, code seat F5), so a bare
    ``!= 422`` passed on a fixture that had failed before reaching the length
    check. Attached, all three answer 404 — the route looked, and did not
    complain about length.
    """
    _, lib = _real_registry(tmp_path)
    monkeypatch.setattr(
        app.state, "beets_library", make_test_handle(lib, beets_dir_for(tmp_path)), raising=False
    )
    client = TestClient(app, raise_server_exceptions=False)

    def call(value: str) -> Any:
        if in_query:
            return client.request(method, url, params={field: value})
        return client.request(method, url, json={field: value})

    over = call("x" * 256)
    assert over.status_code == 422, over.text
    assert over.json()["detail"][0]["type"] == "string_too_long"

    # The control: the longest name a listing can emit is NOT refused, and
    # the route reaches its own lookup to say so.
    at_bound = call("x" * 255)
    assert at_bound.status_code == 404, at_bound.text


def _seed_a_library_row(lib: Library) -> None:
    """One item row naming a file under the music root — a library with content."""
    from beets.library import Item

    item = Item(album="The Wall", albumartist="Pink Floyd", title="T1", track=1)
    item.path = os.fsencode(os.path.join(os.fsdecode(lib.directory), "Pink Floyd/The Wall/01.flac"))
    lib.add_album([item])


def _drop_root(lib: Library, *, bare: bool, rows: bool = True) -> Path:
    """Make the music root look like a dropped share. Returns the root.

    ``rows`` seeds one item row first: an EMPTY root is a dropped share only
    when the library lists files. ``rows=False`` is a new install's bind mount.
    """
    if rows:
        _seed_a_library_row(lib)
    root = Path(os.fsdecode(lib.directory))
    shutil.rmtree(root)
    if bare:
        root.mkdir()  # the mountpoint survives a dropped mount, empty
    return root


@pytest.mark.parametrize(
    ("bare", "rows"),
    [
        (False, True),  # the root is gone and the library lists files
        (False, False),  # gone with no rows either: MISSING refuses regardless
        (True, True),  # the bare mountpoint of a dropped share
    ],
)
def test_an_import_does_not_start_while_the_library_root_is_unavailable(
    tmp_path: Path, bare: bool, rows: bool
) -> None:
    """Measured without the guard: beets re-created the root and filed the album
    onto it, and a ``move`` emptied the download."""
    _reg, lib = _real_registry(tmp_path)
    folder = _album_folder(tmp_path / "downloads", b"okc")
    before = sorted(p.name for p in folder.iterdir())
    root = _drop_root(lib, bare=bare, rows=rows)
    albums_before = len(list(lib.albums()))

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/import", json={"path": str(folder), "options": {"operation": "move"}})
    assert resp.status_code == 503
    assert "share mounted" in resp.json()["detail"]
    assert sorted(p.name for p in folder.iterdir()) == before
    assert list(root.iterdir()) == [] if bare else not root.exists()
    assert len(list(lib.albums())) == albums_before


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits this test sets")
def test_an_unreadable_root_refuses_even_with_no_rows(tmp_path: Path) -> None:
    """Invariant 3's other half: only the EMPTY arm is forgiven, never this one."""
    _reg, lib = _real_registry(tmp_path)
    folder = _album_folder(tmp_path / "downloads", b"okc")
    root = Path(os.fsdecode(lib.directory))
    root.chmod(0o000)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/import", json={"path": str(folder)})
    finally:
        root.chmod(0o755)
    assert resp.status_code == 503
    assert "unreadable" in resp.json()["detail"]
    assert len(list(lib.albums())) == 0


# ----- C1: a fresh install's empty music folder must still import -----


def test_a_fresh_install_with_an_empty_music_folder_can_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker creates ``/music`` empty and the app never creates it.

    Measured before the fix: ``POST /api/import`` -> 503 "Library folder is
    empty. Is the music share mounted?" on every new install.
    """
    _canned_lookup(monkeypatch)
    _reg, lib = _real_registry(tmp_path)
    (Path(os.fsdecode(lib.directory)) / ".keep").unlink()  # the bare bind mount
    folder = _album_folder(tmp_path / "downloads", b"okc")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/import", json={"path": str(folder)})
    assert resp.status_code == 202, resp.text
    state = _drive(client, resp.json()["job_id"])
    assert state["phase"] == "done", state
    assert len(list(lib.albums())) == 1


def test_the_forgiven_arm_warns_which_root_it_is_about_to_file_into(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The hole is keyed on "``items`` holds no row", which an operator can reset.

    A ``library:`` edited to a new path, a database restored from before any
    import, or a repointed ``BEETSDIR`` each reach the forgiven arm on a
    configured install, so the one record that makes a shadowed-mountpoint import
    diagnosable afterwards is this line. The control below is the same route with
    one row in ``items``: 503, no record.
    """
    _canned_lookup(monkeypatch)
    _reg, lib = _real_registry(tmp_path)
    root = Path(os.fsdecode(lib.directory))
    root.joinpath(".keep").unlink()  # the bare bind mount
    folder = _album_folder(tmp_path / "downloads", b"okc")

    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        resp = client.post("/api/import", json={"path": str(folder)})
        assert resp.status_code == 202, resp.text
        _drive(client, resp.json()["job_id"])

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    filing = [m for m in warnings if "filing this import there" in m]
    assert len(filing) == 1, warnings
    assert str(root) in filing[0]


def test_an_empty_root_with_rows_files_nothing_and_logs_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The control for the record above: the refusing arm writes no such line."""
    _reg, lib = _real_registry(tmp_path)
    root = Path(os.fsdecode(lib.directory))
    root.joinpath(".keep").unlink()
    _seed_a_library_row(lib)
    folder = _album_folder(tmp_path / "downloads", b"okc")

    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        resp = client.post("/api/import", json={"path": str(folder)})

    assert resp.status_code == 503
    assert [m for m in (r.getMessage() for r in caplog.records) if "filing this import" in m] == []


def test_a_fresh_installs_empty_music_folder_leaves_the_gate_open(tmp_path: Path) -> None:
    """The drains must not wait for ever on a new install either."""
    from app.import_jobs.gates import import_gate_clear

    reg, lib = _real_registry(tmp_path)
    (Path(os.fsdecode(lib.directory)) / ".keep").unlink()
    assert import_gate_clear(reg, None) is True


def test_an_empty_root_with_rows_is_still_a_dropped_share(tmp_path: Path) -> None:
    """The control for C1: one row is the difference between the two states."""
    from app.import_jobs.gates import import_gate_clear

    reg, lib = _real_registry(tmp_path)
    root = Path(os.fsdecode(lib.directory))
    root.joinpath(".keep").unlink()
    _seed_a_library_row(lib)
    folder = _album_folder(tmp_path / "downloads", b"okc")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/import", json={"path": str(folder)})
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Library folder is empty. Is the music share mounted?"
    assert import_gate_clear(reg, None) is False


def test_a_row_naming_no_file_is_still_a_dropped_share(tmp_path: Path) -> None:
    """The forgiveness asks the items table, not a random one-album sample.

    Measured with the sample: one pathless row beside real albums forgave a
    dropped share on 9 of 400 polls, and a single-row library on 400 of 400 —
    the gate opened and the queued import filed onto the bare mountpoint. The
    question is now ``SELECT 1 FROM items LIMIT 1``, so 200 polls agree.
    """
    from beets.library import Item

    from app.import_jobs.gates import import_gate_clear

    reg, lib = _real_registry(tmp_path)
    _seed_a_library_row(lib)
    blank = Item(album="Damaged", albumartist="Nobody", title="T1", track=1)
    blank.path = b""
    lib.add_album([blank])
    (Path(os.fsdecode(lib.directory)) / ".keep").unlink()  # the share dropped
    folder = _album_folder(tmp_path / "downloads", b"okc")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/import", json={"path": str(folder)})
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Library folder is empty. Is the music share mounted?"
    assert [import_gate_clear(reg, None) for _ in range(200)] == [False] * 200


def test_album_rows_with_no_tracks_are_still_a_fresh_install(tmp_path: Path) -> None:
    """The control for the row above: ALBUM rows alone do not name a file.

    beets writes an album row with its tracks, so this is a partially-cleared
    database, not an ordinary one — it is here because the predicate reads
    ``items`` and a reader should see which table decides.
    """
    from beets.library import Album

    from app.import_jobs.gates import import_gate_clear

    reg, lib = _real_registry(tmp_path)
    lib.add(Album(album="Ghost", albumartist="Nobody"))
    (Path(os.fsdecode(lib.directory)) / ".keep").unlink()
    assert [import_gate_clear(reg, None) for _ in range(20)] == [True] * 20


def test_the_strict_predicates_refuse_an_empty_root_with_an_empty_database(
    tmp_path: Path,
) -> None:
    """Invariant 4, half one: the predicates themselves do not read the row count.

    An empty root refuses ``require_library_root`` and ``require_library_present``
    on exactly the fresh-install shape the import side forgives. Which callers ask
    them is the twin below.
    """
    from app.beets.library import (
        LibraryRootUnavailableError,
        require_library_present,
        require_library_root,
    )

    _reg, lib = _real_registry(tmp_path)
    (Path(os.fsdecode(lib.directory)) / ".keep").unlink()
    assert len(list(lib.items())) == 0  # the fresh-install shape the import side forgives
    with pytest.raises(LibraryRootUnavailableError, match="is empty"):
        require_library_root(lib)
    with pytest.raises(LibraryRootUnavailableError, match="is empty"):
        require_library_present(lib)


def test_only_the_import_job_gates_reach_the_forgiving_predicate() -> None:
    """Invariant 4, half two: the exemption is the IMPORT side's, not the predicate's.

    ``require_importable_library_root`` forgives the empty root when ``items``
    holds no row. Trash, Delete, Restore and disk sync must keep asking the strict
    one, so the forgiving name is read in ``app/import_jobs/`` and nowhere else.
    Counted from the AST rather than by grep: a name in a docstring is not a call.
    """
    import ast

    app_dir = Path(__file__).resolve().parents[1] / "app"
    readers = set()
    for source in sorted(app_dir.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "require_importable_library_root":
                readers.add(source.relative_to(app_dir).as_posix())
            elif isinstance(node, ast.Attribute) and node.attr == "require_importable_library_root":
                readers.add(source.relative_to(app_dir).as_posix())

    assert readers == {"import_jobs/gates.py", "import_jobs/runner.py"}, readers


def test_the_inbox_review_refuses_while_the_library_root_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one-click inbox review answers the same declared status."""
    import app.api.acquisition as acq_api

    _reg, lib = _real_registry(tmp_path)
    inbox = tmp_path / "inbox"
    folder = _album_folder(inbox, b"okc")
    # The settle window is 60s by default; the folder is settled for this test.
    monkeypatch.setattr(acq_api, "settled_folders", lambda *a, **k: [folder])
    monkeypatch.setattr(acq_api, "count_pending", lambda _d: 1)
    _drop_root(lib, bare=True)
    monkeypatch.setattr(app.state, "inbox_dir", inbox, raising=False)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/acquisition/review-inbox")
    assert resp.status_code == 503
    assert "share mounted" in resp.json()["detail"]


def test_the_per_item_inbox_import_refuses_while_the_library_root_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sibling route's 503 arm, which no test reached.

    Measured by the code seat: narrowing this ``except`` back survived its own
    31 tests, and without the arm the refusal is a plain ``Exception`` the route
    turns into a 500.
    """
    _reg, lib = _real_registry(tmp_path)
    inbox = tmp_path / "inbox"
    folder = _album_folder(inbox, b"okc")
    _drop_root(lib, bare=True)
    monkeypatch.setattr(app.state, "inbox_dir", inbox, raising=False)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/acquisition/inbox/items/import", json={"name": folder.name})
    assert resp.status_code == 503, resp.text
    assert "share mounted" in resp.json()["detail"]


# ----- 10: the two automatic producers wait instead of burning their work -----


def test_the_gate_is_closed_while_the_library_root_is_unavailable(tmp_path: Path) -> None:
    """The gate both drains already poll answers the root question too."""
    from app.import_jobs.gates import import_gate_clear

    reg, lib = _real_registry(tmp_path)
    assert import_gate_clear(reg, None) is True
    _drop_root(lib, bare=True)
    assert import_gate_clear(reg, None) is False


def test_a_registry_with_no_library_leaves_the_gate_open(tmp_path: Path) -> None:
    """The control: the fake-runner registries every other suite builds."""
    from app.import_jobs.fakes import FakeImportRunner
    from app.import_jobs.gates import import_gate_clear

    assert import_gate_clear(ImportJobRegistry(runner=FakeImportRunner(parked=[])), None) is True


# ----- the gate is on the drains' un-caught path, so it must never raise -----


#: The two shapes "never raises" has to cover: the gate's own filesystem work
#: failing, and a defect inside the predicate it calls. Pinning only ``OSError``
#: let ``except Exception`` -> ``except OSError`` survive (measured).
_FAULTS = [OSError(5, "Input/output error"), AttributeError("'NoneType' has no attribute 'x'")]


def _make_the_root_question_raise(
    monkeypatch: pytest.MonkeyPatch, fault: BaseException | None = None
) -> None:
    """Any unexpected failure of the gate's own filesystem work."""
    from app.import_jobs import gates

    def boom(_library: object) -> None:
        raise fault if fault is not None else OSError(5, "Input/output error")

    monkeypatch.setattr(gates, "require_importable_library_root", boom)


@pytest.mark.parametrize("fault", _FAULTS, ids=lambda f: type(f).__name__)
def test_an_unexpected_raise_inside_the_gate_reads_as_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fault: BaseException,
) -> None:
    """Measured before the fix: the raise escaped and killed the drain thread.

    Also pins the latch: the drains poll this four times a second between them,
    so one failing episode is ONE record, and a later episode says so again.
    The record carries its traceback — the message is fixed text, so a
    ``.error`` call would leave the operator nothing that names the defect.
    """
    import logging

    from app.import_jobs import gates

    reg, _lib = _real_registry(tmp_path)
    gates._gate_fault.clear()
    assert gates.import_gate_clear(reg, None) is True  # control: healthy root, gate open
    with caplog.at_level(logging.INFO):
        with monkeypatch.context() as broken:
            _make_the_root_question_raise(broken, fault)
            for _ in range(5):
                assert gates.import_gate_clear(reg, None) is False
        assert gates.import_gate_clear(reg, None) is True  # the fault cleared
        with monkeypatch.context() as broken_again:
            _make_the_root_question_raise(broken_again, fault)
            assert gates.import_gate_clear(reg, None) is False
    faults = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(faults) == 2, [r.getMessage() for r in faults]
    assert {r.name for r in faults} == {"uvicorn.error"}
    assert [r.exc_info is not None for r in faults] == [True, True]
    assert [type(r.exc_info[1]).__name__ for r in faults if r.exc_info] == [
        type(fault).__name__
    ] * 2
    # The recovery says so once, like the root latch does — a drain that
    # resumed after a transient defect is not a silent event.
    backs = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(backs) == 1, [r.getMessage() for r in backs]
    assert backs[0].name == "uvicorn.error"
    assert "resume" in backs[0].getMessage()


def test_the_root_question_runs_even_when_another_check_would_close_the_gate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Asked FIRST, so a share that drops while a backfill holds the gate is still
    logged and its recovery still clears the latch."""
    import logging

    from app.import_jobs import gates

    reg, lib = _real_registry(tmp_path)
    gates._root_wait.clear()
    _drop_root(lib, bare=True)

    class _HeldLock:
        def locked(self) -> bool:
            return True

    with caplog.at_level(logging.INFO):
        assert gates.import_gate_clear(reg, _HeldLock()) is False  # type: ignore[arg-type]
    waits = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(waits) == 1, [r.getMessage() for r in waits]
    assert waits[0].name == "uvicorn.error"


def test_a_raising_gate_does_not_kill_the_inbox_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.acquisition.ledger import AcquisitionLedger
    from app.acquisition.queue import AcquisitionQueue

    reg, _lib = _real_registry(tmp_path)
    folder = _album_folder(tmp_path / "inbox", b"okc")
    _make_the_root_question_raise(monkeypatch)
    ledger = AcquisitionLedger(tmp_path / "ledger")
    queue = AcquisitionQueue(
        import_registry=reg, ledger=ledger, poll_interval=0.01, busy_backoff=0.01
    )
    queue.start()
    queue.enqueue(folder)
    try:
        time.sleep(0.4)
        assert queue.status().processed == 0
        assert not ledger.seen(folder)
        alive = [t for t in threading.enumerate() if t.name == "musicdrop-acquisition"]
        assert [t.is_alive() for t in alive] == [True]
    finally:
        queue.stop()


def test_a_raising_gate_does_not_fail_a_bank_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.bank import store as bank_store
    from app.bank.apply_runner import BankApplyRunner
    from app.bank.fingerprint import folder_fingerprint
    from app.models.bank import BankDecision

    reg, _lib = _real_registry(tmp_path)
    folder = _album_folder(tmp_path / "swept", b"okc")
    bank_dir = tmp_path / "bank"
    item = bank_store.create_item(
        bank_dir,
        folder=str(folder),
        source="sweep",
        reason="no_match",
        fingerprint=folder_fingerprint(folder),
    )
    bank_store.decide_item(bank_dir, item.id, BankDecision(action="asis"))
    _make_the_root_question_raise(monkeypatch)

    drain = BankApplyRunner(
        bank_dir=bank_dir,
        import_registry=reg,
        library=lambda: (_ for _ in ()).throw(AssertionError("no library read expected")),
        poll_interval=0.01,
        busy_backoff=0.01,
        idle_poll=0.01,
    )
    drain.start()
    try:
        time.sleep(0.4)
    finally:
        drain.stop()
    assert _bank_status(bank_dir, item.id) == "queued"


def test_a_queued_bank_row_is_not_claimed_while_the_root_is_unavailable(
    tmp_path: Path,
) -> None:
    """No claim, no fingerprint walk, no revert: the row is simply not picked."""
    from app.bank import store as bank_store
    from app.bank.apply_runner import BankApplyRunner
    from app.bank.fingerprint import folder_fingerprint
    from app.models.bank import BankDecision

    reg, lib = _real_registry(tmp_path)
    folder = _album_folder(tmp_path / "swept", b"okc")
    bank_dir = tmp_path / "bank"
    item = bank_store.create_item(
        bank_dir,
        folder=str(folder),
        source="sweep",
        reason="no_match",
        fingerprint=folder_fingerprint(folder),
    )
    bank_store.decide_item(bank_dir, item.id, BankDecision(action="asis"))
    _drop_root(lib, bare=True)

    drain = BankApplyRunner(
        bank_dir=bank_dir,
        import_registry=reg,
        library=lambda: (_ for _ in ()).throw(AssertionError("no library read expected")),
        poll_interval=0.01,
        busy_backoff=0.01,
        idle_poll=0.01,
    )
    drain.start()
    try:
        time.sleep(0.5)
    finally:
        drain.stop()
    assert _bank_status(bank_dir, item.id) == "queued"


def test_the_root_returning_lets_the_queued_bank_row_proceed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No restart: the waiting drain picks the row up once the share is back."""
    from app.bank import store as bank_store
    from app.bank.apply_runner import BankApplyRunner
    from app.bank.fingerprint import folder_fingerprint
    from app.models.bank import BankDecision

    _canned_lookup(monkeypatch)
    reg, lib = _real_registry(tmp_path)
    folder = _album_folder(tmp_path / "swept", b"okc")
    bank_dir = tmp_path / "bank"
    item = bank_store.create_item(
        bank_dir,
        folder=str(folder),
        source="sweep",
        reason="no_match",
        fingerprint=folder_fingerprint(folder),
    )
    bank_store.decide_item(bank_dir, item.id, BankDecision(action="asis"))
    root = _drop_root(lib, bare=True)

    drain = BankApplyRunner(
        bank_dir=bank_dir,
        import_registry=reg,
        library=lambda: (_ for _ in ()).throw(AssertionError("no library read expected")),
        poll_interval=0.01,
        busy_backoff=0.01,
        idle_poll=0.01,
    )
    drain.start()
    try:
        time.sleep(0.2)
        assert _bank_status(bank_dir, item.id) == "queued"
        (root / ".keep").write_bytes(b"")  # the share is back
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if _bank_status(bank_dir, item.id) not in ("queued", "applying"):
                break
            time.sleep(0.02)
    finally:
        drain.stop()
    assert _bank_status(bank_dir, item.id) == "done"


def test_the_inbox_drain_keeps_its_folder_queued_and_stays_alive(tmp_path: Path) -> None:
    """Measured without the guard: the refusal escaped ``_drain`` and the daemon
    thread died, stranding every later download for the process lifetime."""
    from app.acquisition.ledger import AcquisitionLedger
    from app.acquisition.queue import AcquisitionQueue

    reg, lib = _real_registry(tmp_path)
    folder = _album_folder(tmp_path / "inbox", b"okc")
    _drop_root(lib, bare=True)
    ledger = AcquisitionLedger(tmp_path / "ledger")
    queue = AcquisitionQueue(
        import_registry=reg, ledger=ledger, poll_interval=0.01, busy_backoff=0.01
    )
    queue.start()
    queue.enqueue(folder)
    try:
        time.sleep(0.5)
        status = queue.status()
        assert status.processed == 0
        assert status.failed == 0
        assert not ledger.seen(folder)
        alive = [t for t in threading.enumerate() if t.name == "musicdrop-acquisition"]
        assert [t.is_alive() for t in alive] == [True]
    finally:
        queue.stop()


def test_the_inbox_drain_survives_a_folder_that_is_no_longer_there(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The source-missing refusal reaches ``start`` from the drain too.

    Terminal for the drop, not deferred: a requeue would poll a path that is
    gone. Uncaught it would kill this daemon thread and strand every later
    download, which is the defect the share-drop arm beside it was written for.
    """
    caplog.set_level(logging.WARNING, logger="app.acquisition.queue")
    from app.acquisition.ledger import AcquisitionLedger
    from app.acquisition.queue import AcquisitionQueue

    reg, _lib = _real_registry(tmp_path)
    (tmp_path / "inbox").mkdir()
    gone = tmp_path / "inbox" / "gone"  # never created
    ledger = AcquisitionLedger(tmp_path / "ledger")
    queue = AcquisitionQueue(
        import_registry=reg, ledger=ledger, poll_interval=0.01, busy_backoff=0.01
    )
    queue.start()
    queue.enqueue(gone)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and queue.status().processed == 0:
            time.sleep(0.02)
        status = queue.status()
        assert status.processed == 1
        assert status.failed == 1
        assert status.error == "That folder doesn't exist."
        # The RAW entries, not ``seen()``: ``seen`` stats the folder first and
        # answers False for one that is gone whether ``mark`` ran or not, so it
        # cannot see this at all (adding a ``mark`` to the drain arm left the
        # whole suite green — measured 2026-09-20). ``mark`` on a gone folder
        # writes (mtime=0.0, size=0), which the entries DO show.
        assert [e.path for e in ledger.entries()] == []
        # The one durable trace of the drop, since no row is written.
        assert [r.message for r in caplog.records if "no longer there" in r.message] != []
        alive = [t for t in threading.enumerate() if t.name == "musicdrop-acquisition"]
        assert [t.is_alive() for t in alive] == [True]
        assert reg.active_job_id() is None
    finally:
        queue.stop()


def _gate_that_drops_the_root(
    module: Any, lib: Library, monkeypatch: pytest.MonkeyPatch, drops: list[int]
) -> None:
    """Drive the REAL TOCTOU window: the share goes right after the gate opens.

    The wrapper restores the root before each poll, lets the drain's own
    ``import_gate_clear`` answer, then removes it — so ``start`` -> ``validate``
    raises the type the ``except`` arm has to name. Nothing is stubbed.
    """
    real = module.import_gate_clear
    root = Path(os.fsdecode(lib.directory))
    _seed_a_library_row(lib)  # rows: an empty root is a dropped share, not a new install

    def wrapper(*args: Any, **kwargs: Any) -> bool:
        root.mkdir(parents=True, exist_ok=True)
        (root / ".keep").write_bytes(b"")
        answer = bool(real(*args, **kwargs))
        if answer:
            drops.append(1)
            shutil.rmtree(root)
            root.mkdir()
        return answer

    monkeypatch.setattr(module, "import_gate_clear", wrapper)


def test_the_inbox_drain_survives_a_share_that_drops_after_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured: uncaught, the refusal killed the daemon thread outright."""
    import app.acquisition.queue as queue_mod
    from app.acquisition.ledger import AcquisitionLedger

    reg, lib = _real_registry(tmp_path)
    folder = _album_folder(tmp_path / "inbox", b"okc")
    drops: list[int] = []
    _gate_that_drops_the_root(queue_mod, lib, monkeypatch, drops)
    ledger = AcquisitionLedger(tmp_path / "ledger")
    queue = queue_mod.AcquisitionQueue(
        import_registry=reg, ledger=ledger, poll_interval=0.01, busy_backoff=0.01
    )
    queue.start()
    queue.enqueue(folder)
    try:
        time.sleep(0.5)
        assert len(drops) > 1  # the window opened and closed more than once
        assert queue.status().processed == 0
        assert not ledger.seen(folder)
        alive = [t for t in threading.enumerate() if t.name == "musicdrop-acquisition"]
        assert [t.is_alive() for t in alive] == [True]
    finally:
        queue.stop()
    assert sorted(p.name for p in folder.iterdir()) == ["01 Track 1.flac", "02 Track 2.flac"]


def test_a_bank_row_returns_to_queued_when_the_share_drops_after_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured: uncaught, the row failed permanently on a merely-unmounted share."""
    import app.bank.apply_runner as apply_mod
    from app.bank import store as bank_store
    from app.bank.fingerprint import folder_fingerprint
    from app.models.bank import BankDecision

    reg, lib = _real_registry(tmp_path)
    folder = _album_folder(tmp_path / "swept", b"okc")
    bank_dir = tmp_path / "bank"
    item = bank_store.create_item(
        bank_dir,
        folder=str(folder),
        source="sweep",
        reason="no_match",
        fingerprint=folder_fingerprint(folder),
    )
    bank_store.decide_item(bank_dir, item.id, BankDecision(action="asis"))
    drops: list[int] = []
    _gate_that_drops_the_root(apply_mod, lib, monkeypatch, drops)

    drain = apply_mod.BankApplyRunner(
        bank_dir=bank_dir,
        import_registry=reg,
        library=lambda: (_ for _ in ()).throw(AssertionError("no library read expected")),
        poll_interval=0.01,
        busy_backoff=0.01,
        idle_poll=0.01,
    )
    drain.start()
    try:
        time.sleep(0.5)
        assert len(drops) > 1
    finally:
        drain.stop()
    assert _bank_status(bank_dir, item.id) == "queued"


def test_the_wait_is_logged_once_and_its_end_is_logged_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An operator whose inbox stopped draining has to be able to read why."""
    import logging

    from app.import_jobs import gates

    reg, lib = _real_registry(tmp_path)
    gates._root_wait.clear()  # the latch is process-wide; this file already reads privates
    root = _drop_root(lib, bare=True)
    with caplog.at_level(logging.INFO):
        for _ in range(5):
            assert gates.import_gate_clear(reg, None) is False
        (root / ".keep").write_bytes(b"")
        for _ in range(5):
            assert gates.import_gate_clear(reg, None) is True
    starts = [r for r in caplog.records if r.levelno == logging.WARNING]
    ends = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(starts) == 1, [r.getMessage() for r in starts]
    assert len(ends) == 1, [r.getMessage() for r in ends]
    # BOTH on the operator logger: measured under uvicorn's own LOGGING_CONFIG,
    # an app-namespace WARNING prints as a bare untagged line and an INFO is
    # dropped outright, so a record an operator must read cannot live there.
    assert {r.name for r in starts + ends} == {"uvicorn.error"}


# ----- 12: Review all refuses when a folder vanished since the listing -----


def test_review_all_survives_a_folder_that_vanished_since_the_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One settled folder removed between the listing and the start.

    The batch imports what is still there. The source-missing refusal added on
    2026-09-20 deliberately does NOT fire here: it asks "is there nothing to
    import", not "is every member present". The window between the server's own
    ``settled_folders`` call and ``start`` cannot be closed by a stat - the
    folder is as free to vanish after it as before - and beets already answers
    the case, contributing nothing for a toppath whose ``read_item`` returns
    None (``ImportTaskFactory.read_item``, beets/importer/tasks.py:1128 in beets
    2.13.1, the version ``.venv`` runs; :1142 in the 2.12.0 reference checkout)
    while the rest import.
    """
    _canned_lookup(monkeypatch)
    _reg, lib = _real_registry(tmp_path)
    inbox = tmp_path / "inbox"
    gone = _album_folder(inbox, b"gone")
    stays = _album_folder(inbox, b"stays")

    def settle_then_vanish(*args: Any, **kwargs: Any) -> list[Path]:
        """Both folders are settled; one is removed in the window before start."""
        shutil.rmtree(gone)
        return [gone, stays]

    import app.api.acquisition as acq_api

    monkeypatch.setattr(acq_api, "settled_folders", settle_then_vanish)
    monkeypatch.setattr(acq_api, "count_pending", lambda _d: 2)
    monkeypatch.setattr(app.state, "inbox_dir", inbox, raising=False)
    client = TestClient(app)
    resp = client.post("/api/acquisition/review-inbox")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["started"] is True
    state = _drive(client, body["job_id"])
    assert state["phase"] == "done", state
    assert not gone.exists()
    assert len(list(lib.albums())) == 1
    assert not stays.exists() or sorted(p.name for p in stays.iterdir()) == []


def test_review_all_refuses_when_EVERY_settled_folder_vanished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing left to import, so the click is refused rather than run hollow.

    The route arm's only reader: without the mapping this race is a 500, and
    without the refusal it is a job that finishes having filed nothing.
    """
    _canned_lookup(monkeypatch)
    _reg, lib = _real_registry(tmp_path)
    inbox = tmp_path / "inbox"
    first = _album_folder(inbox, b"first")
    second = _album_folder(inbox, b"second")

    def settle_then_vanish(*args: Any, **kwargs: Any) -> list[Path]:
        """Both settle, then both are removed before the start."""
        shutil.rmtree(first)
        shutil.rmtree(second)
        return [first, second]

    import app.api.acquisition as acq_api

    monkeypatch.setattr(acq_api, "settled_folders", settle_then_vanish)
    monkeypatch.setattr(acq_api, "count_pending", lambda _d: 2)
    monkeypatch.setattr(app.state, "inbox_dir", inbox, raising=False)
    client = TestClient(app)
    resp = client.post("/api/acquisition/review-inbox")
    assert resp.status_code == 422, resp.text
    # This route's OWN copy: it hands over folders the browser is never shown,
    # and only refuses when every one of them went. The shared singular sentence
    # would be about a folder the caller typed.
    assert resp.json()["detail"] == "Those folders are no longer there."
    assert list(lib.albums()) == []
    assert _reg.active_job_id() is None


# ----- 13: a source path that is not on disk -----
#
# Measured 2026-09-20: a path typed into the import box was truncated at a space
# (".../stop-test/Courtney" for ".../stop-test/Courtney Barnett"). beets takes a
# missing toppath down the branch a single FILE takes
# (``ImportTaskFactory.paths`` -> ``if not os.path.isdir(util.syspath(self.toppath))``,
# beets/importer/tasks.py:1041 in beets 2.13.1, the version ``.venv`` runs; :1055 in
# the 2.12.0 reference checkout), reads no item, produces zero tasks and ends the
# session normally - so the app created a job, ran it, and said "Import finished
# - 0 albums imported". Nothing refused.

_MISSING = "That folder doesn't exist."


def test_a_source_that_does_not_exist_is_refused_before_any_job(tmp_path: Path) -> None:
    """The reported defect: a truncated path became a job that imported nothing."""
    reg, lib = _real_registry(tmp_path)
    real = _album_folder(tmp_path / "downloads", b"Courtney Barnett")
    truncated = str(real)[: str(real).rindex(" ")]  # what the box was left holding
    assert not os.path.exists(truncated)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/import", json={"path": truncated})

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == _MISSING
    assert "job_id" not in resp.json()
    # No slot was claimed, so there is no job to poll and nothing to resume.
    assert reg.active_job_id() is None
    probe = client.get("/api/imports/active").json()
    assert probe["active"] is False
    assert probe["job_id"] is None
    assert list(lib.albums()) == []


def test_a_source_that_is_a_FILE_is_not_refused_by_the_existence_guard(tmp_path: Path) -> None:
    """The control that makes this guard EXISTENCE and not ``is_dir``.

    beets imports a single file as one track (the same
    ``ImportTaskFactory.paths`` ``isdir`` branch cited above), so
    an ``is_dir`` guard would take away something the engine can do. Mutating
    ``os.path.exists`` to ``os.path.isdir`` must fail this test.
    """
    _real_registry(tmp_path)
    folder = _album_folder(tmp_path / "downloads", b"single")
    one_track = next(p for p in sorted(folder.iterdir()) if p.suffix == ".flac")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/import", json={"path": str(one_track)})

    assert resp.status_code == 202, resp.text
    _drive(client, resp.json()["job_id"])


def test_a_list_refuses_only_when_NO_member_is_there(tmp_path: Path) -> None:
    """``validate`` takes a LIST, and the question it asks is "is there nothing
    to import" - not "is every member present".

    Both directions, because one alone is satisfied by the wrong predicate: a
    list that still holds one folder starts (an ``all`` check would refuse it),
    and a list where every member is gone refuses (no check at all would let it
    run hollow). The PRESENT member is second in the passing case, so a check
    that stops at the first member cannot pass this.
    """
    from app.import_jobs.runner import BeetsImportRunner, SourcePathMissingError

    _reg, lib = _real_registry(tmp_path)
    here = _album_folder(tmp_path / "downloads", b"here")
    gone = tmp_path / "downloads" / "gone"
    other = tmp_path / "downloads" / "other-gone"
    assert not gone.exists()
    assert not other.exists()

    runner = BeetsImportRunner(lib)
    runner.validate([str(gone), str(here)], None)
    with pytest.raises(SourcePathMissingError, match=r"^That folder doesn't exist\.$"):
        runner.validate([str(gone), str(other)], None)


def test_the_per_item_inbox_import_answers_a_folder_that_vanished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route's own ``is_dir`` check leaves a window before ``start``.

    Drives the REAL window: ``is_dir`` answers True, then the folder goes. Left
    unmapped the refusal is a 500.
    """
    _reg, _lib = _real_registry(tmp_path)
    inbox = tmp_path / "inbox"
    folder = _album_folder(inbox, b"vanishes")

    from app.fsutil import is_dir as real_is_dir

    def is_dir_then_remove(path: Path) -> bool:
        answer = bool(real_is_dir(path))
        if answer and Path(path) == folder:
            shutil.rmtree(folder)
        return answer

    monkeypatch.setattr("app.api.acquisition.is_dir", is_dir_then_remove)
    monkeypatch.setattr(app.state, "inbox_dir", inbox, raising=False)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/acquisition/inbox/items/import", json={"name": "vanishes"})

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == _MISSING
    assert _reg.active_job_id() is None


def test_the_unmounted_share_still_reports_ITSELF_not_the_missing_folder(tmp_path: Path) -> None:
    """A better diagnosis wins: with the share gone every inbox folder reads as
    missing, and the sentence the user needs is the one about the mount."""
    _reg, lib = _real_registry(tmp_path)
    _drop_root(lib, bare=False, rows=True)
    missing = str(tmp_path / "downloads" / "never-existed")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/import", json={"path": missing})

    assert resp.status_code == 503, resp.text
    assert "share mounted" in resp.json()["detail"]
    assert resp.json()["detail"] != _MISSING


def test_the_existence_check_costs_one_stat_and_no_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One stat per source, never a walk — the guard must not pay for the folder.

    ``validate`` moved OFF the loop this round, so the loop is no longer the
    reason: a walk on a hung mount holds a WORKER thread for as long as the mount
    hangs, and the start path is capped at one of those
    (``app/api/import_.py::start_import_off_loop``).
    """
    from app.import_jobs.runner import BeetsImportRunner

    _reg, lib = _real_registry(tmp_path)
    source = _album_folder(tmp_path / "downloads", b"counted")
    scans: list[str] = []
    stats: list[str] = []
    real_scandir = os.scandir
    real_stat = os.stat

    def counting_scandir(path: Any = ".") -> Any:
        scans.append(str(path))
        return real_scandir(path)

    def counting_stat(path: Any, **kwargs: Any) -> Any:
        stats.append(str(path))
        return real_stat(path, **kwargs)

    monkeypatch.setattr(os, "scandir", counting_scandir)
    monkeypatch.setattr(os, "stat", counting_stat)
    BeetsImportRunner(lib).validate([str(source)], None)

    assert [s for s in scans if s.startswith(str(source))] == []
    assert [s for s in stats if s.startswith(str(source))] == [str(source)]


# ----- 14: "doesn't exist" must not also be what a PERMISSIONS problem says -----
#
# Measured 2026-09-20: a real album folder under a parent chmod'd 0o600 ->
# ``os.path.exists`` False, exactly as for an absent one, while ``os.stat``
# reported errno 13, Permission denied. The container drops to the operator's
# PUID via gosu, so a downloads share owned by another uid is a real shape, and
# a PUID/GID mismatch is the commonest self-hosted misconfiguration.

_UNREADABLE = "That folder can't be read. Permission denied."


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits this test sets")
def test_a_source_the_owner_cannot_read_says_so_rather_than_missing(tmp_path: Path) -> None:
    """The refusal's whole value is naming what to fix.

    The absent case keeps the owner-approved sentence (its own test above); this
    is the other side, and one of the two must fail whichever way the errno
    split is removed.
    """
    _reg, lib = _real_registry(tmp_path)
    parent = tmp_path / "downloads"
    folder = _album_folder(parent, b"okc")
    parent.chmod(0o600)  # searchable by nobody: the stat below is refused
    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/import", json={"path": str(folder)})
    finally:
        parent.chmod(0o755)

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == _UNREADABLE
    # ``strerror`` is the OS's own summary and names no path, which is what makes
    # surfacing it safe.
    assert str(folder) not in resp.json()["detail"]
    assert _reg.active_job_id() is None
    assert list(lib.albums()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits this test sets")
def test_review_all_keeps_the_reason_when_the_settled_folders_are_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The batch route's plural copy must not overwrite an actionable diagnosis.

    The state is CONSTRUCTED, not observed: ``settled_folders`` answers ``[]`` on
    any ``OSError``, so the real system cannot hand this route a folder out of an
    inbox it cannot read — it reports "nothing to review" instead (pre-existing,
    recorded in BACKLOG, deliberately not changed here). The patch is what puts a
    folder in the caller's hands with the stat still refused, which is the only
    way to reach the branch under test: ``exc.unreadable`` deciding the 422's
    sentence, the defect the errno split exists to remove re-introduced one layer
    up.

    The sentence NAMES the folder. The shared one says "That folder", which is
    right when the caller typed a path and names nothing here - this route hands
    over folders the browser is never shown, and ``strerror`` carries no path by
    design, so a batch of N used to promise a specificity it did not have.
    """
    _reg, _lib = _real_registry(tmp_path)
    inbox = tmp_path / "inbox"
    folder = _album_folder(inbox, b"unreadable")

    import app.api.acquisition as acq_api

    monkeypatch.setattr(acq_api, "settled_folders", lambda *a, **k: [folder])
    monkeypatch.setattr(acq_api, "count_pending", lambda _d: 1)
    monkeypatch.setattr(app.state, "inbox_dir", inbox, raising=False)
    inbox.chmod(0o600)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/acquisition/review-inbox")
    finally:
        inbox.chmod(0o755)

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail == "\u201cunreadable\u201d can't be read. Permission denied.", detail
    # The BASENAME only - the listing already ships it as ``InboxItem.name``, so
    # this discloses nothing new, and the absolute path stays server-side.
    assert str(folder) not in detail
    assert str(inbox) not in detail
    assert _reg.active_job_id() is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits this test sets")
def test_the_per_item_import_answers_a_permissions_fault_rather_than_500ing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third route has to say what the other two say about the same fault.

    ``app.fsutil.is_dir`` swallows only ENAMETOOLONG, so ``Path.is_dir`` re-raises
    EACCES from the resolve step — BEFORE ``reg.start``, where the route's own
    ``except`` clauses could not see it. Measured on a pristine HEAD tree as an
    unhandled 500; pre-existing, and in scope because a PUID/GID mismatch reading
    as a crash is what this round exists to end.

    The fake runner, so the control below starts a job without beets touching the
    folder: what is under test is the resolve step, not the import.
    """
    from app.import_jobs.fakes import FakeImportRunner

    inbox = tmp_path / "inbox"
    folder = inbox / "locked"
    folder.mkdir(parents=True)
    monkeypatch.setattr(app.state, "inbox_dir", inbox, raising=False)
    client = TestClient(app, raise_server_exceptions=False)

    reset_registry(runner=FakeImportRunner(parked=[]))
    ok = client.post("/api/acquisition/inbox/items/import", json={"name": "locked"})
    assert ok.status_code == 200, ok.text  # the control: readable, and accepted

    reg = reset_registry(runner=FakeImportRunner(parked=[]))  # the single slot, free again
    inbox.chmod(0o600)  # searchable by nobody: the stat is refused
    try:
        resp = client.post("/api/acquisition/inbox/items/import", json={"name": "locked"})
    finally:
        inbox.chmod(0o755)

    assert resp.status_code == 422, resp.text
    # Not "Inbox item not found", which would call a folder that is right there
    # missing, and not a 500.
    assert resp.json()["detail"] == _UNREADABLE
    assert str(folder) not in resp.json()["detail"]
    assert reg.active_job_id() is None


def test_every_hostile_path_still_reaches_a_verdict(tmp_path: Path) -> None:
    """Paths the OS rejects reach this guard from the disk-side callers.

    ``os.stat`` RAISES where ``os.path.exists`` swallowed, so each shape has to
    be caught and classified rather than propagated. Everything that names
    nothing on disk reads as absent; a link loop is something the OS refused to
    answer for, so it takes the other sentence — and both are verdicts.
    """
    from app.import_jobs.runner import BeetsImportRunner, SourcePathMissingError

    _reg, lib = _real_registry(tmp_path)
    runner = BeetsImportRunner(lib)
    loop = tmp_path / "loop"
    loop.symlink_to(loop)  # ELOOP on every stat
    track = tmp_path / "track.mp3"
    track.write_bytes(b"x")

    absent = (
        os.fsdecode(b"/downloads/Caf\xe9/album"),  # a surrogateescape byte (encodes)
        # A LONE surrogate. The escaped PAIR that stood here reached no arm at
        # all: CPython folds it into U+10000, which encodes fine and stats ENOENT
        # (measured from a written file 2026-09-20 — a shell heredoc folds it the
        # same way, which is how it read as confirmed).
        "/downloads/\ud800",  # -> UnicodeEncodeError
        "/downloads/" + "x" * 4096,  # past NAME_MAX and PATH_MAX -> ENAMETOOLONG
        "/downloads/a\x00b",  # an embedded NUL -> ValueError
        f"{track}/x",  # a FILE with a child component -> ENOTDIR
    )
    for hostile in absent:
        with pytest.raises(SourcePathMissingError) as caught:
            runner.validate([hostile], None)
        assert str(caught.value) == _MISSING, hostile
        assert caught.value.unreadable is False, hostile

    with pytest.raises(SourcePathMissingError) as looped:
        runner.validate([str(loop)], None)
    assert looped.value.unreadable is True
    assert str(looped.value).startswith("That folder can't be read. ")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits this test sets")
def test_the_first_unreadable_member_decides_the_sentence(tmp_path: Path) -> None:
    """Two members the OS refuses differently, and the FIRST one is quoted.

    The short-circuit is what makes it the first rather than the last, and with
    two unreadables it is the only thing that does: dropping it left the file
    green. Both orders are asserted, so a mutant keeping the last one cannot pass
    by happening to agree.
    """
    from app.import_jobs.runner import missing_source_error

    loop = tmp_path / "loop"
    loop.symlink_to(loop)  # ELOOP
    parent = tmp_path / "shut"
    folder = parent / "album"
    folder.mkdir(parents=True)
    parent.chmod(0o600)  # EACCES on the stat below
    try:
        loop_first = missing_source_error([str(loop), str(folder)])
        folder_first = missing_source_error([str(folder), str(loop)])
    finally:
        parent.chmod(0o755)

    assert loop_first is not None
    assert folder_first is not None
    assert str(loop_first) == "That folder can't be read. Too many levels of symbolic links."
    assert str(folder_first) == _UNREADABLE
    assert loop_first.unreadable is True
    assert folder_first.unreadable is True


def test_an_empty_source_list_is_nothing_to_import(tmp_path: Path) -> None:
    """``validate([])`` used to return None and ``start([])`` to finish at phase
    ``done``, 0 albums, no error — the reported defect, reached through the
    guard. Not reachable from a route today (every caller hands over at least
    one source), so the predicate is what pins it: "no source exists" is exactly
    what an empty list means.
    """
    from app.import_jobs.runner import BeetsImportRunner, SourcePathMissingError

    _reg, lib = _real_registry(tmp_path)
    with pytest.raises(SourcePathMissingError, match=r"^That folder doesn\'t exist\.$"):
        BeetsImportRunner(lib).validate([], None)


# ----- 15: the start must not park a request thread on the source's filesystem -----


def _on_the_loop() -> bool:
    """Whether the CALLING thread is running the event loop.

    ``asyncio.get_running_loop()`` answers for the calling thread and raises in
    an anyio threadpool worker, so it is an exact discriminator for "this
    blocking call was offloaded" — where ``threading.main_thread()`` is not
    (the test client runs the loop in a portal thread, so neither side is main).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.mark.anyio
async def test_a_start_whose_source_stat_blocks_leaves_the_loop_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller-named path's filesystem must not decide the loop's availability.

    Measured on the previous commit against a FUSE filesystem whose ``getattr``
    sleeps: ``reg.start`` took 10000.6 ms and produced a 10002.7 ms event-loop
    gap against a 2.1 ms idle baseline (security seat, 2026-09-20). Reproduced
    here without FUSE by blocking inside ``validate``, which is where the stat
    is: the second request must be SERVED while the first is still in ``start``.
    """
    reg, lib = _real_registry(tmp_path)
    # A path that is not there, so the blocked ``validate`` ends in the ordinary
    # refusal and no beets session runs: this test is about the loop, not the
    # import.
    folder = tmp_path / "downloads" / "hung"
    entered = threading.Event()
    release = threading.Event()
    on_loop: list[bool] = []

    from app.import_jobs.runner import BeetsImportRunner

    real_validate = BeetsImportRunner.validate

    def blocking_validate(self: Any, paths: list[str], options: Any = None) -> str | None:
        on_loop.append(_on_the_loop())
        entered.set()
        release.wait(10)
        return real_validate(self, paths, options)

    monkeypatch.setattr(BeetsImportRunner, "validate", blocking_validate)
    transport = httpx.ASGITransport(app=app)
    # ``TestClient`` is stamped with a session cookie suite-wide (conftest); an
    # ``httpx.AsyncClient`` is not, and the session gate is secure by default.
    jar = {SESSION_COOKIE_NAME: session_cookie_value()}
    started: list[int] = []

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies=jar)

    async def start_it() -> None:
        async with client() as c:
            started.append((await c.post("/api/import", json={"path": str(folder)})).status_code)

    async with anyio.create_task_group() as group:
        group.start_soon(start_it)
        await anyio.to_thread.run_sync(entered.wait, 10)
        assert entered.is_set(), "the start never reached validate"
        began = time.monotonic()
        async with client() as c:
            probe = await c.get("/api/imports/active")
        served = time.monotonic() - began
        release.set()

    assert probe.status_code == 200, probe.text
    # A generous ceiling: on the loop this waits out the whole block (10 s here,
    # 10 s measured on the hung mount), so the margin is two orders of magnitude.
    assert served < 2.0, served
    # ...and the blocking call did not run on the loop thread at all. Asserted
    # as a LIST, so it doubles as a call-count pin.
    assert on_loop == [False]
    assert started == [422], started  # the blocked start still answered normally
    assert reg.active_job_id() is None
    assert list(lib.albums()) == []


def _loop_recording_runner(on_loop: list[bool], threads: list[int] | None = None) -> Any:
    """A ``FakeImportRunner`` recording whether ``validate`` ran on the loop.

    The fake, not the real runner, so the routes below answer without beets:
    what is under test is WHERE the caller-path work happens, not what it finds.
    ``threads`` collects the recorded ident when a caller compares the halves.
    """
    from app.import_jobs.fakes import FakeImportRunner

    class _Recording(FakeImportRunner):
        def validate(self, paths: list[str], options: Any = None) -> str | None:
            on_loop.append(_on_the_loop())
            if threads is not None:
                threads.append(threading.get_ident())
            return super().validate(paths, options)

    return _Recording(parked=[])


@pytest.mark.anyio
async def test_review_all_starts_off_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``start`` -> ``validate`` stats every settled folder; the loop must not wait."""
    import app.api.acquisition as acq_api

    inbox = tmp_path / "inbox"
    folder = inbox / "okc"
    folder.mkdir(parents=True)
    started_on_loop: list[bool] = []
    reset_registry(runner=_loop_recording_runner(started_on_loop))
    monkeypatch.setattr(acq_api, "settled_folders", lambda *a, **k: [folder])
    monkeypatch.setattr(acq_api, "count_pending", lambda _d: 1)
    monkeypatch.setattr(app.state, "inbox_dir", inbox, raising=False)

    transport = httpx.ASGITransport(app=app)
    jar = {SESSION_COOKIE_NAME: session_cookie_value()}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=jar
    ) as c:
        resp = await c.post("/api/acquisition/review-inbox")

    assert resp.status_code == 200, resp.text
    assert started_on_loop == [False]


@pytest.mark.anyio
async def test_the_per_item_import_does_its_whole_path_block_off_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This route resolves, CONTAINS and stats the name before it ever starts.

    ``contain`` calls ``Path.resolve`` and ``is_dir`` is a real stat, so moving
    only ``start`` would leave the route parked on the same filesystem it was
    parked on before. Both recorders have to come back off-loop.
    """
    import app.api.acquisition as acq_api
    from app.fsutil import is_dir as real_is_dir

    inbox = tmp_path / "inbox"
    folder = inbox / "okc"
    folder.mkdir(parents=True)
    started_on_loop: list[bool] = []
    stat_on_loop: list[bool] = []
    hop_threads: list[int] = []

    def recording_is_dir(path: Path) -> bool:
        stat_on_loop.append(_on_the_loop())
        hop_threads.append(threading.get_ident())
        return real_is_dir(path)

    reset_registry(runner=_loop_recording_runner(started_on_loop, hop_threads))
    monkeypatch.setattr(acq_api, "is_dir", recording_is_dir)
    monkeypatch.setattr(app.state, "inbox_dir", inbox, raising=False)

    transport = httpx.ASGITransport(app=app)
    jar = {SESSION_COOKIE_NAME: session_cookie_value()}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=jar
    ) as c:
        resp = await c.post("/api/acquisition/inbox/items/import", json={"name": "okc"})

    assert resp.status_code == 200, resp.text
    assert stat_on_loop == [False]
    assert started_on_loop == [False]
    # Both halves on ONE worker thread. Weak on its own — an idle pool hands the
    # second hop the same thread straight back — so the test below it is what
    # measures the property this only pins the shape of.
    assert len(hop_threads) == 2, hop_threads
    assert hop_threads[0] == hop_threads[1], hop_threads


@pytest.mark.anyio
async def test_the_per_item_import_does_not_queue_for_a_thread_between_check_and_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap between the containment check and the start must not grow with load.

    Nothing re-validates containment before beets opens the resolved path, so
    that string is trusted across the gap either way — what matters is its size.
    One synchronous block on HEAD, it was 0.003-0.010 ms and did not move with
    load; split into two threadpool hops it has to queue for a fresh token, and
    with 39 stuck and 12 contenders its MINIMUM was 3002.9 ms (measured
    2026-09-20). The named non-adversarial case is slskd finalising a temp
    directory name inside the window.

    Reproduced with the shared pool shrunk to ONE token: a competitor queues for
    it while the route's own hop holds it. In one hop the start never asks for a
    token again and the answer does not wait for the competitor; in two it lines
    up behind it.
    """
    import app.api.acquisition as acq_api
    from app.fsutil import is_dir as real_is_dir

    inbox = tmp_path / "inbox"
    folder = inbox / "okc"
    folder.mkdir(parents=True)
    entered = threading.Event()
    release = threading.Event()
    competitor_block = 2.0

    def blocking_is_dir(path: Path) -> bool:
        entered.set()
        release.wait(10)
        return real_is_dir(path)

    reset_registry(runner=_loop_recording_runner([]))
    monkeypatch.setattr(acq_api, "is_dir", blocking_is_dir)
    monkeypatch.setattr(app.state, "inbox_dir", inbox, raising=False)

    transport = httpx.ASGITransport(app=app)
    jar = {SESSION_COOKIE_NAME: session_cookie_value()}
    answered: list[tuple[int, float]] = []
    finished = anyio.Event()

    async def import_it() -> None:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", cookies=jar
        ) as c:
            began = time.monotonic()
            resp = await c.post("/api/acquisition/inbox/items/import", json={"name": "okc"})
            answered.append((resp.status_code, time.monotonic() - began))
        finished.set()

    limiter = anyio.to_thread.current_default_thread_limiter()
    original = limiter.total_tokens
    limiter.total_tokens = 1
    try:
        async with anyio.create_task_group() as group:
            group.start_soon(import_it)
            # Waited on the LOOP, never through ``to_thread``: the one token is
            # held by the hop under test, so waiting in the pool would deadlock.
            await anyio.sleep(0.25)
            assert entered.is_set(), "the route never reached the containment check"
            group.start_soon(anyio.to_thread.run_sync, time.sleep, competitor_block)
            await anyio.sleep(0.1)  # let the competitor reach the acquire
            released = time.monotonic()
            release.set()
            await finished.wait()
            waited = time.monotonic() - released
    finally:
        limiter.total_tokens = original

    assert answered[0][0] == 200, answered
    # A generous ceiling against a 2 s competitor: two hops would spend the whole
    # of it queued behind it.
    assert waited < 1.0, (waited, answered)


# ----- 16: the start path's share of the PROCESS-WIDE thread pool -----


@pytest.mark.anyio
async def test_the_import_start_path_takes_one_worker_thread_however_many_callers() -> None:
    """anyio's pool is process-wide and holds 40; sign-in draws from the same one.

    With the slot claimed only AFTER ``validate``, nothing bounded how many stats
    were in flight: 40 concurrent starts against a hung mount took every token,
    ``POST /api/auth/login`` timed out at 20 s, and ``GET /api/health`` answered
    200 in 0.7 ms throughout (measured 2026-09-20).
    """
    from app.api.import_ import start_import_off_loop

    lock = threading.Lock()
    release = threading.Event()
    inside = 0
    peak = 0
    done: list[str] = []

    def blocking() -> str:
        nonlocal inside, peak
        with lock:
            inside += 1
            peak = max(peak, inside)
        release.wait(10)
        with lock:
            inside -= 1
        return "ok"

    async def call() -> None:
        done.append(await start_import_off_loop(blocking))

    async with anyio.create_task_group() as group:
        for _ in range(5):
            group.start_soon(call)
        await anyio.sleep(0.2)  # every caller has queued by now
        with lock:
            concurrent = inside
        borrowed = anyio.to_thread.current_default_thread_limiter().borrowed_tokens
        release.set()

    assert concurrent == 1, concurrent
    assert peak == 1, peak
    # ...and the app keeps the other 39 for its sync ``Depends`` and its derive.
    assert borrowed == 1, borrowed
    assert done == ["ok"] * 5


@pytest.mark.anyio
async def test_a_cancelled_import_start_keeps_its_slot_until_the_thread_returns() -> None:
    """An ANYIO cancel scope must not hand the slot on while the worker is stuck.

    The reason admission is taken by hand rather than passed to anyio as
    ``limiter=``: ``app/api/auth.py::_one_derive_at_a_time`` measured peak 3
    concurrent derives at a cap of 1 that way, because unwinding released the
    token while the un-interruptible thread kept going.

    The scope is narrower than "a client disconnect", which is what this
    docstring used to claim, and both halves of that were wrong. The shield is
    ``abandon_on_cancel=False``, which anyio implements and only anyio's own
    scopes honour: under ``move_on_after`` the ``finally`` ran AFTER the worker
    (1.001 s against a 0.1 s deadline), while a raw ``asyncio.Task.cancel()``
    unwound at 0.100 s leaving ``borrowed_tokens == 0`` with the thread still
    running (anyio 4.13.0, measured 2026-09-20). And no disconnect reaches it
    anyway: Starlette 1.1.0's ``request_response`` awaits the handler directly,
    with no disconnect watcher and no task group. So this measures the anyio
    half - the only half that can happen.
    """
    from app.api.import_ import _IMPORT_START_SLOTS, start_import_off_loop

    entered = threading.Event()
    release = threading.Event()

    def blocking() -> str:
        entered.set()
        release.wait(10)
        return "ok"

    async def gives_up() -> None:
        with anyio.move_on_after(0.05):
            await start_import_off_loop(blocking)

    async with anyio.create_task_group() as group:
        group.start_soon(gives_up)
        await anyio.to_thread.run_sync(entered.wait, 10)
        await anyio.sleep(0.3)  # six times the caller's own deadline
        held_after_giving_up = _IMPORT_START_SLOTS.borrowed_tokens
        release.set()

    assert held_after_giving_up == 1, held_after_giving_up
    assert _IMPORT_START_SLOTS.borrowed_tokens == 0


@pytest.mark.anyio
async def test_a_start_queued_behind_a_wedged_one_answers_instead_of_waiting_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A waiter must eventually get an answer.

    The unbounded wait was justified by "the single import slot already refuses
    a concurrent start with 409". That is false in exactly the case the limiter
    exists for: ``ImportJobRegistry.start`` runs ``runner.validate`` BEFORE
    ``claim_slot``, so a start wedged in ``validate``'s ``os.stat`` holds the
    token while ``has_active_job()`` still reads idle - the 409 can never fire,
    and every later start used to wait forever with no status and no sentence.

    ``fail_after`` and not a bare await: with the bound reverted this hangs
    rather than fails, and a hang with no deadline takes the suite with it.
    """
    from fastapi import HTTPException

    from app.api import import_ as import_api

    monkeypatch.setattr(import_api, "_START_WAIT_SECONDS", 0.05)
    entered = threading.Event()
    release = threading.Event()

    def wedged() -> str:
        entered.set()
        release.wait(10)
        return "ok"

    async def holds_the_token() -> None:
        await import_api.start_import_off_loop(wedged)

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(holds_the_token)
            await anyio.to_thread.run_sync(entered.wait, 10)
            with anyio.fail_after(5):  # the mutation's hang becomes a failure
                with pytest.raises(HTTPException) as caught:
                    await import_api.start_import_off_loop(lambda: "never runs")
            release.set()
    finally:
        release.set()

    assert caught.value.status_code == 503, caught.value.status_code
    # Short, human, and it names what to check rather than what happened.
    assert caught.value.detail == (
        "Another import is still starting. Try again in a moment, or check that"
        " your music share is responding."
    )
    assert import_api._IMPORT_START_SLOTS.borrowed_tokens == 0


@pytest.mark.anyio
async def test_theinbox_reads_take_at_most_their_own_share_of_the_pool() -> None:
    """A route is bounded by its FIRST unbounded blocking hop.

    ``review_inbox`` runs ``settled_folders`` - scandir plus a ``has_audio`` and
    a ``_newest_mtime`` walk PER folder - before it reaches the 1-token start,
    and the two GETs the UI polls read the inbox with nothing in front of them.
    Unbounded, N concurrent callers took N of anyio's 40 process-wide tokens,
    which are shared with every sync ``Depends`` and the scrypt derive behind
    sign-in.
    """
    from app.api.acquisition import _INBOX_SCAN_SLOTS, inbox_read

    lock = threading.Lock()
    release = threading.Event()
    inside = 0
    peak = 0
    done: list[str] = []

    def blocking() -> str:
        nonlocal inside, peak
        with lock:
            inside += 1
            peak = max(peak, inside)
        release.wait(10)
        with lock:
            inside -= 1
        return "read"

    async def call() -> None:
        done.append(await inbox_read(blocking))

    callers = 12
    try:
        async with anyio.create_task_group() as group:
            for _ in range(callers):
                group.start_soon(call)
            await anyio.sleep(0.2)  # every caller has queued by now
            with lock:
                concurrent = inside
            borrowed = anyio.to_thread.current_default_thread_limiter().borrowed_tokens
            release.set()
    finally:
        release.set()

    cap = int(_INBOX_SCAN_SLOTS.total_tokens)
    assert concurrent == cap, concurrent
    assert peak == cap, peak
    # 12 callers, and the app still keeps 40 - cap for everything else.
    assert borrowed == cap, borrowed
    assert done == ["read"] * callers


@pytest.mark.anyio
async def test_a_wedged_start_does_not_block_the_polledinbox_read() -> None:
    """The reads get their OWN limiter, never ``_IMPORT_START_SLOTS``.

    The status GET is the UI's only liveness signal. Serialised behind a start
    wedged on a hung mount, the whole page would hang instead of just the
    import - so bounding the reads must not be done by folding them into the
    start's cap. Mutating ``inbox_read`` to use ``_IMPORT_START_SLOTS`` must
    fail this test.
    """
    from app.api.acquisition import inbox_read
    from app.api.import_ import start_import_off_loop

    entered = threading.Event()
    release = threading.Event()

    def wedged() -> str:
        entered.set()
        release.wait(10)
        return "ok"

    async def holds_the_start_token() -> None:
        await start_import_off_loop(wedged)

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(holds_the_start_token)
            await anyio.to_thread.run_sync(entered.wait, 10)
            with anyio.fail_after(5):  # a serialised read hangs; make it fail
                answered = await inbox_read(lambda: "the inbox still answers")
            release.set()
    finally:
        release.set()

    assert answered == "the inbox still answers"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits this test sets")
def test_the_batch_refusal_names_an_undecodable_folder_the_way_the_listing_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name goes through the same ``display_path`` the listing uses.

    A folder whose name is not valid UTF-8 reaches this sentence as lone
    surrogates. Interpolated raw they would make the JSON body non-encodable;
    through ``display_path`` they become the same U+FFFD the browser already
    holds for that row, so the refusal names exactly what the operator sees in
    the list.
    """
    _reg, _lib = _real_registry(tmp_path)
    inbox = tmp_path / "inbox"
    folder = _album_folder(inbox, b"bad\xffname")

    import app.api.acquisition as acq_api

    monkeypatch.setattr(acq_api, "settled_folders", lambda *a, **k: [folder])
    monkeypatch.setattr(acq_api, "count_pending", lambda _d: 1)
    monkeypatch.setattr(app.state, "inbox_dir", inbox, raising=False)
    inbox.chmod(0o600)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/acquisition/review-inbox")
    finally:
        inbox.chmod(0o755)

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail == "\u201cbad\ufffdname\u201d can't be read. Permission denied.", detail
    # The listing's own spelling of the same name, so the two agree.
    assert "bad\ufffdname" == display_path(os.fsdecode(os.fsencode(folder.name)))


# ----- the name the batch refusal quotes, and the message a crash renders -----


def _refusal_for(name: str) -> str:
    """The batch refusal for a folder called ``name``, straight from the sink."""
    from app.api.acquisition import _batch_unreadable_sentence
    from app.import_jobs.runner import unreadable_source_error

    return _batch_unreadable_sentence(
        unreadable_source_error(
            PermissionError(errno.EACCES, "Permission denied", f"/srv/inbox/{name}")
        )
    )


def test_a_hostile_folder_name_cannot_forge_the_refusal_it_is_quoted_in() -> None:
    """Quoting is only a delimiter if the value cannot spell the delimiter.

    The name is chosen by a REMOTE Soulseek peer - slskd names the local
    download directory after the peer's directory - and U+201C/U+201D are
    ordinary characters a POSIX filename may hold, which ``display_path``
    (bytes UTF-8 cannot carry) does not touch. Measured through the route
    before this guard: a folder named ``X" is fine. The folder "Y`` (curly)
    produced a complete forged clause, a leading U+202E rendered the server's
    own tail reversed, and a raw newline and an ESC both survived into the body.

    This round chose ``%r`` for the LOG sinks because repr escapes exactly this
    class of character, then shipped the same value unescaped into a response
    body. One rationale, both sinks.
    """
    forged = _refusal_for("X\u201d is fine. The folder \u201cY")
    # Exactly the two delimiters - the name contributed none.
    assert forged.count("\u201c") == 1, forged
    assert forged.count("\u201d") == 1, forged
    assert forged.endswith("can't be read. Permission denied."), forged

    for hostile in ("\u202eevil", "a\nWARNING forged line", "\x1b[31mRED", "\u2066flip"):
        sentence = _refusal_for(hostile)
        unprintable = [hex(ord(c)) for c in sentence if not c.isprintable() and c != " "]
        assert not unprintable, (hostile, unprintable)

    # The control: a name that only LOOKS like the sentence is still named in
    # full, which is what the quoting is for.
    assert _refusal_for("Permission denied") == (
        "\u201cPermission denied\u201d can't be read. Permission denied."
    )


def test_the_quoted_folder_name_is_capped() -> None:
    """A 255-character name made a 291-character red sentence read out in full.

    NAME_MAX admits 255, so the cap bites; the frame is 36 characters at this
    reason, so a capped refusal is 116 - about two lines at the banner's width.
    """
    from app.api.acquisition import _NAME_CAP

    sentence = _refusal_for("x" * 255)
    quoted = sentence.split("\u201c", 1)[1].split("\u201d", 1)[0]
    assert len(quoted) == _NAME_CAP, len(quoted)
    assert quoted.endswith("\u2026"), quoted
    assert len(sentence) == _NAME_CAP + 36, len(sentence)


def test_a_folder_with_no_nameable_basename_falls_back_to_the_singular() -> None:
    """``Path("/").name`` and ``Path("").name`` are both "".

    Uncapped and unguarded that reads as an empty quoted span - "" can't be
    read. - which names less than the shared singular does.
    """
    from app.api.acquisition import _batch_unreadable_sentence
    from app.import_jobs.runner import unreadable_source_error

    for filename in ("/", ""):
        exc = unreadable_source_error(PermissionError(errno.EACCES, "Permission denied", filename))
        assert _batch_unreadable_sentence(exc) == "That folder can't be read. Permission denied."


def test_a_crash_message_is_cut_at_its_first_absolute_path() -> None:
    """``isinstance(exc, OSError)`` is not the question "does this carry a path".

    It is true about ``OSError.filename`` and false about disclosure:
    ``beets.util.FilesystemError``, ``beets.library.ReadError`` and
    ``WriteError`` are plain ``Exception``s whose ``__str__`` interpolates
    absolute paths raw, and beets' family is the commonest carrier at the two
    sinks that render one.
    """
    from app.import_jobs.runner import path_free_message

    beets_style = (
        "Permission denied while moving /srv/downloads/inbox/Album to /srv/music/Artist/Album"
    )
    assert path_free_message(beets_style) == "Permission denied while moving"
    # A path with SPACES in it is why this cuts rather than redacting token by
    # token: a per-token redaction leaves "Floyd/The Wall" behind.
    assert path_free_message("error copying /srv/a/Pink Floyd/The Wall") == "error copying"
    assert path_free_message("[Errno 13] Permission denied: '/srv/x.db'") == (
        "[Errno 13] Permission denied"
    )
    # The controls: an ordinary message is untouched, and a slash inside a word
    # is not a path.
    assert path_free_message("No album found") == "No album found"
    assert path_free_message("matched 24/7 and/or nothing") == "matched 24/7 and/or nothing"
    # A message that IS a path leaves nothing; both sinks fall back to the class.
    assert path_free_message("/srv/only") == ""


def test_the_import_worker_catch_all_renders_no_absolute_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LIVE sink: ``run_import_worker`` does not wrap ``session.run()``.

    So a beets filesystem error escapes straight into the runner's catch-all,
    which reaches ``ImportJobState.error`` and is rendered by ImportPage.
    """
    from app.import_jobs.runner import BeetsImportRunner

    monkeypatch.setattr("app.import_jobs.runner.WebImportSession", lambda *a, **k: object())

    def explode(*_a: object, **_k: object) -> None:
        # A plain Exception, exactly beets' own shape - no ``filename``.
        raise Exception("Permission denied while moving /srv/downloads/Album to /srv/music/A")

    monkeypatch.setattr("app.import_jobs.runner.run_import_worker", explode)

    seen: list[str] = []
    done = threading.Event()

    def on_error(message: str) -> None:
        seen.append(message)
        done.set()

    BeetsImportRunner(None).run(
        ["/srv/downloads/Album"],
        bridge=None,  # type: ignore[arg-type]  # the stubbed session never reads it
        on_finish=done.set,
        on_error=on_error,
    )
    assert done.wait(5), "the worker thread never reported"
    assert seen == ["Permission denied while moving"], seen
