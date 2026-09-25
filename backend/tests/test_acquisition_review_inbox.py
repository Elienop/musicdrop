"""POST /api/acquisition/review-inbox — one-click attended import of the inbox.

The slskd inbox path is fixed (configured once), so reviewing the set-aside
backlog is a single click that resolves the path SERVER-SIDE and starts a normal
attended import with ``operation="move"`` (applied albums leave the inbox) and
``origin="inbox"`` — targeting the SETTLED top-level folders, never the inbox
root (which is the downloader's live output dir). Nothing settled is a no-op
(``started=False``), never an error. The shared import-slot gate refuses
while another beets mutation holds the swap lock.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import reset_registry
from app.main import app

# Every route here reads the bank; keep it in the test's tmp dir.
pytestmark = pytest.mark.usefixtures("inbox_bank_dir")


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


def _age(folder: Path, seconds: float = 3600) -> None:
    """Backdate every file under ``folder`` so it reads as SETTLED (quiet)."""
    import os
    import time

    old = time.time() - seconds
    for path in [folder, *folder.rglob("*")]:
        os.utime(path, (old, old))


def test_review_inbox_no_state_is_empty_noop() -> None:
    # Lifespan-less client: no inbox_dir on app.state -> empty, not 500.
    reset_registry(runner=FakeImportRunner(parked=[]))
    with _inbox_on_state(None):
        resp = TestClient(app).post("/api/acquisition/review-inbox")
    assert resp.status_code == 200
    assert resp.json() == {"started": False, "job_id": None, "pending": 0, "in_flight": 0}


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
    assert resp.json() == {"started": False, "job_id": None, "pending": 0, "in_flight": 0}
    assert fake.received_options is None  # nothing started


def test_review_inbox_starts_attended_move_import(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    album = inbox / "ZZ Artist" / "Some Album"
    album.mkdir(parents=True)
    (album / "01 track.flac").write_bytes(b"\0")  # a real (audio-bearing) drop
    _age(inbox)  # quiet long enough to be settled
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
        # I1: the SETTLED top-level folder is the toppath — never the inbox root,
        # which would sweep in whatever is still downloading beside it.
        assert fake.received_paths == [str(inbox / "ZZ Artist")]
        state = client.get(f"/api/import/{job_id}").json()
        assert state["origin"] == "inbox"


def test_review_inbox_409_while_swap_lock_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = tmp_path / "inbox"
    (inbox / "ZZ Artist").mkdir(parents=True)
    fake = FakeImportRunner(parked=[])
    reset_registry(runner=fake)

    class _LockedLock:
        def locked(self) -> bool:
            return True

    monkeypatch.setattr(app.state, "beets_swap_lock", _LockedLock(), raising=False)
    with _inbox_on_state(inbox):
        resp = TestClient(app).post("/api/acquisition/review-inbox")
    assert resp.status_code == 409
    assert fake.received_options is None  # gate fired before start


# ----- I1: never import the inbox ROOT; skip folders still being written -----


def test_review_inbox_skips_a_still_downloading_folder(tmp_path: Path) -> None:
    # The inbox IS the downloader's live output dir: slskd moves each file in as
    # it completes. A folder touched moments ago may still be gaining tracks, and
    # importing it would file a PARTIAL album (whose remainder then arrives and
    # imports again as a duplicate). Only the quiet folder may be handed over.
    inbox = tmp_path / "inbox"
    settled = inbox / "Aged Album"
    fresh = inbox / "Still Downloading"
    for folder in (settled, fresh):
        folder.mkdir(parents=True)
        (folder / "01 track.flac").write_bytes(b"\0")
    _age(inbox)
    (fresh / "02 track.flac").write_bytes(b"\0")  # lands NOW -> unsettled

    fake = FakeImportRunner(parked=[])
    reset_registry(runner=fake)
    with _inbox_on_state(inbox):
        resp = TestClient(app).post("/api/acquisition/review-inbox")

    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is True
    assert body["pending"] == 1  # only the settled one counts
    assert fake.received_paths == [str(settled)]
    assert str(inbox) not in (fake.received_paths or [])  # never the root


def test_review_inbox_all_folders_in_flight_is_a_noop(tmp_path: Path) -> None:
    # Everything still downloading -> nothing to review right now. A no-op, not an
    # error, and emphatically NOT a root import.
    inbox = tmp_path / "inbox"
    album = inbox / "Still Downloading"
    album.mkdir(parents=True)
    (album / "01 track.flac").write_bytes(b"\0")  # fresh mtime

    fake = FakeImportRunner(parked=[])
    reset_registry(runner=fake)
    with _inbox_on_state(inbox):
        resp = TestClient(app).post("/api/acquisition/review-inbox")

    assert resp.status_code == 200
    # in_flight > 0 is what tells the UI "still arriving" apart from "inbox
    # empty" — both are started=False, but only the latter means nothing is left.
    assert resp.json() == {"started": False, "job_id": None, "pending": 0, "in_flight": 1}
    assert fake.received_paths is None  # nothing started at all


def test_review_inbox_hands_over_every_settled_folder(tmp_path: Path) -> None:
    # Several settled drops go over as their OWN toppaths (beets treats each as a
    # separate import root), so one shared parent is never the target.
    inbox = tmp_path / "inbox"
    for name in ("A Album", "B Album", "C Album"):
        folder = inbox / name
        folder.mkdir(parents=True)
        (folder / "01 track.flac").write_bytes(b"\0")
    _age(inbox)

    fake = FakeImportRunner(parked=[])
    reset_registry(runner=fake)
    with _inbox_on_state(inbox):
        resp = TestClient(app).post("/api/acquisition/review-inbox")

    assert resp.json()["pending"] == 3
    assert fake.received_paths == [
        str(inbox / "A Album"),
        str(inbox / "B Album"),
        str(inbox / "C Album"),
    ]


def test_review_inbox_ignores_an_audio_free_folder(tmp_path: Path) -> None:
    # Same item definition as the listing/badge: a folder with no audio is not an
    # inbox item, settled or not.
    inbox = tmp_path / "inbox"
    (inbox / "Just Artwork").mkdir(parents=True)
    (inbox / "Just Artwork" / "cover.jpg").write_bytes(b"\0")
    _age(inbox)

    fake = FakeImportRunner(parked=[])
    reset_registry(runner=fake)
    with _inbox_on_state(inbox):
        resp = TestClient(app).post("/api/acquisition/review-inbox")

    assert resp.json() == {"started": False, "job_id": None, "pending": 0, "in_flight": 0}


@pytest.mark.parametrize("settle", [0])
def test_review_inbox_settle_window_is_configurable(
    tmp_path: Path, settle: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With the window at 0 a just-written folder is eligible immediately — the
    # knob exists so a user whose downloader writes differently can tune it.
    inbox = tmp_path / "inbox"
    album = inbox / "Fresh Album"
    album.mkdir(parents=True)
    (album / "01 track.flac").write_bytes(b"\0")

    fake = FakeImportRunner(parked=[])
    reset_registry(runner=fake)
    monkeypatch.setattr(
        app.state,
        "settings",
        SimpleNamespace(inbox_settle_seconds=settle),
        raising=False,
    )
    with _inbox_on_state(inbox):
        resp = TestClient(app).post("/api/acquisition/review-inbox")

    assert resp.json()["started"] is True
    assert fake.received_paths == [str(album)]
