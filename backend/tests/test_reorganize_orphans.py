from __future__ import annotations

import os
from pathlib import Path

from beets.library import Library

from app.beets.reorganize import plan_reorganize, reorganize_album, reorganize_singleton


def test_reorganize_album_sets_source_dir_on_move(reorganize_lib: Library) -> None:
    outcomes = [reorganize_album(reorganize_lib, a) for a in reorganize_lib.albums()]
    movers = [o for o in outcomes if o.status == "moved"]
    assert movers, "reorganize_lib should contain at least one misfiled album"
    assert all(m.source_dir for m in movers)  # moved outcomes carry their pre-move dir


def test_plan_lists_orphans(reorganize_lib: Library, tmp_path: Path) -> None:
    music_dir = Path(os.fsdecode(reorganize_lib.directory))
    husk = music_dir / "Ghost Artist"
    husk.mkdir(parents=True, exist_ok=True)
    (husk / "artist-poster.jpg").write_bytes(b"x")
    trash = tmp_path / "trash"
    plan = plan_reorganize(
        reorganize_lib, scope="library", artist=None, album_id=None, trash_dir=trash
    )
    assert any(o.name == "Ghost Artist" for o in plan.orphans)
    assert plan.orphans_total >= 1


def test_plan_without_trash_dir_has_no_orphans(reorganize_lib: Library) -> None:
    # Back-compat: existing callers pass no trash_dir -> orphan preview inactive.
    plan = plan_reorganize(reorganize_lib, scope="library", artist=None, album_id=None)
    assert plan.orphans == []
    assert plan.orphans_total == 0


def test_scoped_preview_has_no_orphans_premove(reorganize_lib: Library, tmp_path: Path) -> None:
    # A scoped (artist) preview cannot show husks: they form only during the run
    # (post-move). The actual sweep still cleans + reports them. Documents the design.
    music_dir = Path(os.fsdecode(reorganize_lib.directory))
    (music_dir / "Ghost").mkdir(parents=True, exist_ok=True)
    (music_dir / "Ghost" / "art.jpg").write_bytes(b"x")
    trash = tmp_path / "trash"
    plan = plan_reorganize(
        reorganize_lib, scope="artist", artist="Radiohead", album_id=None, trash_dir=trash
    )
    assert plan.orphans == []
    assert plan.orphans_total == 0


def test_reorganize_singleton_sets_source_dir(reorganize_lib: Library) -> None:
    outcomes = [
        reorganize_singleton(reorganize_lib, i)
        for i in reorganize_lib.items()
        if i.album_id is None
    ]
    movers = [o for o in outcomes if o.status == "moved"]
    # reorganize_lib includes a misfiled singleton; if present, its source_dir is set.
    for m in movers:
        assert m.source_dir


def test_registry_records_orphans() -> None:
    from app.reorganize_jobs.registry import ReorganizeRegistry

    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    reg.record_orphans(2)
    reg.record_orphans(1)
    reg.finish("done")
    assert reg.state().orphans_trashed == 3


def test_sweep_trashes_library_orphans(reorganize_lib: Library, tmp_path: Path) -> None:
    from app.reorganize_jobs.registry import ReorganizeRegistry
    from app.reorganize_jobs.runner import sweep
    from tests.conftest import make_test_handle

    music_dir = Path(os.fsdecode(reorganize_lib.directory))
    husk = music_dir / "Ghost Artist"
    husk.mkdir(parents=True, exist_ok=True)
    (husk / "artist-poster.jpg").write_bytes(b"x")
    trash = tmp_path / "trash"

    handle = make_test_handle(reorganize_lib, tmp_path)
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    sweep(reg, handle, scope="library", trash_dir=trash)

    assert reg.state().orphans_trashed == 1
    assert not husk.exists()
    assert (trash / "Ghost Artist" / "artist-poster.jpg").exists()
