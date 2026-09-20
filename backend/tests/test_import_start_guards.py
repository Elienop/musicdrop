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

import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import beets.importer.tasks as beets_tasks
import pytest
from beets import config
from beets.autotag import AlbumInfo, AlbumMatch, TrackInfo
from beets.autotag.distance import distance
from beets.autotag.match import Proposal, assign_items
from beets.autotag.match import Recommendation as BeetsRec
from beets.library import Library
from fastapi.testclient import TestClient

from app.import_jobs.registry import ImportJobRegistry, reset_registry
from app.main import app
from tests.conftest import beets_dir_for, build_library, make_test_handle

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


def test_the_inbox_drain_survives_a_folder_that_is_no_longer_there(tmp_path: Path) -> None:
    """The source-missing refusal reaches ``start`` from the drain too.

    Terminal for the drop, not deferred: a requeue would poll a path that is
    gone. Uncaught it would kill this daemon thread and strand every later
    download, which is the defect the share-drop arm beside it was written for.
    """
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
        assert not ledger.seen(gone)  # nothing was handled, so nothing is retired
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
    None (beets/importer/tasks.py:1142-1147) while the rest import.
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
    assert resp.json()["detail"] == "That folder doesn't exist."
    assert list(lib.albums()) == []
    assert _reg.active_job_id() is None


# ----- 13: a source path that is not on disk -----
#
# Measured 2026-09-20: a path typed into the import box was truncated at a space
# (".../stop-test/Courtney" for ".../stop-test/Courtney Barnett"). beets takes a
# missing toppath down the branch a single FILE takes (ImportTaskFactory.paths,
# beets/importer/tasks.py:1055), reads no item, produces zero tasks and ends the
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

    beets imports a single file as one track (the same tasks.py:1055 branch), so
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


def test_a_hostile_path_is_answered_rather_than_raised(tmp_path: Path) -> None:
    """Paths the OS rejects reach this guard from the disk-side callers.
    ``os.path.exists`` answers False for each instead of propagating."""
    from app.import_jobs.runner import BeetsImportRunner, SourcePathMissingError

    _reg, lib = _real_registry(tmp_path)
    runner = BeetsImportRunner(lib)
    for hostile in (
        os.fsdecode(b"/downloads/Caf\xe9/album"),  # a surrogate-bearing path
        "/downloads/" + "x" * 4096,  # past NAME_MAX and PATH_MAX -> OSError
        "/downloads/a\x00b",  # an embedded NUL -> ValueError
    ):
        with pytest.raises(SourcePathMissingError):
            runner.validate([hostile], None)


def test_the_existence_check_costs_one_stat_and_no_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``validate`` runs on the event loop, so it must not list the source."""
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
