"""GET /api/acquisition/inbox/items + POST /api/acquisition/inbox/items/import.

The Review page lists the inbox backlog (top-level folders holding audio,
ledger-annotated, never filtered) and imports ONE folder at a time. A
client-supplied folder name is re-rooted under the inbox and ``contain``-guarded
so it can never escape.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from app.acquisition.ledger import AcquisitionLedger
from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import reset_registry
from app.main import app

_SENTINEL = object()


@contextmanager
def _state(inbox: Path | None, ledger: AcquisitionLedger | None = None) -> Iterator[None]:
    """Set (or clear) ``app.state.inbox_dir`` + ``acquisition_ledger``, then restore."""
    prior_inbox = getattr(app.state, "inbox_dir", _SENTINEL)
    prior_ledger = getattr(app.state, "acquisition_ledger", _SENTINEL)
    if inbox is None:
        if prior_inbox is not _SENTINEL:
            del app.state.inbox_dir
    else:
        app.state.inbox_dir = inbox
    if ledger is None:
        if prior_ledger is not _SENTINEL:
            del app.state.acquisition_ledger
    else:
        app.state.acquisition_ledger = ledger
    try:
        yield
    finally:
        for name, prior in (("inbox_dir", prior_inbox), ("acquisition_ledger", prior_ledger)):
            if prior is _SENTINEL:
                if getattr(app.state, name, _SENTINEL) is not _SENTINEL:
                    delattr(app.state, name)
            else:
                setattr(app.state, name, prior)


def _album(inbox: Path, *parts: str, tracks: int = 2) -> Path:
    folder = inbox.joinpath(*parts)
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(tracks):
        (folder / f"{i + 1:02d} track.flac").write_bytes(b"\0")
    return folder


# ----- listing -----


def test_list_inbox_empty_without_state() -> None:
    reset_registry(runner=FakeImportRunner(parked=[]))
    with _state(None):
        resp = TestClient(app).get("/api/acquisition/inbox/items")
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_list_inbox_lists_audio_folders_skips_empty(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    _album(inbox, "Direct Album", tracks=2)  # flat album at the inbox root
    _album(inbox, "Some Artist", "Nested Album", tracks=1)  # nested under an artist
    (inbox / "Empty Leftover").mkdir(parents=True)  # no audio -> skipped
    (inbox / "loose.flac").write_bytes(b"\0")  # a file, not a dir -> skipped
    reset_registry(runner=FakeImportRunner(parked=[]))
    with _state(inbox):
        resp = TestClient(app).get("/api/acquisition/inbox/items")
    assert resp.status_code == 200
    items = {it["name"]: it for it in resp.json()["items"]}
    assert set(items) == {"Direct Album", "Some Artist"}
    assert items["Direct Album"]["track_count"] == 2
    assert items["Some Artist"]["track_count"] == 1
    assert items["Direct Album"]["outcome"] is None  # fresh, no ledger entry
    assert "source" not in items["Direct Album"]  # no hardcoded provenance claim


def test_list_inbox_skips_symlinked_top_dir(tmp_path: Path) -> None:
    # A symlink planted in the inbox that points at an audio tree OUTSIDE the
    # inbox must NOT be listed or walked — parity with the import path's contain()
    # symlink-escape rejection.
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    outside = tmp_path / "outside"
    (outside / "Album").mkdir(parents=True)
    (outside / "Album" / "01.flac").write_bytes(b"\0")
    (inbox / "escape").symlink_to(outside)
    _album(inbox, "Real Album", tracks=1)  # a genuine inbox item
    reset_registry(runner=FakeImportRunner(parked=[]))
    with _state(inbox):
        resp = TestClient(app).get("/api/acquisition/inbox/items")
    names = {it["name"] for it in resp.json()["items"]}
    assert names == {"Real Album"}  # the symlink is skipped


def test_status_inbox_pending_counts_only_audio_dirs(tmp_path: Path) -> None:
    # The nav badge (inbox_pending) must match the listing's item definition, so a
    # loose file or an empty leftover dir can't keep a phantom badge lit.
    inbox = tmp_path / "inbox"
    _album(inbox, "Real Album", tracks=1)
    (inbox / "Empty Leftover").mkdir()  # no audio
    (inbox / "cover.jpg").write_bytes(b"\0")  # a loose non-audio file
    reset_registry(runner=FakeImportRunner(parked=[]))
    with _state(inbox):
        resp = TestClient(app).get("/api/acquisition/status")
    assert resp.status_code == 200
    assert resp.json()["inbox_pending"] == 1


def test_list_inbox_annotates_set_aside_without_filtering(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    album = _album(inbox, "Some Artist", "Nested Album", tracks=1)
    ledger = AcquisitionLedger(inbox / ".musicdrop-ledger.json")
    ledger.mark(album, outcome="set_aside")  # ledger keys the DEEPER album path
    reset_registry(runner=FakeImportRunner(parked=[]))
    with _state(inbox, ledger):
        resp = TestClient(app).get("/api/acquisition/inbox/items")
    items = {it["name"]: it for it in resp.json()["items"]}
    # The set-aside item is STILL listed (annotate, never filter), and the
    # deeper ledger path annotates the top-level folder row.
    assert "Some Artist" in items
    assert items["Some Artist"]["outcome"] == "set_aside"


# ----- single-folder import -----


def test_import_inbox_item_starts_attended_move(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    _album(inbox, "Echoes 4412", tracks=2)
    fake = FakeImportRunner(parked=[])
    reset_registry(runner=fake)
    with _state(inbox):
        client = TestClient(app)
        resp = client.post("/api/acquisition/inbox/items/import", json={"name": "Echoes 4412"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["started"] is True
        assert body["job_id"]
        assert fake.received_options is not None
        assert fake.received_options.operation == "move"
        assert fake.received_options.unattended is False
        state = client.get(f"/api/import/{body['job_id']}").json()
        assert state["origin"] == "inbox"


def test_import_inbox_item_rejects_escape(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    reset_registry(runner=FakeImportRunner(parked=[]))
    with _state(inbox):
        client = TestClient(app)
        for name in ("../etc", "/etc", "evil\x00album", "Does Not Exist"):
            resp = client.post("/api/acquisition/inbox/items/import", json={"name": name})
            assert resp.status_code == 404, name


def test_import_inbox_item_409_while_swap_lock_held(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    _album(inbox, "Echoes 4412", tracks=1)
    fake = FakeImportRunner(parked=[])
    reset_registry(runner=fake)

    class _LockedLock:
        def locked(self) -> bool:
            return True

    prior = getattr(app.state, "beets_swap_lock", None)
    app.state.beets_swap_lock = _LockedLock()
    try:
        with _state(inbox):
            resp = TestClient(app).post(
                "/api/acquisition/inbox/items/import", json={"name": "Echoes 4412"}
            )
    finally:
        if prior is None:
            del app.state.beets_swap_lock
        else:
            app.state.beets_swap_lock = prior
    assert resp.status_code == 409
    assert fake.received_options is None
