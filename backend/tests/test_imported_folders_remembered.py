"""Remember and hide (decisions #77): a slskd folder MusicDrop imported is not listed.

Every run that ends ``done``, was not cut short, and landed every album it fed
from a folder strictly inside slskd's folder records that folder in the
acquisition ledger as ``imported``, whoever started it. "Not imported yet" (the
list, the badge and Review all) leaves such a folder out while its own
``(st_mtime, st_size)`` still matches, so a file added, removed or renamed in it
brings it back.

The runs are the FakeImportRunner's, so the files stay where they are, as they
do under every config that keeps downloads. That the inbox starts use beets'
own file operation is pinned where each start is tested
(``test_acquisition_queue``'s ``_INBOX_OPTS``, ``test_acquisition_inbox_items``,
``test_acquisition_review_inbox``, ``test_bank_apply``); what ``default``
resolves to is ``test_import_worker_config_restore``'s.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient

from app.acquisition.inbox import record_imported
from app.acquisition.ledger import AcquisitionLedger
from app.acquisition.queue import AcquisitionQueue
from app.bank import store
from app.bank.apply_runner import BankApplyRunner
from app.bank.fingerprint import folder_fingerprint
from app.beets.import_session import ImportBridge
from app.config import settings
from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import ImportJobRegistry, reset_registry
from app.main import app
from app.models.bank import BankDecision
from app.models.import_models import (
    AlbumOutcome,
    AlbumOutcomeStatus,
    DuplicateAction,
    ImportOptions,
    Recommendation,
)

if TYPE_CHECKING:
    from app.beets.library import LibraryHandle
    from app.events.broker import EventBroker

# Every inbox route reads the bank; keep it in the test's tmp dir.
pytestmark = pytest.mark.usefixtures("inbox_bank_dir")

_SETTLED = time.time() - 3600


class _Finished:
    """Stands in for the event broker: ``_on_finish`` publishes AFTER it records.

    Waiting on the job's phase is not enough, because the phase goes ``done``
    under the lock and the record is written after it; this is the one signal
    that follows the record on every finished run, recorded or not.
    """

    def __init__(self) -> None:
        self.fired = threading.Event()

    def publish_library_changed(self) -> None:
        self.fired.set()

    def wait(self) -> None:
        assert self.fired.wait(5.0), "the import never finished"


def _album(inbox: Path, *parts: str) -> Path:
    """A settled album folder holding one audio file."""
    folder = inbox.joinpath(*parts)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "01 track.flac").write_bytes(b"\0")
    for path in [folder, *folder.rglob("*")]:
        os.utime(path, (_SETTLED, _SETTLED))
    return folder


def _landed(index: int, folder: Path) -> AlbumOutcome:
    """An album that auto-applied and got its library id: it landed."""
    return AlbumOutcome(
        album_index=index,
        folder=str(folder),
        artist="A",
        album=f"B{index}",
        recommendation=Recommendation.strong,
        confidence=99.0,
        status=AlbumOutcomeStatus.applied,
        album_id=index + 1,
    )


def _skipped(index: int, folder: Path) -> AlbumOutcome:
    return _landed(index, folder).model_copy(
        update={"status": AlbumOutcomeStatus.skipped, "album_id": None}
    )


def _inbox(tmp_path: Path) -> Path:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    return inbox.resolve()  # how the lifespan hands it over (resolve_inbox_dir)


def _wire(
    runner: FakeImportRunner, ledger: AcquisitionLedger, inbox: Path
) -> tuple[ImportJobRegistry, _Finished]:
    """A fresh global registry with the recorder the lifespan attaches."""
    reg = reset_registry(runner=runner)
    reg.attach_import_recorder(partial(record_imported, ledger, inbox))
    finished = _Finished()
    reg.attach_event_broker(cast("EventBroker", finished))
    return reg, finished


def _surfaces(
    client: TestClient, runner: FakeImportRunner
) -> tuple[set[str], int, list[str] | None]:
    """The list's names, the badge, and what Review all hands over (by name)."""
    listed = {item["name"] for item in client.get("/api/acquisition/inbox/items").json()["items"]}
    badge = client.get("/api/acquisition/status").json()["inbox_pending"]
    runner.received_paths = None
    client.post("/api/acquisition/review-inbox")
    handed = (
        None if runner.received_paths is None else [Path(p).name for p in runner.received_paths]
    )
    return listed, badge, handed


def _on_state(monkeypatch: pytest.MonkeyPatch, inbox: Path, ledger: AcquisitionLedger) -> None:
    monkeypatch.setattr(app.state, "inbox_dir", inbox, raising=False)
    monkeypatch.setattr(app.state, "acquisition_ledger", ledger, raising=False)


def _review_one(client: TestClient, name: str, finished: _Finished) -> str:
    """Per-row Review of ``name``; returns its job id once the run finished."""
    resp = client.post("/api/acquisition/inbox/items/import", json={"name": name})
    assert resp.status_code == 200, resp.text
    finished.wait()
    job_id: str = resp.json()["job_id"]
    return job_id


def _rows(ledger: AcquisitionLedger) -> dict[str, str]:
    return {row.path: row.outcome for row in ledger.entries()}


def _review_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, AcquisitionLedger, TestClient]:
    """A per-row Review of ``Kept`` that lands its one album, beside ``Other``."""
    inbox = _inbox(tmp_path)
    kept = _album(inbox, "Kept")
    _album(inbox, "Other")
    ledger = AcquisitionLedger(inbox / ".musicdrop-ledger.json")
    _reg, finished = _wire(FakeImportRunner(applied=[_landed(0, kept)]), ledger, inbox)
    _on_state(monkeypatch, inbox, ledger)
    client = TestClient(app)
    _review_one(client, "Kept", finished)
    return inbox, kept, ledger, client


# ----- remember -----


def test_a_landed_review_records_the_folder_and_every_surface_hides_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _inbox_dir, kept, ledger, client = _review_kept(tmp_path, monkeypatch)

    (row,) = ledger.entries()
    st = kept.stat()
    assert (row.path, row.outcome, row.mtime, row.size) == (
        str(kept),
        "imported",
        st.st_mtime,
        st.st_size,
    )
    reset_registry(runner=(runner := FakeImportRunner()))
    assert _surfaces(client, runner) == ({"Other"}, 1, ["Other"])


def test_a_file_moved_into_it_lists_it_again_and_its_webhook_is_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ACCEPT side: a re-download adds a file, so the identity moves."""
    inbox, kept, ledger, client = _review_kept(tmp_path, monkeypatch)
    queue = AcquisitionQueue(
        import_registry=ImportJobRegistry(runner=FakeImportRunner()),
        ledger=ledger,
        inbox_dir=inbox,
    )
    queue.enqueue(kept)  # the same webhook again, folder unchanged
    assert queue.status().queued == 0

    arrived = tmp_path / "02 track.flac"
    arrived.write_bytes(b"\0")
    arrived.rename(kept / arrived.name)

    reset_registry(runner=(runner := FakeImportRunner()))
    # Listed and counted again; Review all waits for it to settle, as for any
    # folder still receiving files.
    assert _surfaces(client, runner) == ({"Kept", "Other"}, 2, ["Other"])
    queue.enqueue(kept)
    assert queue.status().queued == 1


def test_review_all_records_only_the_folder_whose_album_landed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Grouped by whole names: ``Album 2``'s skipped album is not ``Album``'s."""
    inbox = _inbox(tmp_path)
    landed = _album(inbox, "Album")
    skipped = _album(inbox, "Album 2")
    ledger = AcquisitionLedger(inbox / ".musicdrop-ledger.json")
    runner = FakeImportRunner(applied=[_landed(0, landed), _skipped(1, skipped)])
    _reg, finished = _wire(runner, ledger, inbox)
    _on_state(monkeypatch, inbox, ledger)

    resp = TestClient(app).post("/api/acquisition/review-inbox")
    assert resp.json()["pending"] == 2
    finished.wait()

    assert _rows(ledger) == {str(landed): "imported"}


def test_a_ledger_that_cannot_be_written_leaves_the_run_done(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The folder simply stays listed, the safe side; the run is not failed for it."""
    inbox = _inbox(tmp_path)
    folder = _album(inbox, "Unwritten")
    reg, finished = _wire(
        FakeImportRunner(applied=[_landed(0, folder)]),
        AcquisitionLedger(inbox / ".musicdrop-ledger.json"),
        inbox,
    )

    def refuse(_folders: list[str]) -> None:
        raise PermissionError(13, "Permission denied")

    reg.attach_import_recorder(refuse)
    with caplog.at_level("WARNING", logger="uvicorn.error"):
        job_id = reg.start(str(folder))
        finished.wait()

    assert reg.state(job_id).phase == "done"
    assert "could not record the imported folders" in caplog.text


def test_a_folder_with_one_album_skipped_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every album the run fed from the folder must land, not just one."""
    inbox = _inbox(tmp_path)
    disc1 = _album(inbox, "Two", "CD1")
    disc2 = _album(inbox, "Two", "CD2")
    os.utime(inbox / "Two", (_SETTLED, _SETTLED))
    ledger = AcquisitionLedger(inbox / ".musicdrop-ledger.json")
    runner = FakeImportRunner(applied=[_landed(0, disc1), _skipped(1, disc2)])
    _reg, finished = _wire(runner, ledger, inbox)
    _on_state(monkeypatch, inbox, ledger)
    client = TestClient(app)

    _review_one(client, "Two", finished)

    assert ledger.entries() == []
    reset_registry(runner=(listing := FakeImportRunner()))
    assert _surfaces(client, listing) == ({"Two"}, 1, ["Two"])


def test_add_from_folder_records_a_slskd_folder_and_leaves_any_other_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any start, by location (design call 1). The typed path keeps its trailing
    slash: beets normalises it, so the feed's folder does not end in one."""
    inbox = _inbox(tmp_path)
    inside = _album(inbox, "Typed")
    outside = _album(tmp_path.resolve() / "elsewhere", "Typed")
    ledger = AcquisitionLedger(inbox / ".musicdrop-ledger.json")
    client = TestClient(app)

    _reg, finished = _wire(FakeImportRunner(applied=[_landed(0, outside)]), ledger, inbox)
    assert client.post("/api/import", json={"path": f"{outside}/"}).status_code == 202
    finished.wait()
    assert ledger.entries() == []

    _reg, finished = _wire(FakeImportRunner(applied=[_landed(0, inside)]), ledger, inbox)
    assert client.post("/api/import", json={"path": f"{inside}/"}).status_code == 202
    finished.wait()
    assert _rows(ledger) == {str(inside): "imported"}


def _apply_one(
    tmp_path: Path, inbox: Path, decision: BankDecision, outcome: AlbumOutcome
) -> tuple[AcquisitionLedger, str]:
    """Run one decided bank row through the real apply runner; return its end status."""
    folder = Path(outcome.folder)
    bank_dir = tmp_path / "bank"
    item = store.create_item(
        bank_dir,
        folder=str(folder),
        source="inbox",
        reason="no_match",
        fingerprint=folder_fingerprint(folder),
    )
    store.decide_item(bank_dir, item.id, decision)
    ledger = AcquisitionLedger(inbox / ".musicdrop-ledger.json")
    reg, finished = _wire(FakeImportRunner(applied=[outcome]), ledger, inbox)

    def no_library() -> LibraryHandle:
        raise AssertionError("this apply must not read the library")

    runner = BankApplyRunner(
        bank_dir=bank_dir,
        import_registry=reg,
        library=no_library,
        poll_interval=0.01,
        busy_backoff=0.02,
        idle_poll=0.05,
    )
    runner.start()
    try:
        finished.wait()
        deadline = time.monotonic() + 5.0
        row = store.get_item(bank_dir, item.id)
        while row is not None and row.status == "applying" and time.monotonic() < deadline:
            time.sleep(0.01)
            row = store.get_item(bank_dir, item.id)
    finally:
        runner.stop()
    assert row is not None
    return ledger, row.status


def test_a_bank_apply_that_lands_records_the_folder(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    folder = _album(inbox, "Banked")

    ledger, status = _apply_one(tmp_path, inbox, BankDecision(action="asis"), _landed(0, folder))

    assert status == "done"
    assert _rows(ledger) == {str(folder): "imported"}


def test_a_skip_new_apply_is_done_and_records_nothing(tmp_path: Path) -> None:
    """The row ends ``done`` (the decision was carried out) and nothing landed."""
    inbox = _inbox(tmp_path)
    folder = _album(inbox, "Kept Out")
    duplicate = _skipped(0, folder).model_copy(
        update={"status": AlbumOutcomeStatus.needs_dup_resolution}
    )

    ledger, status = _apply_one(
        tmp_path,
        inbox,
        BankDecision(action="duplicate", duplicate_action=DuplicateAction.skip_new),
        duplicate,
    )

    assert status == "done"
    assert ledger.entries() == []


# ----- controls: what records nothing, and what never hides -----


class _CutShort(FakeImportRunner):
    """Its one album lands, then the stop is raised at the next album's hook,
    before that album reached the feed: the feed alone reads "all landed"."""

    def _emit_canned(self, bridge: ImportBridge) -> None:
        super()._emit_canned(bridge)
        bridge.abort_now()


class _FailsAfterLanding(FakeImportRunner):
    """Lands its album, then the worker dies (``on_error``, never ``on_finish``)."""

    def run(
        self,
        paths: list[str],
        bridge: ImportBridge,
        on_finish: Callable[[], None],
        on_error: Callable[[str], None],
        options: ImportOptions | None = None,
        directive: object = None,
    ) -> None:
        def target() -> None:
            self._emit_canned(bridge)
            on_error("boom")

        threading.Thread(target=target, daemon=True).start()


def test_a_run_cut_short_after_its_last_album_landed_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = _inbox(tmp_path)
    folder = _album(inbox, "Stopped")
    ledger = AcquisitionLedger(inbox / ".musicdrop-ledger.json")
    reg, finished = _wire(_CutShort(applied=[_landed(0, folder)]), ledger, inbox)
    _on_state(monkeypatch, inbox, ledger)

    job_id = _review_one(TestClient(app), "Stopped", finished)

    assert reg.job_aborted(job_id)
    assert reg.state(job_id).progress.applied == 1
    assert ledger.entries() == []


def test_a_failed_run_records_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inbox = _inbox(tmp_path)
    folder = _album(inbox, "Failed")
    ledger = AcquisitionLedger(inbox / ".musicdrop-ledger.json")
    runner = _FailsAfterLanding(applied=[_landed(0, folder)])
    _reg, finished = _wire(runner, ledger, inbox)
    _on_state(monkeypatch, inbox, ledger)

    _review_one(TestClient(app), "Failed", finished)

    assert ledger.entries() == []


def test_a_sweep_of_a_slskd_folder_records_nothing(tmp_path: Path) -> None:
    """A sweep keeps no per-album feed, so there is nothing to judge."""
    inbox = _inbox(tmp_path)
    folder = _album(inbox, "Swept")
    ledger = AcquisitionLedger(inbox / ".musicdrop-ledger.json")
    reg, finished = _wire(FakeImportRunner(applied=[_landed(0, folder)]), ledger, inbox)

    reg.start(str(folder), options=ImportOptions(sweep=True))
    finished.wait()

    assert ledger.entries() == []


@pytest.mark.parametrize("outcome", ["set_aside", "failed"])
def test_a_set_aside_or_failed_row_never_hides(
    outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = _inbox(tmp_path)
    folder = _album(inbox, "Waiting")
    ledger = AcquisitionLedger(inbox / ".musicdrop-ledger.json")
    ledger.mark(folder, outcome=outcome)  # type: ignore[arg-type]  # the parametrized literal
    _on_state(monkeypatch, inbox, ledger)
    reset_registry(runner=(runner := FakeImportRunner()))

    assert _surfaces(TestClient(app), runner) == ({"Waiting"}, 1, ["Waiting"])


def test_a_record_hides_only_its_own_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whole names: ``X`` does not hide ``X 2``, and ``Y/CD1`` does not hide ``Y``."""
    inbox = _inbox(tmp_path)
    x = _album(inbox, "X")
    _album(inbox, "X 2")
    disc = _album(inbox, "Y", "CD1")
    os.utime(inbox / "Y", (_SETTLED, _SETTLED))
    ledger = AcquisitionLedger(inbox / ".musicdrop-ledger.json")
    ledger.mark(x, outcome="imported")
    ledger.mark(disc, outcome="imported")
    _on_state(monkeypatch, inbox, ledger)
    reset_registry(runner=(runner := FakeImportRunner()))

    assert _surfaces(TestClient(app), runner) == ({"X 2", "Y"}, 2, ["X 2", "Y"])


def test_a_record_outside_slskd_folder_is_never_written(tmp_path: Path) -> None:
    """The recorder keeps what ``contain(strict=True)`` admits: not slskd's
    folder itself, not a folder beside it, not a link out of it."""
    inbox = _inbox(tmp_path)
    beside = _album(tmp_path.resolve(), "inbox 2")
    (inbox / "Link").symlink_to(beside)
    ledger = AcquisitionLedger(inbox / ".musicdrop-ledger.json")

    record_imported(ledger, inbox, [str(inbox), str(beside), str(inbox / "Link")])

    assert ledger.entries() == []


def test_the_lifespan_attaches_the_recorder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real lifespan: its ledger, its resolved inbox, its registry wiring."""
    music = tmp_path / "music"
    music.mkdir()
    beets = tmp_path / "beets"
    beets.mkdir()
    (beets / "config.yaml").write_text(f"directory: {music}\nlibrary: library.db\n")
    monkeypatch.setattr(settings, "beets_dir", str(beets))
    inbox = beets.resolve() / "inbox"
    kept = _album(inbox, "Kept")
    runner = FakeImportRunner(applied=[_landed(0, kept)])
    reset_registry(runner=runner)
    app.dependency_overrides.clear()

    with TestClient(app) as client:
        resp = client.post("/api/acquisition/inbox/items/import", json={"name": "Kept"})
        assert resp.status_code == 200, resp.text
        deadline = time.monotonic() + 5.0
        names: set[str] = {"Kept"}
        while "Kept" in names and time.monotonic() < deadline:
            time.sleep(0.02)
            items = client.get("/api/acquisition/inbox/items").json()["items"]
            names = {item["name"] for item in items}
        assert names == set()
        ledger: AcquisitionLedger = app.state.acquisition_ledger
        assert _rows(ledger) == {str(kept): "imported"}


# ----- the engine pin: what the identity survives -----


def test_a_beets_tag_write_through_a_hardlink_leaves_the_folder_identity(
    tmp_path: Path,
) -> None:
    """#52: the record survives a tag write because the file is rewritten IN
    PLACE. beets writes through ``Item.write`` -> ``MediaFile.save``, which
    mutagen does on the open file. A ``mediafile``/``mutagen`` that switched to
    write-a-temp-and-rename would add and remove an entry in the download's
    folder, move its mtime and relist every kept folder after each tag write:
    this fails first.
    """
    from beets.library import Item

    download = tmp_path / "downloads" / "Album"
    download.mkdir(parents=True)
    source = download / "01 track.flac"
    shutil.copyfile(Path(__file__).parent / "fixtures" / "silent.flac", source)
    os.utime(download, (_SETTLED, _SETTLED))
    library = tmp_path / "music" / "01 track.flac"
    library.parent.mkdir()
    os.link(source, library)
    before = download.stat()
    size_before = source.stat().st_size

    item = Item.from_path(str(library))
    item.lyrics = "la " * 20000  # grows the file, so mutagen must resize it
    item.write()

    after = download.stat()
    assert (after.st_mtime, after.st_size) == (before.st_mtime, before.st_size)
    # ...and the write did reach the download: same inode, new bytes.
    assert source.stat().st_ino == library.stat().st_ino
    assert source.stat().st_size > size_before
