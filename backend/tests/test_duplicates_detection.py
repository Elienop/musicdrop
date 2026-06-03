"""Tests for the duplicate-albums contract + detection."""

from __future__ import annotations

import pytest
from beets.library import Library

from app.beets.duplicates import find_duplicate_albums, normalize
from app.models.duplicates import (
    DuplicateAlbum,
    DuplicateGroup,
    DuplicateMode,
    DuplicatesReport,
    MovedAlbum,
    ResolveRequest,
    ResolveResult,
)


def test_duplicate_album_extends_album_fields() -> None:
    dup = DuplicateAlbum(
        id=1,
        album_artist="Radiohead",
        title="In Rainbows",
        year=2007,
        track_count=10,
        genre=None,
        mb_albumid=None,
        format="FLAC",
        bitrate_kbps=900,
        folder="/music/Radiohead/In Rainbows",
        is_suggested_keeper=True,
    )
    # Reuses the shared Album contract (album_artist/title), adds per-copy fields.
    assert dup.album_artist == "Radiohead"
    assert dup.is_suggested_keeper is True
    assert dup.bitrate_kbps == 900


def test_report_and_resolve_models_round_trip() -> None:
    report = DuplicatesReport(mode=DuplicateMode.strict, group_count=0, album_count=0, groups=[])
    assert report.mode == "strict"

    req = ResolveRequest(mode=DuplicateMode.fuzzy, keep_album_id=1, remove_album_ids=[2, 3])
    assert req.remove_album_ids == [2, 3]

    group = DuplicateGroup(
        match_reason="MusicBrainz album id",
        suggested_keeper_id=1,
        members=[
            DuplicateAlbum(
                id=1,
                album_artist="A",
                title="B",
                year=None,
                track_count=10,
                genre=None,
                mb_albumid=None,
                format="FLAC",
                bitrate_kbps=900,
                folder="/m/B",
                is_suggested_keeper=True,
            )
        ],
    )
    assert group.suggested_keeper_id == group.members[0].id

    result = ResolveResult(
        kept_album_id=1,
        moved=[MovedAlbum(id=2, album_artist="A", title="B", trash_path="/t/B")],
    )
    assert result.moved[0].trash_path == "/t/B"


def test_resolve_request_rejects_empty_removes() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ResolveRequest(mode=DuplicateMode.strict, keep_album_id=1, remove_album_ids=[])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("In Rainbows", "in rainbows"),
        ("In Rainbows (Deluxe Edition)", "in rainbows"),
        ("Discovery [Remastered]", "discovery"),
        ("Get Lucky feat. Pharrell", "get lucky"),
        ("  Multiple   Spaces  ", "multiple spaces"),
        ("Punk!? & Roll", "punk roll"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


def test_detection_strict_groups_by_mb_albumid(duplicates_lib: Library) -> None:
    # Fixture builds: two albums sharing mb_albumid "mb-1" (10 + 9 tracks),
    # one album with mb_albumid "mb-2" (unique), one untagged copy pair.
    report = find_duplicate_albums(duplicates_lib, mode=DuplicateMode.strict)
    keys = {g.match_reason for g in report.groups}
    assert keys == {"MusicBrainz album id"}
    # Only the mb-1 pair is a strict duplicate group.
    assert report.group_count == 1
    group = report.groups[0]
    assert len(group.members) == 2
    # Keeper = most tracks (10 > 9), and it is members[0].
    assert group.members[0].is_suggested_keeper is True
    assert group.members[0].track_count == 10
    assert group.suggested_keeper_id == group.members[0].id


def test_detection_fuzzy_catches_untagged_copies(duplicates_lib: Library) -> None:
    report = find_duplicate_albums(duplicates_lib, mode=DuplicateMode.fuzzy)
    reasons = sorted(g.match_reason for g in report.groups)
    # The mb-1 pair (MB id) AND the untagged pair (artist+title) both surface.
    assert reasons == ["MusicBrainz album id", "artist + album title"]
    assert report.group_count == 2


def test_detection_clean_library_is_empty(empty_lib: Library) -> None:
    report = find_duplicate_albums(empty_lib, mode=DuplicateMode.fuzzy)
    assert report.group_count == 0
    assert report.album_count == 0
    assert report.groups == []
