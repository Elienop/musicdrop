"""Tests for duplicate resolution (move losers to Trash, drop from library)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from beets.library import Library

from app.beets.duplicates import (
    StaleGroupError,
    find_duplicate_albums,
    resolve_duplicate_group,
)
from app.models.duplicates import DuplicateGroup, DuplicateMode


def _strict_group(lib: Library) -> DuplicateGroup:
    report = find_duplicate_albums(lib, mode=DuplicateMode.strict)
    assert report.group_count == 1
    return report.groups[0]


def test_resolve_moves_losers_to_trash_and_drops_from_db(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    trash = tmp_path / "trash"
    group = _strict_group(duplicates_lib)
    keep = group.suggested_keeper_id
    losers = [m.id for m in group.members if m.id != keep]

    result = resolve_duplicate_group(
        duplicates_lib,
        mode=DuplicateMode.strict,
        keep_album_id=keep,
        remove_album_ids=losers,
        trash_dir=trash,
    )

    # Keeper survives in the library; losers are gone from the DB.
    assert duplicates_lib.get_album(keep) is not None
    for loser in losers:
        assert duplicates_lib.get_album(loser) is None
    # Loser files live under trash now; nothing destroyed.
    assert result.kept_album_id == keep
    assert len(result.moved) == len(losers)
    for moved in result.moved:
        assert str(trash) in moved.trash_path
        assert os.path.isdir(moved.trash_path)
    # The group is no longer a duplicate (only the keeper remains).
    assert find_duplicate_albums(duplicates_lib, mode=DuplicateMode.strict).group_count == 0


def test_resolve_rejects_stale_group(duplicates_lib: Library, tmp_path: Path) -> None:
    group = _strict_group(duplicates_lib)
    keep = group.suggested_keeper_id
    losers = [m.id for m in group.members if m.id != keep]
    # Lie about membership: claim a non-member should be removed.
    with pytest.raises(StaleGroupError):
        resolve_duplicate_group(
            duplicates_lib,
            mode=DuplicateMode.strict,
            keep_album_id=keep,
            remove_album_ids=[*losers, 9999],
            trash_dir=tmp_path / "trash",
        )


def test_resolve_unknown_keep_is_stale(duplicates_lib: Library, tmp_path: Path) -> None:
    with pytest.raises(StaleGroupError):
        resolve_duplicate_group(
            duplicates_lib,
            mode=DuplicateMode.strict,
            keep_album_id=4242,
            remove_album_ids=[1],
            trash_dir=tmp_path / "trash",
        )
