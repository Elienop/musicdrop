"""The atomic, identity-keyed import ledger.

Records which inbox folders the queue has already handled so a webhook retry (or
a restart) never re-imports the same drop. Identity is ``(path, st_mtime,
st_size)`` so a *re-download* of a same-named folder (different mtime/size) reads
as new. Persisted via the shared ``write_atomic_text`` recipe; a corrupt file
degrades to an empty ledger rather than crashing the seam.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from app.acquisition.ledger import AcquisitionLedger


def test_ledger_round_trips_and_marks(tmp_path: Path) -> None:
    led = AcquisitionLedger(tmp_path / "ledger.json")
    folder = tmp_path / "inbox" / "Album"
    folder.mkdir(parents=True)
    assert led.seen(folder) is False
    led.mark(folder, outcome="imported")
    assert led.seen(folder) is True
    # Reload from disk: the mark must have been persisted atomically.
    led2 = AcquisitionLedger(tmp_path / "ledger.json")
    assert led2.seen(folder) is True
    assert [e.path for e in led2.entries()] == [str(folder)]
    assert led2.entries()[0].outcome == "imported"


def test_ledger_detects_changed_folder(tmp_path: Path) -> None:
    led = AcquisitionLedger(tmp_path / "l.json")
    folder = tmp_path / "Album"
    folder.mkdir()
    led.mark(folder, outcome="set_aside")
    assert led.seen(folder) is True
    # Bump the folder's mtime forward: identity no longer matches -> treat as new.
    future = time.time() + 1000
    os.utime(folder, (future, future))
    assert led.seen(folder) is False


def test_ledger_unmarked_folder_is_not_seen(tmp_path: Path) -> None:
    led = AcquisitionLedger(tmp_path / "l.json")
    folder = tmp_path / "Album"
    folder.mkdir()
    assert led.seen(folder) is False


def test_ledger_missing_folder_is_not_seen(tmp_path: Path) -> None:
    led = AcquisitionLedger(tmp_path / "l.json")
    folder = tmp_path / "Album"
    folder.mkdir()
    led.mark(folder, outcome="imported")
    # Folder vanishes: stat() raises OSError -> identity is None -> not seen.
    folder.rmdir()
    assert led.seen(folder) is False


def test_ledger_remark_replaces_entry(tmp_path: Path) -> None:
    led = AcquisitionLedger(tmp_path / "l.json")
    folder = tmp_path / "Album"
    folder.mkdir()
    led.mark(folder, outcome="set_aside")
    led.mark(folder, outcome="imported")
    entries = led.entries()
    assert len(entries) == 1
    assert entries[0].outcome == "imported"


def test_ledger_survives_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "l.json"
    path.write_text("{not json")
    assert AcquisitionLedger(path).entries() == []


def test_ledger_survives_missing_file(tmp_path: Path) -> None:
    assert AcquisitionLedger(tmp_path / "never-written.json").entries() == []
