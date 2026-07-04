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


def _run(lib: Library) -> tuple[list[Any], int]:
    from app.beets.disk_sync import run_disk_sync

    outcomes: list[Any] = []
    emptied = run_disk_sync(
        lib,
        on_total=lambda n: None,
        on_item=outcomes.append,
        should_stop=lambda: False,
    )
    return outcomes, emptied


def test_run_removes_missing_row_keeps_album_with_survivors(edit_lib: Library) -> None:
    victim = _items(edit_lib)[0]
    album_id = victim.album_id
    os.remove(victim.path)
    outcomes, emptied = _run(edit_lib)
    assert edit_lib.get_item(victim.id) is None
    assert edit_lib.get_album(album_id) is not None  # survivors keep the album
    assert emptied == 0
    assert [o.status for o in outcomes].count("removed") == 1


def test_run_prunes_album_when_all_files_gone(edit_lib: Library) -> None:
    album = next(iter(edit_lib.albums()))
    album_id = int(album.id)
    for it in album.items():
        os.remove(it.path)
    _outcomes, emptied = _run(edit_lib)
    assert edit_lib.get_album(album_id) is None
    assert emptied == 1


def test_run_refreshes_changed_tags_and_realigns_album(edit_lib: Library) -> None:
    # Change a track-level field (title) on one item AND an album-inherited
    # field (year) on EVERY item of its album: the run must refresh the item
    # rows AND realign the Album row (beets copies Album.item_keys from the
    # album's FIRST item, so all items must agree for a deterministic assert).
    album = next(iter(edit_lib.albums()))
    item = sorted(album.items(), key=lambda i: os.fsdecode(i.path))[0]
    future = time.time() + 10
    for it in album.items():
        p = os.fsdecode(it.path)
        mf = MediaFile(p)
        if it.id == item.id:
            mf.title = "Fresh Title From Disk"
        mf.year = 1987
        mf.save()
        os.utime(p, (future, future))
    outcomes, _ = _run(edit_lib)
    refreshed = edit_lib.get_item(item.id)
    assert refreshed is not None
    assert refreshed.title == "Fresh Title From Disk" and refreshed.year == 1987
    updated = [o for o in outcomes if o.status == "updated"]
    assert updated and any("title" in o.fields for o in updated)
    # Album-level realign: year is an Album.item_keys field.
    realigned = edit_lib.get_album(int(album.id))
    assert realigned is not None and realigned.year == 1987


def test_run_realign_keeps_per_track_album_fields(edit_lib: Library) -> None:
    """Regression: the album realign must NOT clobber per-track album-level
    fields with track 1's values. beets' Album.store(inherit=True) propagates
    them onto every track AND zeroes each touched track's mtime (models.py
    "Reset mtime on dirty"), making the same tracks re-sync forever."""
    items = _items(edit_lib)
    genres = {0: ["Rock"], 1: ["Hard Rock"], 2: ["Rock"]}
    for idx, item in enumerate(items):
        path = os.fsdecode(item.path)
        mf = MediaFile(path)
        mf.genres = genres[idx]
        mf.save()
    # Re-rest: DB rows honestly match each file (incl. heterogeneous genres).
    for item in edit_lib.items():
        item.read()
        item.store()
    # Real drift on the heterogeneous track -> run 1 marks its album affected.
    hetero = _items(edit_lib)[1]
    _bump_title_on_disk(hetero, "Bodysnatchers (remaster)")

    outcomes, _ = _run(edit_lib)
    assert [o.status for o in outcomes].count("updated") == 1

    row = edit_lib.get_item(hetero.id)
    assert row is not None
    assert list(row.genres) == ["Hard Rock"]  # its OWN file's value, not track 1's
    assert row.mtime == int(os.path.getmtime(os.fsdecode(row.path)))  # gate shut

    # The loop is dead: second run all-unchanged, preview quiet.
    outcomes2, _ = _run(edit_lib)
    assert {o.status for o in outcomes2} == {"unchanged"}
    assert plan_disk_sync(edit_lib).will_update == 0


def test_run_persists_mtime_so_second_run_is_quiet(edit_lib: Library) -> None:
    item = _items(edit_lib)[0]
    item_id = item.id
    path = os.fsdecode(item.path)
    _bump_title_on_disk(item, "Once")
    _run(edit_lib)
    # Run 1 must persist the file's on-disk mtime into the DB row. Otherwise the
    # mtime gate (current_mtime() <= item.mtime) never shuts and this file is
    # re-read on EVERY later sync forever — the exact I/O the gate exists to
    # prevent. Pin it: the stored mtime now equals the file's current mtime.
    row = edit_lib.get_item(item_id)
    assert row is not None
    assert row.mtime == int(os.path.getmtime(path))
    outcomes, _ = _run(edit_lib)
    assert all(o.status == "unchanged" for o in outcomes)


def test_run_albumartist_special_case_preserved(edit_lib: Library) -> None:
    # File on disk carries NO albumartist; DB row has albumartist == artist.
    item = _items(edit_lib)[0]
    path = os.fsdecode(item.path)
    mf = MediaFile(path)
    mf.albumartist = None
    mf.save()
    future = time.time() + 10
    os.utime(path, (future, future))
    old_albumartist = item.albumartist
    assert old_albumartist == item.artist  # fixture precondition
    _run(edit_lib)
    row = edit_lib.get_item(item.id)
    assert row is not None and row.albumartist == old_albumartist


def test_run_read_error_is_isolated(edit_lib: Library) -> None:
    items = _items(edit_lib)
    bad, good = items[0], items[1]
    with open(os.fsdecode(bad.path), "wb") as fh:
        fh.write(b"garbage")
    future = time.time() + 10
    os.utime(bad.path, (future, future))
    _bump_title_on_disk(good, "Still Synced")
    outcomes, _ = _run(edit_lib)
    statuses = {o.status for o in outcomes}
    assert "read_error" in statuses
    good_row = edit_lib.get_item(good.id)
    assert good_row is not None and good_row.title == "Still Synced"


def test_run_honors_stop(edit_lib: Library) -> None:
    for it in _items(edit_lib):
        os.remove(it.path)
    from app.beets.disk_sync import run_disk_sync

    calls = {"n": 0}

    def stop_after_one() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    run_disk_sync(
        edit_lib, on_total=lambda n: None, on_item=lambda o: None, should_stop=stop_after_one
    )
    # Stopped early: at least one row must survive.
    assert len(list(edit_lib.items())) >= 1


def test_run_fails_fast_when_root_missing(edit_lib: Library) -> None:
    import shutil

    from app.beets.disk_sync import run_disk_sync

    n_before = len(list(edit_lib.items()))
    shutil.rmtree(os.fsdecode(edit_lib.directory))
    with pytest.raises(LibraryRootUnavailableError):
        run_disk_sync(
            edit_lib, on_total=lambda n: None, on_item=lambda o: None, should_stop=lambda: False
        )
    assert len(list(edit_lib.items())) == n_before  # nothing removed
