from __future__ import annotations

import os
from pathlib import Path

from beets.library import Library

from app.beets.reorganize import plan_reorganize, reorganize_album


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
    assert plan.orphans == [] and plan.orphans_total == 0
