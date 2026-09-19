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
from tests.conftest import build_library

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
    resp = client.post("/api/import", json={"path": str(tmp_path / "dl")})
    assert resp.status_code == 202
    # Drained before returning: a worker thread outliving the test reads beets'
    # config after the autouse reset and raises confuse.NotFoundError elsewhere.
    _drive(client, resp.json()["job_id"])


# ----- the posted path is bounded (the resolver is quadratic, on the event loop) -----


def test_a_path_longer_than_PATH_MAX_is_refused(tmp_path: Path) -> None:
    """4097 characters can name no folder, and the resolver is quadratic.

    Measured unbounded by the security seat: 80 KB of path stalled the whole
    API for 210 s, on the event loop, uncancellable. At the bound the densest
    path (2048 placeholder components, U+FFFD being one character) costs 314 ms.
    """
    _real_registry(tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/import", json={"path": "/" + "x" * 4096})
    assert resp.status_code == 422
    assert "4096" in resp.text


def test_a_path_at_PATH_MAX_still_starts(tmp_path: Path) -> None:
    """The control: the bound refuses nothing the feature can do."""
    _real_registry(tmp_path)
    client = TestClient(app)
    resp = client.post("/api/import", json={"path": "/" + "x" * 4095})
    assert resp.status_code == 202
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
    ordinary = str(tmp_path / "downloads" / "plain")
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


def _seed_a_library_row(lib: Library) -> None:
    """One item row naming a file under the music root — a library with content."""
    from beets.library import Item

    item = Item(album="The Wall", albumartist="Pink Floyd", title="T1", track=1)
    item.path = os.fsencode(os.path.join(os.fsdecode(lib.directory), "Pink Floyd/The Wall/01.flac"))
    lib.add_album([item])


def _drop_root(lib: Library, *, bare: bool, rows: bool = True) -> Path:
    """Make the music root look like a dropped share. Returns the root.

    ``rows`` seeds one item row BEFORE dropping, because an EMPTY root is only a
    dropped share when the library still lists files. With no rows it is the
    empty bind mount Docker gives every new install, which must still import
    (C1) — ``rows=False`` is how that case is spelled here.
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


def test_trash_and_delete_keep_the_stricter_predicate(tmp_path: Path) -> None:
    """Invariant 4: the exemption is the IMPORT side's, not the predicate's.

    ``require_library_root`` is what Trash, Delete, Restore and disk sync ask,
    and an empty root must still refuse them however empty the database is.
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
    app.state.inbox_dir = inbox
    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/acquisition/review-inbox")
        assert resp.status_code == 503
        assert "share mounted" in resp.json()["detail"]
    finally:
        app.state.inbox_dir = None


def test_the_per_item_inbox_import_refuses_while_the_library_root_is_unavailable(
    tmp_path: Path,
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
    app.state.inbox_dir = inbox
    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/acquisition/inbox/items/import", json={"name": folder.name})
        assert resp.status_code == 503, resp.text
        assert "share mounted" in resp.json()["detail"]
    finally:
        app.state.inbox_dir = None


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


def _make_the_root_question_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any unexpected failure of the gate's own filesystem work."""
    from app.import_jobs import gates

    def boom(_library: object) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(gates, "require_importable_library_root", boom)


def test_an_unexpected_raise_inside_the_gate_reads_as_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Measured before the fix: the raise escaped and killed the drain thread.

    Also pins the latch: the drains poll this four times a second between them,
    so one failing episode is ONE record, and a later episode says so again.
    """
    import logging

    from app.import_jobs import gates

    reg, _lib = _real_registry(tmp_path)
    gates._gate_fault.clear()
    assert gates.import_gate_clear(reg, None) is True  # control: healthy root, gate open
    with caplog.at_level(logging.INFO):
        with monkeypatch.context() as broken:
            _make_the_root_question_raise(broken)
            for _ in range(5):
                assert gates.import_gate_clear(reg, None) is False
        assert gates.import_gate_clear(reg, None) is True  # the fault cleared
        with monkeypatch.context() as broken_again:
            _make_the_root_question_raise(broken_again)
            assert gates.import_gate_clear(reg, None) is False
    faults = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(faults) == 2, [r.getMessage() for r in faults]
    assert {r.name for r in faults} == {"uvicorn.error"}


def test_the_root_question_runs_even_when_another_check_would_close_the_gate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Ordering: asked FIRST, so the wait latch cannot go stale behind an early return.

    Without it a share that dropped while a backfill held the gate is never
    logged, and its recovery never clears the latch, so the NEXT outage is
    silent too.
    """
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


def _gate_that_drops_the_root(
    module: Any, lib: Library, monkeypatch: pytest.MonkeyPatch, drops: list[int]
) -> None:
    """Drive the REAL TOCTOU window: the share goes right after the gate opens.

    Nothing is stubbed on the way down. The wrapper restores the root before
    each poll, so the drain's own ``import_gate_clear`` answers True on the
    healthy root, then removes it — leaving the bare mountpoint of a library
    that still lists files. ``start`` -> ``validate`` then raises on its own, so
    the test sees the type the ``except`` arm actually has to name.
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


# ----- 12: Review all survives a folder that vanished since the listing -----


def test_review_all_survives_a_folder_that_vanished_since_the_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One settled folder removed between the listing and the start.

    The current tree holds no source-missing refusal, so the batch must simply
    import what is still there. This pins that — a refusal added later must not
    make one vanished member refuse the whole click.
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
    app.state.inbox_dir = inbox
    try:
        client = TestClient(app)
        resp = client.post("/api/acquisition/review-inbox")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["started"] is True
        state = _drive(client, body["job_id"])
    finally:
        app.state.inbox_dir = None
    assert state["phase"] == "done", state
    assert not gone.exists()
    assert len(list(lib.albums())) == 1
    assert not stays.exists() or sorted(p.name for p in stays.iterdir()) == []
