"""Adapter tests for app/beets/disk_sync.py against a real hermetic library.

Uses the ``edit_lib`` fixture (real files with tags on disk). Mutating a file's
tags goes through mediafile.MediaFile + an explicit os.utime bump so the mtime
gate opens deterministically.
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest
from beets.library import Library
from mediafile import MediaFile

from app.beets.disk_sync import LibraryRootUnavailableError, plan_disk_sync


@pytest.fixture(autouse=True)
def _library_at_rest(edit_lib: Library) -> None:
    """Bring ``edit_lib`` to the state a real post-import library is in: tags
    written to disk and each file's mtime recorded in the DB (so the sync gate
    is shut at rest). The shared fixture seeds DB rows with ``mtime=0`` and
    never writes tags — without this, every item would look changed on disk.
    ``try_write`` writes the DB tags to the file and refreshes ``item.mtime``;
    ``store`` persists that mtime. Read-only baseline: disk matches the DB.
    """
    for item in edit_lib.items():
        item.try_write()
        item.store()


def _items(lib: Library) -> list[Any]:
    return sorted(lib.items(), key=lambda i: os.fsdecode(i.path))


def _bump_title_on_disk(item: Any, new_title: str) -> None:
    path = os.fsdecode(item.path)
    mf = MediaFile(path)
    mf.title = new_title
    mf.save()
    # Force the mtime PAST the DB-recorded value (same-second saves would be
    # skipped by the <= gate).
    future = time.time() + 10
    os.utime(path, (future, future))


def test_plan_empty_when_disk_matches_db(edit_lib: Library) -> None:
    plan = plan_disk_sync(edit_lib)
    assert plan.will_remove == 0 and plan.will_update == 0
    assert plan.emptied_total == 0 and plan.read_errors == []
    assert plan.total_items == len(list(edit_lib.items()))


def test_plan_lists_missing_file(edit_lib: Library) -> None:
    victim = _items(edit_lib)[0]
    os.remove(victim.path)
    plan = plan_disk_sync(edit_lib)
    assert plan.will_remove == 1
    assert len(plan.removals) == 1
    assert plan.removals[0].label  # "Artist — Title", non-empty
    # DB untouched by the plan (read-only):
    assert edit_lib.get_item(victim.id) is not None


def test_plan_flags_whole_album_as_emptied(edit_lib: Library) -> None:
    album = next(iter(edit_lib.albums()))
    for it in album.items():
        os.remove(it.path)
    plan = plan_disk_sync(edit_lib)
    assert plan.emptied_total == 1
    assert plan.emptied_albums and album.album in plan.emptied_albums[0]


def test_plan_lists_changed_tags_with_field_names(edit_lib: Library) -> None:
    item = _items(edit_lib)[0]
    _bump_title_on_disk(item, "Renamed On Disk")
    plan = plan_disk_sync(edit_lib)
    assert plan.will_update == 1
    assert plan.changes and "title" in plan.changes[0].fields
    # read-only: the DB still has the old title
    row = edit_lib.get_item(item.id)
    assert row is not None and row.title != "Renamed On Disk"


def test_plan_skips_untouched_mtime(edit_lib: Library) -> None:
    # Change tags WITHOUT advancing mtime past the DB value -> gate stays shut.
    item = _items(edit_lib)[0]
    path = os.fsdecode(item.path)
    mf = MediaFile(path)
    mf.title = "Sneaky Edit"
    mf.save()
    past = item.mtime - 100
    os.utime(path, (past, past))
    plan = plan_disk_sync(edit_lib)
    assert plan.will_update == 0


def test_plan_reports_unreadable_file(edit_lib: Library) -> None:
    item = _items(edit_lib)[0]
    path = os.fsdecode(item.path)
    with open(path, "wb") as fh:
        fh.write(b"not audio at all")
    future = time.time() + 10
    os.utime(path, (future, future))
    plan = plan_disk_sync(edit_lib)
    assert len(plan.read_errors) == 1 and plan.read_errors[0].error
    assert plan.will_update == 0


def test_plan_fails_fast_when_root_missing(
    edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    root = os.fsdecode(edit_lib.directory)
    shutil.rmtree(root)
    with pytest.raises(LibraryRootUnavailableError):
        plan_disk_sync(edit_lib)
