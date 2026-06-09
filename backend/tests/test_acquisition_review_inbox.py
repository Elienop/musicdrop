"""POST /api/acquisition/review-inbox — one-click attended import of the inbox.

The slskd inbox path is fixed (configured once), so reviewing the set-aside
backlog is a single click that resolves the path SERVER-SIDE and starts a normal
attended import with ``operation="move"`` (applied albums leave the inbox) and
``origin="inbox"``. An empty inbox is a no-op (``started=False``), never an
error. The shared import-slot gate refuses while another beets mutation holds
the swap lock.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import reset_registry
from app.main import app


@contextmanager
def _inbox_on_state(path: Path | None) -> Iterator[None]:
    """Set (or clear) ``app.state.inbox_dir`` for the duration, then restore it."""
    sentinel = object()
    prior: object = getattr(app.state, "inbox_dir", sentinel)
    if path is None:
        if prior is not sentinel:
            del app.state.inbox_dir
    else:
        app.state.inbox_dir = path
    try:
        yield
    finally:
        if prior is sentinel:
            if getattr(app.state, "inbox_dir", sentinel) is not sentinel:
                del app.state.inbox_dir
        else:
            app.state.inbox_dir = prior


def test_review_inbox_no_state_is_empty_noop() -> None:
    # Lifespan-less client: no inbox_dir on app.state -> empty, not 500.
    reset_registry(runner=FakeImportRunner(parked=[]))
    with _inbox_on_state(None):
        resp = TestClient(app).post("/api/acquisition/review-inbox")
    assert resp.status_code == 200
    assert resp.json() == {"started": False, "job_id": None, "pending": 0}


def test_review_inbox_only_ledger_is_empty_noop(tmp_path: Path) -> None:
    # An inbox holding only the ledger file (no album folders) is empty.
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / ".musicdrop-ledger.json").write_text("{}")
    fake = FakeImportRunner(parked=[])
    reset_registry(runner=fake)
    with _inbox_on_state(inbox):
        resp = TestClient(app).post("/api/acquisition/review-inbox")
    assert resp.status_code == 200
    assert resp.json() == {"started": False, "job_id": None, "pending": 0}
    assert fake.received_options is None  # nothing started


def test_review_inbox_starts_attended_move_import(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    album = inbox / "ZZ Artist" / "Some Album"
    album.mkdir(parents=True)
    (album / "01 track.flac").write_bytes(b"\0")  # a real (audio-bearing) drop
    fake = FakeImportRunner(parked=[])
    reset_registry(runner=fake)
    with _inbox_on_state(inbox):
        client = TestClient(app)
        resp = client.post("/api/acquisition/review-inbox")
        assert resp.status_code == 200
        body = resp.json()
        assert body["started"] is True
        assert body["pending"] == 1
        job_id = body["job_id"]
        assert job_id
        # Attended (NOT unattended) + forced move; labelled origin=inbox.
        assert fake.received_options is not None
        assert fake.received_options.operation == "move"
        assert fake.received_options.unattended is False
        state = client.get(f"/api/import/{job_id}").json()
        assert state["origin"] == "inbox"


def test_review_inbox_409_while_swap_lock_held(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    (inbox / "ZZ Artist").mkdir(parents=True)
    fake = FakeImportRunner(parked=[])
    reset_registry(runner=fake)

    class _LockedLock:
        def locked(self) -> bool:
            return True

    prior = getattr(app.state, "beets_swap_lock", None)
    app.state.beets_swap_lock = _LockedLock()
    try:
        with _inbox_on_state(inbox):
            resp = TestClient(app).post("/api/acquisition/review-inbox")
    finally:
        if prior is None:
            del app.state.beets_swap_lock
        else:
            app.state.beets_swap_lock = prior
    assert resp.status_code == 409
    assert fake.received_options is None  # gate fired before start
