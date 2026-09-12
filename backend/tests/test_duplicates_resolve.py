"""Tests for duplicate resolution (move losers to Trash, drop from library)."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

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


def _a_strict_group_of_three(tmp_path: Path) -> Library:
    """One strict group with TWO losers, so a fault has a first album behind it.

    ``duplicates_lib``'s strict group is a pair, which cannot show what a
    mid-loop fault leaves: there is no earlier album to leave dropped.
    """
    import os

    from beets.library import Item

    from tests.conftest import build_library

    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))
    for folder in ("Album", "Album (copy)", "Album (copy 2)"):
        base = music / "Artist A" / folder
        base.mkdir(parents=True)
        items = []
        for i in (1, 2):
            f = base / f"{i:02d} Track {i}.mp3"
            f.write_bytes(b"\x00")
            it = Item(
                album="Album", albumartist="Artist A", artist="Artist A", title=f"T{i}", track=i
            )
            it.path = os.fsencode(str(f))
            items.append(it)
        al = lib.add_album(items)
        al["mb_albumid"] = "mb-x"
        al.store()
    return lib


def test_a_fault_on_the_second_loser_leaves_the_first_one_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transaction is per-CALL, not per-album (security seat L-2).

    Measured 2026-09-12: with the second ``trash_album`` raising, album rows
    ``[1,2,3,4,5]`` became ``[1,3,4,5]`` — the first loser's rows stay dropped,
    its files are in Trash with an origin record, and the route answers 500.
    Pre-existing, and recoverable through Restore, which is what the response
    body already promises. Pinned rather than redesigned; the per-album
    transaction is recorded in ``BACKLOG.md``.
    """
    from app.beets.trash import trash_album as real_trash_album

    lib = _a_strict_group_of_three(tmp_path)
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    group = _strict_group(lib)
    keep = group.suggested_keeper_id
    losers = sorted(m.id for m in group.members if m.id != keep)
    calls: list[int] = []

    def flaky(lib_: Library, album: Any, **kw: Any) -> str:
        calls.append(int(album.id))
        if len(calls) == 2:
            raise OSError(13, "Permission denied")
        return real_trash_album(lib_, album, **kw)

    monkeypatch.setattr("app.beets.duplicates.trash_album", flaky)

    with pytest.raises(OSError):
        resolve_duplicate_group(
            lib,
            mode=DuplicateMode.strict,
            keep_album_id=keep,
            remove_album_ids=losers,
            trash_dir=trash,
            origins_dir=origins,
        )

    assert calls == losers, "both were attempted, in the order the client asked for"
    assert lib.get_album(losers[0]) is None, "the first loser's rows are gone and stay gone"
    assert lib.get_album(losers[1]) is not None
    assert lib.get_album(keep) is not None
    assert list(origins.glob("*.json")), "and its move left the record Restore needs"


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
