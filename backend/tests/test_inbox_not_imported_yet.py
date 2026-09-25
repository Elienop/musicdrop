"""One place to decide each album: "Not imported yet" steps aside for the bank.

While a folder's bank row shows in "Waiting for review" (the store's own
``ACTIVE_STATUSES``), the inbox list does not show it, the badge does not count
it and Review all does not hand it over. ``ignored`` and ``done`` let it come
back. One shared rule decides all three, off one bank read per request.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.acquisition.inbox as inbox_mod
from app.acquisition.inbox import count_pending, list_inbox, settled_folders
from app.acquisition.ledger import AcquisitionLedger
from app.acquisition.queue import AcquisitionQueue
from app.bank import store
from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import ImportJobRegistry, reset_registry
from app.main import app
from app.models.bank import BankDecision, BankStatus

_SETTLED = time.time() - 3600


def _album(inbox: Path, *parts: str) -> Path:
    """A settled album folder holding one audio file."""
    folder = inbox.joinpath(*parts)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "01 track.flac").write_bytes(b"\0")
    for path in [folder, *folder.rglob("*")]:
        os.utime(path, (_SETTLED, _SETTLED))
    return folder


def _bank(bank_dir: Path, folder: Path, status: BankStatus) -> str:
    """A bank row for ``folder`` in ``status``, reached the way the app gets there."""
    item_id = store.create_item(
        bank_dir, folder=str(folder), source="inbox", reason="no_match", fingerprint="f" * 64
    ).id
    if status in ("queued", "applying", "done"):
        store.decide_item(bank_dir, item_id, BankDecision(action="asis"))
    if status in ("applying", "done"):
        store.set_status(bank_dir, item_id, "applying")
    if status == "done":
        store.set_status(bank_dir, item_id, "done", album_id=1)
    elif status in ("failed", "stale", "ignored"):
        store.set_status(bank_dir, item_id, status)
    row = store.get_item(bank_dir, item_id)
    assert row is not None
    assert row.status == status
    return item_id


def _surfaces(inbox: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[set[str], int, list[str]]:
    """What the three surfaces show: the list's names, the badge, Review all's folders."""
    monkeypatch.setattr(app.state, "inbox_dir", inbox, raising=False)
    fake = FakeImportRunner(parked=[])
    reset_registry(runner=fake)
    client = TestClient(app)
    listed = {item["name"] for item in client.get("/api/acquisition/inbox/items").json()["items"]}
    badge = client.get("/api/acquisition/status").json()["inbox_pending"]
    client.post("/api/acquisition/review-inbox")
    handed = [Path(p).name for p in fake.received_paths or []]
    return listed, badge, handed


# Named here, not read from ``store.ACTIVE_STATUSES``: a case derived from the
# set under test would vanish with the status it should catch.
@pytest.mark.parametrize("status", ["needs_review", "queued", "applying", "failed", "stale"])
def test_a_folder_waiting_for_review_leaves_all_three_surfaces(
    status: BankStatus,
    tmp_path: Path,
    inbox_bank_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox = tmp_path / "inbox"
    _bank(inbox_bank_dir, _album(inbox, "Held"), status)
    _album(inbox, "Free")  # the control: the surfaces are not simply empty

    assert _surfaces(inbox, monkeypatch) == ({"Free"}, 1, ["Free"])


@pytest.mark.parametrize("status", ["ignored", "done"])
def test_an_ignored_or_done_row_lets_the_folder_come_back(
    status: BankStatus,
    tmp_path: Path,
    inbox_bank_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox = tmp_path / "inbox"
    _bank(inbox_bank_dir, _album(inbox, "Back"), status)

    assert _surfaces(inbox, monkeypatch) == ({"Back"}, 1, ["Back"])


def test_a_removed_row_lets_the_folder_come_back(
    tmp_path: Path, inbox_bank_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = tmp_path / "inbox"
    item_id = _bank(inbox_bank_dir, _album(inbox, "Back"), "needs_review")
    assert _surfaces(inbox, monkeypatch) == (set(), 0, [])

    assert store.delete_item(inbox_bank_dir, item_id)
    assert _surfaces(inbox, monkeypatch) == ({"Back"}, 1, ["Back"])


def test_a_row_holds_whole_names_only(
    tmp_path: Path, inbox_bank_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = tmp_path / "inbox"
    _bank(inbox_bank_dir, _album(inbox, "Album"), "needs_review")
    _album(inbox, "Album 2")
    _album(inbox, "Albu")

    assert _surfaces(inbox, monkeypatch) == ({"Album 2", "Albu"}, 2, ["Albu", "Album 2"])


def test_a_row_below_the_entry_holds_the_entry(
    tmp_path: Path, inbox_bank_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drain banks the album folder, which can sit below the listed entry."""
    inbox = tmp_path / "inbox"
    disc = _album(inbox, "X", "CD1")
    _album(inbox, "X", "CD2")
    os.utime(inbox / "X", (_SETTLED, _SETTLED))  # settled, so Review all would take it
    _bank(inbox_bank_dir, disc, "needs_review")
    _album(inbox, "Y")

    assert _surfaces(inbox, monkeypatch) == ({"Y"}, 1, ["Y"])


def test_ignore_is_not_now_and_the_same_webhook_does_not_bring_it_back(
    tmp_path: Path, inbox_bank_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#77: an ignored album's folder is listed again, and nothing imports it on its own."""
    inbox = tmp_path / "inbox"
    folder = _album(inbox, "Unsure")
    other = _album(inbox, "Fresh")
    ledger = AcquisitionLedger(inbox / ".musicdrop-ledger.json")
    # What a drain run that banked the album leaves behind: its ledger row and a bank row.
    ledger.mark(folder, outcome="set_aside")
    item_id = _bank(inbox_bank_dir, folder, "needs_review")
    assert store.bulk_ignore(inbox_bank_dir, [item_id]) == 1

    listed, _badge, _handed = _surfaces(inbox, monkeypatch)
    assert listed == {"Unsure", "Fresh"}

    queue = AcquisitionQueue(
        import_registry=ImportJobRegistry(runner=FakeImportRunner()),
        ledger=ledger,
        inbox_dir=inbox,
    )
    queue.enqueue(folder)  # the same webhook again, folder unchanged
    assert queue.status().queued == 0
    queue.enqueue(other)  # the control: a folder the ledger never saw is taken
    assert queue.status().queued == 1


def test_the_list_the_badge_and_review_all_agree(tmp_path: Path, inbox_bank_dir: Path) -> None:
    """Parity on one inbox holding every kind of entry the shared rule decides."""
    inbox = tmp_path / "inbox"
    _album(inbox, "Plain")
    _album(inbox, "Artist", "Nested")
    _bank(inbox_bank_dir, _album(inbox, "Held"), "failed")
    _bank(inbox_bank_dir, _album(inbox, "Ignored"), "ignored")
    _album(inbox, ".hidden")
    (inbox / "Empty").mkdir()
    (inbox / "loose.flac").write_bytes(b"\0")
    (inbox / "Link").symlink_to(inbox / "Plain")
    (inbox / ".musicdrop-ledger.json").write_text("{}")

    held = inbox_mod.bank_held_names(inbox, inbox_bank_dir)
    listed = [item.name for item in list_inbox(inbox, None, held=held)]
    settled = settled_folders(inbox, held=held, settle_seconds=60, now=time.time())

    assert sorted(listed) == ["Artist", "Ignored", "Plain"]
    assert count_pending(inbox, held=held) == len(listed)
    assert {folder.name for folder in settled} <= set(listed)


def test_each_route_reads_the_bank_once(
    tmp_path: Path, inbox_bank_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bank query per request, whatever the number of folders or reads."""
    inbox = tmp_path / "inbox"
    for name in ("A", "B", "C"):
        _album(inbox, name)
    _bank(inbox_bank_dir, inbox / "B", "needs_review")
    reads: list[Path] = []
    real = store.active_folders_under

    def counting(bank_dir: Path, root: Path) -> list[str]:
        reads.append(root)
        return real(bank_dir, root)

    monkeypatch.setattr(inbox_mod, "active_folders_under", counting)
    monkeypatch.setattr(app.state, "inbox_dir", inbox, raising=False)
    reset_registry(runner=FakeImportRunner(parked=[]))
    client = TestClient(app)
    for method, url in (
        ("GET", "/api/acquisition/inbox/items"),
        ("GET", "/api/acquisition/status"),
        ("POST", "/api/acquisition/review-inbox"),
    ):
        reads.clear()
        assert client.request(method, url).status_code == 200
        assert reads == [inbox], url


def test_the_store_answers_active_rows_strictly_inside_the_root_by_whole_names(
    tmp_path: Path,
) -> None:
    bank_dir = tmp_path / "bank"
    root = Path("/srv/in")
    for folder in ("/srv/in/A", "/srv/in/B/CD1", "/srv/in", "/srv/in-2/C", "/srv/in.x/D"):
        _bank(bank_dir, Path(folder), "needs_review")
    _bank(bank_dir, Path("/srv/in/Ignored"), "ignored")
    _bank(bank_dir, Path("/srv/in/Done"), "done")

    assert sorted(store.active_folders_under(bank_dir, root)) == ["/srv/in/A", "/srv/in/B/CD1"]
