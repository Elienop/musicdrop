"""Tests for the duplicate-albums contract + detection."""

from __future__ import annotations

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
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ResolveRequest(mode=DuplicateMode.strict, keep_album_id=1, remove_album_ids=[])
