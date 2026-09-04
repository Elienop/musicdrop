"""Every library MOVER owes its playlists a `.m3u8` re-export.

The invariant under test: MusicDrop must never leave an export pointing at a path
it just changed or removed itself. The artist rename already had this collateral
(tests/test_rename_reexport.py, tests/test_rename_api.py); these pin it for the
other movers — album tag edit, album/artist delete, duplicate resolve (single and
batch), reorganize, disk sync, and the import's ``replace`` duplicate action.

Every test asserts the export's CONTENT, not merely that a file exists: an export
rewritten to the wrong path, or not rewritten at all, both leave a file on disk.
A moved track must show its NEW relative path; a removed one must lose its line
entirely (``m3u_entries`` drops an id that no longer resolves).
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from beets.library import Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.beets.library import LibraryHandle, _require_id
from app.main import app
from app.playlists import store
from app.playlists.reexport import export_dir_for, render_export
from app.playlists.store import StoredEntry, StoredPlaylist, get_playlists_dir
from tests.conftest import beets_dir_for, make_test_handle

# ----- staging helpers -----


def _stage_playlist(
    playlists_dir: Path, lib: Library, item_ids: list[int], *, name: str = "Mix"
) -> StoredPlaylist:
    """Create a playlist over ``item_ids`` AND write its `.m3u8` as it stands now.

    The pre-write is what makes these tests falsifiable: without it a missing
    re-export and a correct one are indistinguishable (no file either way, or a
    file written by some later mutation). With it, the export starts out holding
    the pre-operation paths and the assertion is about a CHANGE.
    """
    record = store.create_playlist(
        playlists_dir,
        name=name,
        entries=[StoredEntry(uid=f"u{n}", item_id=iid) for n, iid in enumerate(item_ids)],
    )
    render_export(record, lib, export_dir_for(lib))
    return record


def _export_text(lib: Library, playlist_id: str) -> str:
    path = export_dir_for(lib) / f"{playlist_id}.m3u8"
    return path.read_text(encoding="utf-8")


def _item_by_title(lib: Library, title: str) -> Any:
    return next(i for i in lib.items() if str(i.title) == title)


def _album_by_title(lib: Library, title: str) -> Any:
    return next(a for a in lib.albums() if str(a.album) == title)


def _first_item_of(lib: Library, album_id: int) -> Any:
    album = lib.get_album(album_id)
    assert album is not None  # narrows Album | None; the id came from the live report
    return next(iter(album.items()))


def _client_for(lib: Library, tmp_path: Path) -> Iterator[tuple[TestClient, Path, LibraryHandle]]:
    """A TestClient bound to ``lib`` with the playlist store under ``tmp_path``.

    ``get_playlists_dir`` is overridden rather than pointed at settings so the
    test never reads (or writes) the developer's real playlist store.
    """
    handle = make_test_handle(lib, beets_dir_for(tmp_path))
    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()
    app.state.beets_library = handle
    app.dependency_overrides[get_library] = lambda: handle
    app.dependency_overrides[get_playlists_dir] = lambda: playlists_dir
    try:
        yield TestClient(app), playlists_dir, handle
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def edit_client(edit_lib: Library, tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    for client, playlists_dir, _handle in _client_for(edit_lib, tmp_path):
        yield client, playlists_dir


@pytest.fixture
def dup_client(duplicates_lib: Library, tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    for client, playlists_dir, _handle in _client_for(duplicates_lib, tmp_path):
        yield client, playlists_dir


# ----- 1. album tag edit -----


def test_album_edit_reexports_the_tracks_it_moved(
    edit_client: tuple[TestClient, Path], edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A move-enabled edit rewrites the album folder, so the export must follow it."""
    import beets.ui

    monkeypatch.setattr(beets.ui, "should_move", lambda _opt: True)
    monkeypatch.setattr(beets.ui, "should_write", lambda _opt: True)
    client, playlists_dir = edit_client

    item = _item_by_title(edit_lib, "15 Step")
    record = _stage_playlist(playlists_dir, edit_lib, [_require_id(item.id)])
    assert "../Radiohead/In Rainbows/01 15 Step.flac" in _export_text(edit_lib, record.id)

    album_id = _require_id(_album_by_title(edit_lib, "In Rainbows").id)
    r = client.post(f"/api/albums/{album_id}/edit", json={"album": {"title": "In Rainbows (R)"}})

    assert r.status_code == 200
    assert r.json()["playlists_reexported"] == 1
    body = _export_text(edit_lib, record.id)
    assert "../Radiohead/In Rainbows (R)/01 15 Step.flac" in body
    assert "../Radiohead/In Rainbows/01 15 Step.flac" not in body


def test_album_edit_that_moves_nothing_reexports_nothing(
    edit_client: tuple[TestClient, Path], edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control arm: with moves off, no path changed, so no export may be
    touched and the count must be 0 — otherwise the wiring is keyed off the edit
    rather than off ``ItemWriteResult.moved`` and would churn every playlist."""
    import beets.ui

    monkeypatch.setattr(beets.ui, "should_move", lambda _opt: False)
    monkeypatch.setattr(beets.ui, "should_write", lambda _opt: True)
    client, playlists_dir = edit_client

    item = _item_by_title(edit_lib, "15 Step")
    record = _stage_playlist(playlists_dir, edit_lib, [_require_id(item.id)])
    before = _export_text(edit_lib, record.id)

    album_id = _require_id(_album_by_title(edit_lib, "In Rainbows").id)
    r = client.post(f"/api/albums/{album_id}/edit", json={"album": {"title": "In Rainbows (R)"}})

    assert r.status_code == 200
    assert r.json()["playlists_reexported"] == 0
    assert _export_text(edit_lib, record.id) == before


def test_album_edit_leaves_an_unrelated_playlist_alone(
    edit_client: tuple[TestClient, Path], edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only playlists HOLDING a moved item are re-exported (and counted)."""
    import beets.ui

    monkeypatch.setattr(beets.ui, "should_move", lambda _opt: True)
    monkeypatch.setattr(beets.ui, "should_write", lambda _opt: True)
    client, playlists_dir = edit_client

    moved = _item_by_title(edit_lib, "15 Step")
    other = _item_by_title(edit_lib, "Nude")
    affected = _stage_playlist(playlists_dir, edit_lib, [_require_id(moved.id)], name="Affected")
    untouched = _stage_playlist(playlists_dir, edit_lib, [_require_id(other.id)], name="Untouched")
    untouched_before = _export_text(edit_lib, untouched.id)

    album_id = _require_id(_album_by_title(edit_lib, "In Rainbows").id)
    # A per-TRACK edit: only "15 Step" gets a new destination, so "Nude" stays put.
    r = client.post(
        f"/api/albums/{album_id}/edit",
        json={"tracks": [{"item_id": _require_id(moved.id), "title": "15 Steps"}]},
    )

    assert r.status_code == 200
    assert r.json()["playlists_reexported"] == 1
    assert "01 15 Steps.flac" in _export_text(edit_lib, affected.id)
    assert _export_text(edit_lib, untouched.id) == untouched_before


# ----- 2. album delete -----


def test_album_delete_prunes_the_dropped_tracks_from_the_export(
    dup_client: tuple[TestClient, Path], duplicates_lib: Library
) -> None:
    """A deleted album's files live in Trash now; the export must stop naming
    them, while a surviving track on the SAME playlist keeps its line."""
    client, playlists_dir = dup_client

    doomed = _album_by_title(duplicates_lib, "Discovery")
    doomed_item = next(iter(doomed.items()))
    survivor = next(i for i in duplicates_lib.items() if str(i.album) == "In Rainbows")
    record = _stage_playlist(
        playlists_dir,
        duplicates_lib,
        [_require_id(doomed_item.id), _require_id(survivor.id)],
    )
    before = _export_text(duplicates_lib, record.id)
    assert before.count("#EXTINF:") == 2
    doomed_line = os.path.relpath(
        os.fsdecode(doomed_item.path), str(export_dir_for(duplicates_lib))
    )

    r = client.delete(f"/api/albums/{_require_id(doomed.id)}")

    assert r.status_code == 200
    assert r.json()["playlists_reexported"] == 1
    body = _export_text(duplicates_lib, record.id)
    assert body.count("#EXTINF:") == 1
    assert doomed_line not in body
    assert str(survivor.title) in body


# ----- 3. artist delete -----


def test_artist_delete_prunes_every_album_of_that_artist(
    dup_client: tuple[TestClient, Path], duplicates_lib: Library
) -> None:
    """The fan-out's collateral: tracks from EVERY album of the artist go, and
    the two seeded "In Rainbows" rows both count as that artist's."""
    client, playlists_dir = dup_client

    doomed_ids = [
        _require_id(i.id) for i in duplicates_lib.items() if str(i.albumartist) == "Radiohead"
    ][:2]
    survivor = next(i for i in duplicates_lib.items() if str(i.albumartist) == "Daft Punk")
    record = _stage_playlist(playlists_dir, duplicates_lib, [*doomed_ids, _require_id(survivor.id)])
    assert _export_text(duplicates_lib, record.id).count("#EXTINF:") == 3

    r = client.delete("/api/artists", params={"name": "Radiohead"})

    assert r.status_code == 200
    assert r.json()["playlists_reexported"] == 1
    body = _export_text(duplicates_lib, record.id)
    assert body.count("#EXTINF:") == 1
    assert str(survivor.title) in body


# ----- 4. duplicates resolve + resolve-all -----


def _strict_group(client: TestClient) -> tuple[int, list[int]]:
    report = client.get("/api/duplicates", params={"mode": "strict"}).json()
    group = report["groups"][0]
    members = [m["id"] for m in group["members"]]
    return members[0], members[1:]


def test_duplicates_resolve_prunes_the_loser_copy(
    dup_client: tuple[TestClient, Path], duplicates_lib: Library
) -> None:
    client, playlists_dir = dup_client
    keep_id, remove_ids = _strict_group(client)
    loser = _first_item_of(duplicates_lib, remove_ids[0])
    keeper = _first_item_of(duplicates_lib, keep_id)
    record = _stage_playlist(
        playlists_dir, duplicates_lib, [_require_id(loser.id), _require_id(keeper.id)]
    )
    loser_line = os.path.relpath(os.fsdecode(loser.path), str(export_dir_for(duplicates_lib)))
    assert loser_line in _export_text(duplicates_lib, record.id)

    r = client.post(
        "/api/duplicates/resolve",
        json={"mode": "strict", "keep_album_id": keep_id, "remove_album_ids": remove_ids},
    )

    assert r.status_code == 200
    assert r.json()["playlists_reexported"] == 1
    body = _export_text(duplicates_lib, record.id)
    assert loser_line not in body
    assert body.count("#EXTINF:") == 1


def test_duplicates_resolve_all_counts_each_playlist_once(
    dup_client: tuple[TestClient, Path], duplicates_lib: Library
) -> None:
    """One playlist holding losers from BOTH groups is rewritten once, not twice —
    the batch re-exports over the union of the run's dropped items."""
    client, playlists_dir = dup_client
    report = client.get("/api/duplicates", params={"mode": "fuzzy"}).json()
    groups = [
        {"keep_album_id": g["members"][0]["id"], "remove_album_ids": [g["members"][1]["id"]]}
        for g in report["groups"]
    ]
    assert len(groups) == 2  # the strict mb-1 pair + the untagged fuzzy pair

    losers = [_first_item_of(duplicates_lib, g["remove_album_ids"][0]) for g in groups]
    record = _stage_playlist(
        playlists_dir, duplicates_lib, [_require_id(i.id) for i in losers], name="Both"
    )
    assert _export_text(duplicates_lib, record.id).count("#EXTINF:") == 2

    r = client.post("/api/duplicates/resolve-all", json={"mode": "fuzzy", "groups": groups})

    assert r.status_code == 200
    body = r.json()
    assert body["group_count"] == 2
    assert body["playlists_reexported"] == 1
    assert _export_text(duplicates_lib, record.id).count("#EXTINF:") == 0


# ----- 5. reorganize -----


def test_reorganize_sweep_reexports_moved_tracks(reorganize_lib: Library, tmp_path: Path) -> None:
    """The widest mover of all: the sweep's tail pass repairs every export whose
    track it relocated, and the count lands on the terminal status."""
    from app.reorganize_jobs.registry import ReorganizeRegistry
    from app.reorganize_jobs.runner import sweep

    handle = make_test_handle(reorganize_lib, beets_dir_for(tmp_path))
    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()

    mover = _item_by_title(reorganize_lib, "15 Step")  # seeded mis-filed under junk/ir
    settled = _item_by_title(reorganize_lib, "One More Time")  # already in place
    moved_pl = _stage_playlist(playlists_dir, reorganize_lib, [_require_id(mover.id)], name="Moved")
    still = _stage_playlist(playlists_dir, reorganize_lib, [_require_id(settled.id)], name="Still")
    still_before = _export_text(reorganize_lib, still.id)
    assert "../junk/ir/a.mp3" in _export_text(reorganize_lib, moved_pl.id)

    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    sweep(reg, handle, scope="library", playlists_dir=playlists_dir)

    status = reg.state()
    assert status.phase == "done"
    body = _export_text(reorganize_lib, moved_pl.id)
    assert "../Radiohead/In Rainbows/01 15 Step.mp3" in body
    assert "../junk/ir/a.mp3" not in body
    # The singleton moves too, so both its playlist-less move and the album's are
    # in the union; only the one playlist actually holding a moved track counts.
    assert status.playlists_reexported == 1
    assert _export_text(reorganize_lib, still.id) == still_before


def test_reorganize_sweep_reexports_even_when_stopped(
    reorganize_lib: Library, tmp_path: Path
) -> None:
    """A STOPPED run still moved files, so it still owes the collateral.

    This is why ``_sweep_units`` no longer finishes the job itself: it used to
    call ``reg.finish("stopped")`` and return, and the caller returned straight
    out — past any tail pass.

    Stop fires at the first top-of-loop check AFTER the tracked album has moved,
    rather than after a fixed number of units: ``collect_units`` does not promise
    an order, and "stop after unit 1" silently stopped on a DIFFERENT album.
    """
    from app.reorganize_jobs.registry import ReorganizeRegistry
    from app.reorganize_jobs.runner import sweep

    handle = make_test_handle(reorganize_lib, beets_dir_for(tmp_path))
    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()
    mover = _item_by_title(reorganize_lib, "15 Step")
    mover_id = _require_id(mover.id)
    original_path = bytes(mover.path)
    record = _stage_playlist(playlists_dir, reorganize_lib, [mover_id])

    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    real_stop = reg.should_stop

    def stop_once_the_tracked_album_moved() -> bool:
        if real_stop():
            return True
        item = reorganize_lib.get_item(mover_id)
        return item is not None and bytes(item.path) != original_path

    reg.should_stop = stop_once_the_tracked_album_moved  # type: ignore[method-assign]  # deterministic stop point

    sweep(reg, handle, scope="library", playlists_dir=playlists_dir)

    status = reg.state()
    assert status.phase == "stopped"
    assert status.playlists_reexported == 1
    assert "../Radiohead/In Rainbows/01 15 Step.mp3" in _export_text(reorganize_lib, record.id)


def test_reorganize_count_is_recorded_before_the_job_finishes(
    reorganize_lib: Library, tmp_path: Path
) -> None:
    """The terminal status must ALREADY carry the count when the phase flips.

    Reading ``reg.state()`` after the sweep returns cannot see this: the count
    lands either way, just possibly late. It matters because ``finish`` is what
    frees the ``library_busy`` slot and what the polling UI reads as "over" — a
    ``done`` status showing 0 and then 1 a moment later is a wrong terminal
    answer. So the observation is taken AT the finish call.
    """
    from app.reorganize_jobs.registry import ReorganizeRegistry
    from app.reorganize_jobs.runner import sweep

    handle = make_test_handle(reorganize_lib, beets_dir_for(tmp_path))
    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()
    mover = _item_by_title(reorganize_lib, "15 Step")
    _stage_playlist(playlists_dir, reorganize_lib, [_require_id(mover.id)])

    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    real_finish = reg.finish
    at_finish: list[int] = []

    def finish(phase: str) -> None:
        at_finish.append(reg.state().playlists_reexported)
        real_finish(phase)

    reg.finish = finish  # type: ignore[method-assign]  # observe the state at the phase flip

    sweep(reg, handle, scope="library", playlists_dir=playlists_dir)

    assert at_finish == [1]


def test_reorganize_sweep_without_a_playlists_dir_skips_the_pass(
    reorganize_lib: Library, tmp_path: Path
) -> None:
    """Omitting ``playlists_dir`` leaves existing callers unchanged: no export is
    written and the status reads 0."""
    from app.reorganize_jobs.registry import ReorganizeRegistry
    from app.reorganize_jobs.runner import sweep

    handle = make_test_handle(reorganize_lib, beets_dir_for(tmp_path))
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    sweep(reg, handle, scope="library")

    assert reg.state().phase == "done"
    assert reg.state().playlists_reexported == 0
    assert not export_dir_for(reorganize_lib).exists()


def test_relocated_ids_counts_a_diverted_file_as_moved() -> None:
    """``_relocated_ids`` compares against the item's OWN pre-move path, not the
    computed destination.

    beets' ``unique_path`` can land a file at a ``.N`` sibling instead of the
    planned name. That is still a path change every `.m3u8` now gets wrong, so it
    must count — comparing to ``dest`` would call it "not moved" and leave the
    export pointing at a file that is no longer there.
    """
    from app.beets.reorganize import _relocated_ids

    pending = [
        (1, b"/m/old/a.mp3", b"/m/new/a.mp3"),  # landed at the planned dest
        (2, b"/m/old/b.mp3", b"/m/new/b.mp3"),  # diverted to a .1 sibling
        (3, b"/m/old/c.mp3", b"/m/new/c.mp3"),  # never moved
        (4, b"/m/old/d.mp3", b"/m/new/d.mp3"),  # absent from `after` entirely
    ]
    after = {1: b"/m/new/a.mp3", 2: b"/m/new/b.1.mp3", 3: b"/m/old/c.mp3"}

    assert _relocated_ids(pending, after) == [1, 2]


# ----- 6. disk sync -----


def test_disk_sync_sweep_prunes_a_removed_track(edit_lib: Library, tmp_path: Path) -> None:
    """A file deleted outside MusicDrop drops its row, so the export must stop
    listing it — the removed id no longer resolves and its line vanishes."""
    from app.disk_sync_jobs.registry import DiskSyncRegistry
    from app.disk_sync_jobs.runner import sweep

    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()
    victim = _item_by_title(edit_lib, "15 Step")
    survivor = _item_by_title(edit_lib, "Nude")
    record = _stage_playlist(
        playlists_dir, edit_lib, [_require_id(victim.id), _require_id(survivor.id)]
    )
    assert _export_text(edit_lib, record.id).count("#EXTINF:") == 2

    os.remove(os.fsdecode(victim.path))
    handle = make_test_handle(edit_lib, beets_dir_for(tmp_path))
    reg = DiskSyncRegistry()
    reg.start()
    sweep(reg, handle, playlists_dir=playlists_dir)

    status = reg.state()
    assert (status.phase, status.removed) == ("done", 1)
    assert status.playlists_reexported == 1
    body = _export_text(edit_lib, record.id)
    assert body.count("#EXTINF:") == 1
    assert "15 Step" not in body
    assert "Nude" in body


def test_disk_sync_sweep_that_removes_nothing_reexports_nothing(
    edit_lib: Library, tmp_path: Path
) -> None:
    """Control arm: an all-present library removes no row, so no export is
    rewritten — the pass keys off REMOVALS, not off the sweep having run."""
    from app.disk_sync_jobs.registry import DiskSyncRegistry
    from app.disk_sync_jobs.runner import sweep

    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()
    record = _stage_playlist(
        playlists_dir, edit_lib, [_require_id(_item_by_title(edit_lib, "Nude").id)]
    )
    before = _export_text(edit_lib, record.id)

    handle = make_test_handle(edit_lib, beets_dir_for(tmp_path))
    reg = DiskSyncRegistry()
    reg.start()
    sweep(reg, handle, playlists_dir=playlists_dir)

    assert reg.state().playlists_reexported == 0
    assert _export_text(edit_lib, record.id) == before


def test_disk_sync_count_is_recorded_before_the_job_finishes(
    edit_lib: Library, tmp_path: Path
) -> None:
    """Same terminal-status ordering as the reorganize sweep — see that test."""
    from app.disk_sync_jobs.registry import DiskSyncRegistry
    from app.disk_sync_jobs.runner import sweep

    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()
    victim = _item_by_title(edit_lib, "15 Step")
    _stage_playlist(playlists_dir, edit_lib, [_require_id(victim.id)])
    os.remove(os.fsdecode(victim.path))

    handle = make_test_handle(edit_lib, beets_dir_for(tmp_path))
    reg = DiskSyncRegistry()
    reg.start()
    real_finish = reg.finish
    at_finish: list[int] = []

    def finish(phase: str) -> None:
        at_finish.append(reg.state().playlists_reexported)
        real_finish(phase)

    reg.finish = finish  # type: ignore[method-assign]  # observe the state at the phase flip

    sweep(reg, handle, playlists_dir=playlists_dir)

    assert at_finish == [1]


# ----- 7. import: the `replace` duplicate action -----


def _replace_session(lib: Library, *, trash_dir: Path, playlists_dir: Path | None) -> Any:
    """A WebImportSession stripped to what ``_trash_replaced_albums`` reads.

    ``__init__`` is skipped (it would build a real beets ImportSession), so every
    attribute the post-run pass touches is set explicitly — the same fake shape
    tests/test_import_duplicate_session.py uses.
    """
    import logging

    from app.beets.import_session import ImportBridge, WebImportSession

    session = WebImportSession.__new__(WebImportSession)
    session.logger = logging.getLogger("test.replace")
    session.bridge = ImportBridge()
    session.lib = lib
    session._trash_dir = trash_dir
    # Wired as a PAIR with _trash_dir: the post-run pass skips entirely unless
    # both are set, so a fake that sets only one stops trashing silently.
    session._trash_origins_dir = trash_dir.parent / "trash-origins"
    session._playlists_dir = playlists_dir
    session._replace_album_ids = set()
    return session


def test_import_replace_reexports_the_superseded_albums_playlists(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """A ``replace`` decision trashes the pre-existing copy after the run; every
    playlist that held one of its tracks must lose those lines."""
    from app.beets.import_session import _trash_replaced_albums

    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()
    superseded = _album_by_title(duplicates_lib, "Discovery")
    doomed = next(iter(superseded.items()))
    survivor = next(i for i in duplicates_lib.items() if str(i.album) == "In Rainbows")
    record = _stage_playlist(
        playlists_dir, duplicates_lib, [_require_id(doomed.id), _require_id(survivor.id)]
    )
    doomed_line = os.path.relpath(os.fsdecode(doomed.path), str(export_dir_for(duplicates_lib)))
    assert doomed_line in _export_text(duplicates_lib, record.id)

    session = _replace_session(
        duplicates_lib, trash_dir=tmp_path / "trash", playlists_dir=playlists_dir
    )
    session._replace_album_ids = {_require_id(superseded.id)}
    _trash_replaced_albums(session)

    body = _export_text(duplicates_lib, record.id)
    assert doomed_line not in body
    assert body.count("#EXTINF:") == 1


def test_import_replace_without_a_playlists_dir_leaves_exports_alone(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """Unwired (``playlists_dir=None``, the tests/fakes shape) the pass is skipped:
    the trashing still happens, the export is simply not rewritten."""
    from app.beets.import_session import _trash_replaced_albums

    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()
    superseded = _album_by_title(duplicates_lib, "Discovery")
    doomed = next(iter(superseded.items()))
    record = _stage_playlist(playlists_dir, duplicates_lib, [_require_id(doomed.id)])
    before = _export_text(duplicates_lib, record.id)

    session = _replace_session(duplicates_lib, trash_dir=tmp_path / "trash", playlists_dir=None)
    session._replace_album_ids = {_require_id(superseded.id)}
    _trash_replaced_albums(session)

    assert duplicates_lib.get_album(_require_id(superseded.id)) is None  # it really ran
    assert _export_text(duplicates_lib, record.id) == before


def test_import_replace_skips_the_trashing_when_the_store_layout_is_refused(
    duplicates_lib: Library, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The Trash pair here was resolved when the registry got the library, not now.

    ``ImportRegistry.attach_library`` freezes ``(trash_dir, origins_dir)`` at
    lifespan or after an Apply, and an import can run hours later — the same gap
    the request paths close by re-resolving per call. Pointing the Trash at the
    music library is the shape measured to cost the library on Empty Trash; here
    it costs less and still costs something, because this pass MOVES rather than
    deletes: the replaced album's files would be relocated inside the library
    under a container name and its rows dropped. WARNING and skip, so the old
    copy stays where the operator can still see it.

    The control is
    ``test_import_replace_without_a_playlists_dir_leaves_exports_alone``, which
    runs the same helper on an accepted layout and asserts the album IS gone.
    """
    import logging

    from app.beets.import_session import _trash_replaced_albums

    music = Path(os.fsdecode(duplicates_lib.directory))
    superseded = _album_by_title(duplicates_lib, "Discovery")
    session = _replace_session(duplicates_lib, trash_dir=music, playlists_dir=None)
    session._replace_album_ids = {_require_id(superseded.id)}

    with caplog.at_level(logging.WARNING, logger="app.beets.import_session"):
        _trash_replaced_albums(session)

    assert duplicates_lib.get_album(_require_id(superseded.id)) is not None
    assert any("store layout is refused" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_run_import_worker_post_run_pass_tolerates_a_minimal_session(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A REPLICA of the fake-session shape in tests/test_import_session.py.

    That file may never be run here (it writes the real ``~/.config/beets``), and
    both additions to ``_trash_replaced_albums`` — reading ``album.items()``
    before the trash, and reading ``session._playlists_dir`` after it — are
    exactly the kind of change that breaks an attribute-by-attribute fake without
    any local test noticing. This runs the same shape so the edit made over there
    is verified here rather than assumed.

    The ERROR-log assertion is load-bearing, not decoration: ``run_import_worker``
    wraps the whole post-run pass in ``except Exception: logger.exception(...)``,
    so a fault AFTER the trashing (a missing ``_playlists_dir``, say) leaves
    ``trashed`` correct and the run silently annotated. Only the log sees it.
    """
    import contextlib
    import logging
    from contextlib import AbstractContextManager

    import app.beets.import_session as session_mod
    from app.beets.import_session import run_import_worker

    trashed: list[int] = []

    class _Album:
        def __init__(self, album_id: int) -> None:
            self.id = album_id

        def items(self) -> list[Any]:
            return []

    class _Lib:
        # ``directory`` and ``path``: the post-run pass re-checks the store
        # layout before moving anything, so a fake standing in for a beets
        # Library has to say where the music and the DB are. Siblings under
        # ``tmp_path``, which the check accepts.
        directory = os.fsencode(str(tmp_path / "music"))
        path = os.fsencode(str(tmp_path / "beets" / "library.db"))

        def music_dir_context(self) -> AbstractContextManager[None]:
            return contextlib.nullcontext()

        def get_album(self, album_id: int) -> Any:
            return _Album(album_id)

        def transaction(self) -> Any:
            return contextlib.nullcontext()

    class _FakeSession:
        lib = _Lib()
        paths: ClassVar[list[bytes]] = []
        _replace_album_ids: ClassVar[set[int]] = {11, 22}
        _trash_dir = tmp_path / "trash"
        _trash_origins_dir = tmp_path / "trash-origins"
        _playlists_dir = None

        def run(self) -> None:
            pass

    def fake_trash(lib: Any, album: Any, *, trash_dir: Path, origins_dir: Path) -> str:
        trashed.append(int(album.id))
        return str(trash_dir)

    monkeypatch.setattr(session_mod, "trash_album", fake_trash)
    monkeypatch.setattr("app.config.settings.beets_dir", str(tmp_path / "beets"))
    with caplog.at_level(logging.ERROR, logger="app.beets.import_session"):
        run_import_worker(_FakeSession())  # type: ignore[arg-type]  # minimal duck-typed session

    assert sorted(trashed) == [11, 22]
    assert [r.message for r in caplog.records] == []


def test_the_post_run_trash_pass_is_skipped_when_only_the_trash_dir_is_wired(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """Both dirs or neither, and the skip is deliberately LOUD.

    A Replace that trashed the superseded copies without recording where they
    came from leaves rows that can only ever be re-imported by template. The two
    dirs are wired as a pair from one resolve point, so a half-wiring is a bug —
    and stopping the Replace trash pass outright makes it fail visibly (the old
    album is still in the library) rather than degrading every deleted duplicate
    silently.
    """
    from app.beets.import_session import _trash_replaced_albums

    superseded = _album_by_title(duplicates_lib, "Discovery")
    album_id = _require_id(superseded.id)
    session = _replace_session(duplicates_lib, trash_dir=tmp_path / "trash", playlists_dir=None)
    session._trash_origins_dir = None
    session._replace_album_ids = {album_id}

    _trash_replaced_albums(session)

    assert duplicates_lib.get_album(album_id) is not None, "nothing may be trashed unrecorded"
    assert not (tmp_path / "trash").exists()


# ----- the shared core's own guarantees, from a worker thread -----


def test_sync_core_needs_no_event_loop(edit_lib: Library, tmp_path: Path) -> None:
    """The reorganize/disk-sync/import workers are plain daemon threads with no
    running loop. Calling the core from one must work — this is why the sync core
    exists at all, and an ``asyncio.run``-based helper would fail here."""
    from concurrent.futures import ThreadPoolExecutor

    from app.playlists.reexport import reexport_playlists_containing_sync

    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()
    item = _item_by_title(edit_lib, "Nude")
    record = _stage_playlist(playlists_dir, edit_lib, [_require_id(item.id)])
    shutil.rmtree(export_dir_for(edit_lib))

    with ThreadPoolExecutor(max_workers=1) as pool:
        count = pool.submit(
            reexport_playlists_containing_sync,
            {_require_id(item.id)},
            edit_lib,
            playlists_dir,
        ).result()

    assert count == 1
    assert "Nude" in _export_text(edit_lib, record.id)
