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

from app.beets.disk_sync import plan_disk_sync
from app.beets.library import LibraryRootUnavailableError, _require_id


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
    assert plan.will_remove == 0
    assert plan.will_update == 0
    assert plan.emptied_total == 0
    assert plan.read_errors == []
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
    assert plan.emptied_albums
    assert album.album in plan.emptied_albums[0].label
    assert plan.emptied_albums[0].track_count == 3  # the fixture album's rows
    assert plan.emptied_albums[0].path == os.path.join("Radiohead", "In Rainbows")


def _add_phantom_row(lib: Library, folder: str, title: str) -> str:
    """Seed a SECOND album row carrying one duplicate item, in its own folder.

    Reproduces the incident: two album rows share the label
    "Radiohead - In Rainbows" — the real 3-track album and a phantom holding a
    single duplicate track. Returns the phantom item's absolute path.
    """
    import shutil

    from beets.library import Item

    sample = os.path.join(os.path.dirname(__file__), "fixtures", "silent.flac")
    base = os.path.join(os.fsdecode(lib.directory), *folder.split("/"))
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, f"03 {title}.flac")
    shutil.copyfile(sample, path)
    item = Item(
        album="In Rainbows", albumartist="Radiohead", artist="Radiohead", title=title, track=3
    )
    item.path = os.fsencode(path)
    lib.add_album([item]).store()
    return path


def test_plan_emptied_row_identifies_which_album_row(edit_lib: Library) -> None:
    """Two album ROWS can carry the same label, so a bare label cannot say which
    one gets pruned — the user read "1 album becomes empty" as their whole
    17-track album vanishing. Each emptied row must carry the track count and
    the folder of THAT row, not of its same-named twin."""
    twin_track = _items(edit_lib)[0]  # a track of the real 3-track album
    path = _add_phantom_row(edit_lib, "Radiohead/In Rainbows (1)", "Nude")
    os.remove(path)  # the phantom row's single file is gone...
    os.remove(twin_track.path)  # ...and one track of the 3-track twin

    plan = plan_disk_sync(edit_lib)

    assert plan.will_remove == 2  # library-wide; the twin keeps 2 rows, survives
    assert plan.emptied_total == 1
    row = plan.emptied_albums[0]
    assert row.label == "Radiohead - In Rainbows"  # ambiguous on its own
    assert row.track_count == 1  # the phantom row, NOT the 3-track twin
    assert row.path == os.path.join("Radiohead", "In Rainbows (1)")


def test_plan_lists_changed_tags_with_field_names(edit_lib: Library) -> None:
    item = _items(edit_lib)[0]
    _bump_title_on_disk(item, "Renamed On Disk")
    plan = plan_disk_sync(edit_lib)
    assert plan.will_update == 1
    assert plan.changes
    assert "title" in plan.changes[0].fields
    # read-only: the DB still has the old title
    row = edit_lib.get_item(item.id)
    assert row is not None
    assert row.title != "Renamed On Disk"


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
    assert len(plan.read_errors) == 1
    assert plan.read_errors[0].error
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
    album_id = _require_id(album.id)
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
    refreshed = edit_lib.get_item(_require_id(item.id))
    assert refreshed is not None
    assert refreshed.title == "Fresh Title From Disk"
    assert refreshed.year == 1987
    updated = [o for o in outcomes if o.status == "updated"]
    assert updated
    assert any("title" in o.fields for o in updated)
    # Album-level realign: year is an Album.item_keys field.
    realigned = edit_lib.get_album(_require_id(album.id))
    assert realigned is not None
    assert realigned.year == 1987


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
    assert row is not None
    assert row.albumartist == old_albumartist


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
    assert good_row is not None
    assert good_row.title == "Still Synced"


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


def test_run_aborts_when_root_present_but_empty(edit_lib: Library) -> None:
    """A dropped NAS/SMB/NFS mount typically leaves music_dir PRESENT but empty
    (the kernel keeps the mountpoint dir), so os.path.isdir stays True. The
    isdir-only guard would then read every file as deleted and wipe the DB. An
    empty root while the DB has items is a dropped mount — abort, remove nothing."""
    import shutil

    from app.beets.disk_sync import run_disk_sync

    n_before = len(list(edit_lib.items()))
    root = os.fsdecode(edit_lib.directory)
    for entry in os.listdir(root):  # empty the mountpoint (files + artist/album dirs)
        p = os.path.join(root, entry)
        shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    assert os.path.isdir(root)  # present but empty
    assert not os.listdir(root)  # present but empty
    with pytest.raises(LibraryRootUnavailableError):
        run_disk_sync(
            edit_lib, on_total=lambda n: None, on_item=lambda o: None, should_stop=lambda: False
        )
    assert len(list(edit_lib.items())) == n_before  # nothing removed


def test_run_aborts_when_root_drops_midrun(
    edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The nightmare-scenario guard must re-check the root when a file looks
    missing MID-run, not only once at the start. If the share unmounts partway
    through the sweep every remaining file looks deleted; without the re-check the
    loop removes thousands of DB rows in one pass. Abort instead — nothing removed."""
    import app.beets.disk_sync as ds
    from app.beets.disk_sync import run_disk_sync

    n_before = len(list(edit_lib.items()))
    # Mount dropped mid-run: every file now looks gone...
    monkeypatch.setattr(ds, "_file_missing", lambda item: True)
    # ...and the root check passes ONCE (start-of-run guard) then fails (the drop).
    calls = {"n": 0}

    def flaky_require_root(lib: object) -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            raise ds.LibraryRootUnavailableError("share unmounted mid-run")

    monkeypatch.setattr(ds, "_require_root", flaky_require_root)
    with pytest.raises(ds.LibraryRootUnavailableError):
        run_disk_sync(
            edit_lib, on_total=lambda n: None, on_item=lambda o: None, should_stop=lambda: False
        )
    assert len(list(edit_lib.items())) == n_before  # no mass-removal


def _new_multi_disc_album(lib: Library) -> list[bytes]:
    """Seed ONE new album row whose items span sibling CD folders
    (``Dualband/Split/CD1`` + ``/CD2``) — the disc-bearing ``paths:``
    layout. ONE ``add_album`` call so beets groups all four items onto the
    same album row (two calls would create two same-labelled rows). Returns
    the absolute file paths (bytes).
    """
    import shutil

    from beets.library import Item

    sample = os.path.join(os.path.dirname(__file__), "fixtures", "silent.flac")
    items = []
    paths = []
    n = 0
    for disc in ("CD1", "CD2"):
        base = os.path.join(os.fsdecode(lib.directory), "Dualband", "Split", disc)
        os.makedirs(base, exist_ok=True)
        for title in ("A", "B"):
            n += 1
            path = os.path.join(base, f"{n:02d} {title}.flac")
            shutil.copyfile(sample, path)
            item = Item(
                album="Split", albumartist="Dualband", artist="Dualband", title=title, track=n
            )
            item.path = os.fsencode(path)
            items.append(item)
            paths.append(os.fsencode(path))
    lib.add_album(items).store()
    return paths


def test_emptied_row_shows_album_root_for_multi_disc_album(edit_lib: Library) -> None:
    """A disc-bearing ``paths:`` template puts ONE album's items in sibling
    CD folders (Artist/Album/CD1, .../CD2). The emptied row must show the
    album ROOT, not the first disc's folder — first-dir-wins read as "the
    album is in CD1"."""
    paths = _new_multi_disc_album(edit_lib)
    for path in paths:
        os.remove(path)  # the whole album is gone from disk

    plan = plan_disk_sync(edit_lib)

    rows = [row for row in plan.emptied_albums if "Dualband" in row.label]
    assert rows, "the emptied album must be in the preview"
    assert rows[0].track_count == 4
    assert rows[0].path == os.path.join("Dualband", "Split")  # the album ROOT


def test_emptied_row_item_at_library_root_shows_dot(edit_lib: Library) -> None:
    """An item sitting at the MUSIC DIR ROOT has the display dir "." — pin the
    defensive branch so the commonpath fold (which maps POSIX commonpath's
    ``""`` back to ``"."``) cannot regress it to an empty path."""
    import shutil

    from beets.library import Item

    sample = os.path.join(os.path.dirname(__file__), "fixtures", "silent.flac")
    path = os.path.join(os.fsdecode(edit_lib.directory), "01 Root.flac")
    shutil.copyfile(sample, path)
    item = Item(album="Rootsolo", albumartist="Rootsolo", artist="Rootsolo", title="Root", track=1)
    item.path = os.fsencode(path)
    edit_lib.add_album([item]).store()
    os.remove(path)

    plan = plan_disk_sync(edit_lib)

    rows = [row for row in plan.emptied_albums if "Rootsolo" in row.label]
    assert rows
    assert rows[0].path == "."
