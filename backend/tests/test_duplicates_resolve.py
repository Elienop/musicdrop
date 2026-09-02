"""Tests for duplicate resolution (move losers to Trash, drop from library)."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from beets.library import Library

from app.beets.duplicates import (
    StaleGroupError,
    find_duplicate_albums,
    resolve_all_groups,
    resolve_duplicate_group,
)
from app.models.duplicates import DuplicateGroup, DuplicateMode, GroupDecision
from tests.conftest import origins_for


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
        origins_dir=origins_for(trash),
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
            origins_dir=tmp_path / "trash-origins",
        )


def test_resolve_unknown_keep_is_stale(duplicates_lib: Library, tmp_path: Path) -> None:
    with pytest.raises(StaleGroupError):
        resolve_duplicate_group(
            duplicates_lib,
            mode=DuplicateMode.strict,
            keep_album_id=4242,
            remove_album_ids=[1],
            trash_dir=tmp_path / "trash",
            origins_dir=tmp_path / "trash-origins",
        )


def _decision_for(group: DuplicateGroup) -> GroupDecision:
    keep = group.suggested_keeper_id
    return GroupDecision(
        keep_album_id=keep,
        remove_album_ids=[m.id for m in group.members if m.id != keep],
    )


def test_resolve_all_moves_every_group(duplicates_lib: Library, tmp_path: Path) -> None:
    # Fuzzy mode yields TWO groups in the fixture: In Rainbows (mb) + Boards of
    # Canada (untagged). Resolve both at once.
    trash = tmp_path / "trash"
    report = find_duplicate_albums(duplicates_lib, mode=DuplicateMode.fuzzy)
    assert report.group_count == 2
    result = resolve_all_groups(
        duplicates_lib,
        mode=DuplicateMode.fuzzy,
        groups=[_decision_for(g) for g in report.groups],
        trash_dir=trash,
        origins_dir=origins_for(trash),
    )
    assert result.group_count == 2
    assert result.moved_count == 2  # one loser per group
    assert result.skipped_stale == []
    assert find_duplicate_albums(duplicates_lib, mode=DuplicateMode.fuzzy).group_count == 0


def test_resolve_all_skips_stale_and_resolves_the_rest(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    trash = tmp_path / "trash"
    report = find_duplicate_albums(duplicates_lib, mode=DuplicateMode.fuzzy)
    good, *rest = report.groups
    stale = rest[0]
    decisions = [
        _decision_for(good),
        # Lie about the second group's membership -> StaleGroupError -> skipped.
        GroupDecision(keep_album_id=stale.suggested_keeper_id, remove_album_ids=[424242]),
    ]
    result = resolve_all_groups(
        duplicates_lib,
        mode=DuplicateMode.fuzzy,
        groups=decisions,
        trash_dir=trash,
        origins_dir=origins_for(trash),
    )
    assert result.group_count == 1
    assert result.moved_count == 1
    assert len(result.skipped_stale) == 1
    assert result.skipped_stale[0].keep_album_id == stale.suggested_keeper_id
    assert find_duplicate_albums(duplicates_lib, mode=DuplicateMode.fuzzy).group_count == 1


def test_resolve_runs_from_a_worker_thread(duplicates_lib: Library, tmp_path: Path) -> None:
    """Resolve must work off the main thread.

    In production the endpoint runs ``resolve_duplicate_group`` in a FastAPI
    threadpool thread. beets 2.11 stores item paths relative to the library dir
    and expands them on load via a ``ContextVar`` (``beets.context``) that is set
    when the ``Library`` is opened — on the *main* thread. That ``ContextVar`` is
    NOT inherited by worker threads, so ``Album.move`` got a relative source path
    and raised ``FileNotFoundError`` (the live /duplicates resolve-all 500).
    ``resolve_duplicate_group`` must bind the library's music dir itself so it is
    correct from any thread. Running it in a worker here reproduces that path.
    """
    trash = tmp_path / "trash"
    group = _strict_group(duplicates_lib)
    keep = group.suggested_keeper_id
    losers = [m.id for m in group.members if m.id != keep]

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            resolve_duplicate_group,
            duplicates_lib,
            mode=DuplicateMode.strict,
            keep_album_id=keep,
            remove_album_ids=losers,
            trash_dir=trash,
            origins_dir=origins_for(trash),
        ).result()

    assert len(result.moved) == len(losers)
    for moved in result.moved:
        assert os.path.isdir(moved.trash_path)
    assert find_duplicate_albums(duplicates_lib, mode=DuplicateMode.strict).group_count == 0
