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


def _drop_root(lib: Library, *, bare: bool) -> Path:
    """Make the music root look like a dropped share. Returns the root."""
    root = Path(os.fsdecode(lib.directory))
    shutil.rmtree(root)
    if bare:
        root.mkdir()  # the mountpoint survives a dropped mount, empty
    return root


@pytest.mark.parametrize("bare", [False, True])
def test_an_import_does_not_start_while_the_library_root_is_unavailable(
    tmp_path: Path, bare: bool
) -> None:
    """Measured without the guard: beets re-created the root and filed the album
    onto it, and a ``move`` emptied the download."""
    _reg, lib = _real_registry(tmp_path)
    folder = _album_folder(tmp_path / "downloads", b"okc")
    before = sorted(p.name for p in folder.iterdir())
    root = _drop_root(lib, bare=bare)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/import", json={"path": str(folder), "options": {"operation": "move"}})
    assert resp.status_code == 503
    assert "share mounted" in resp.json()["detail"]
    assert sorted(p.name for p in folder.iterdir()) == before
    assert list(root.iterdir()) == [] if bare else not root.exists()
    assert len(list(lib.albums())) == 0


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


def _refuse_start_after_the_gate(
    reg: ImportJobRegistry, monkeypatch: pytest.MonkeyPatch, calls: list[int]
) -> None:
    """The TOCTOU shape: the gate is open, and the share drops before ``start``.

    The root is real here, so ``import_gate_clear`` lets the drain through; only
    ``start`` refuses. That is the window the gate cannot close.
    """
    from app.import_jobs.runner import LibraryRootUnavailableError

    def refusing_start(*args: Any, **kwargs: Any) -> str:
        calls.append(1)
        raise LibraryRootUnavailableError("Library folder unavailable. Is the music share mounted?")

    monkeypatch.setattr(reg, "start", refusing_start)


def test_the_inbox_drain_survives_a_share_that_drops_after_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured: uncaught, the refusal killed the daemon thread outright."""
    from app.acquisition.ledger import AcquisitionLedger
    from app.acquisition.queue import AcquisitionQueue

    reg, _lib = _real_registry(tmp_path)
    folder = _album_folder(tmp_path / "inbox", b"okc")
    calls: list[int] = []
    _refuse_start_after_the_gate(reg, monkeypatch, calls)
    ledger = AcquisitionLedger(tmp_path / "ledger")
    queue = AcquisitionQueue(
        import_registry=reg, ledger=ledger, poll_interval=0.01, busy_backoff=0.01
    )
    queue.start()
    queue.enqueue(folder)
    try:
        time.sleep(0.4)
        assert len(calls) > 1  # retried, not abandoned
        assert queue.status().processed == 0
        assert not ledger.seen(folder)
        alive = [t for t in threading.enumerate() if t.name == "musicdrop-acquisition"]
        assert [t.is_alive() for t in alive] == [True]
    finally:
        queue.stop()


def test_a_bank_row_returns_to_queued_when_the_share_drops_after_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured: uncaught, the row failed permanently on a merely-unmounted share."""
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
    calls: list[int] = []
    _refuse_start_after_the_gate(reg, monkeypatch, calls)

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
        assert len(calls) > 1  # retried, not burned
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
    gates.reset_root_wait_latch()
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
