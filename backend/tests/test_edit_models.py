"""Contract tests for the tag-edit Pydantic models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.edit import AlbumEditRequest, AlbumFieldEdits, TrackFieldEdits


def test_year_must_be_int_not_arbitrary_string() -> None:
    # Up-front type validation (beets would silently coerce "abc" -> null).
    with pytest.raises(ValidationError):
        AlbumFieldEdits(year="abc")  # type: ignore[arg-type]


def test_request_allows_album_only_and_tracks_only() -> None:
    a = AlbumEditRequest(album=AlbumFieldEdits(title="In Rainbows"), tracks=[])
    assert a.album is not None and a.album.title == "In Rainbows"
    b = AlbumEditRequest(album=None, tracks=[TrackFieldEdits(item_id=5, title="Nude")])
    assert b.tracks[0].item_id == 5


def test_track_edit_requires_item_id() -> None:
    with pytest.raises(ValidationError):
        TrackFieldEdits(title="x")  # type: ignore[call-arg]


def test_year_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        AlbumFieldEdits(year=10000)
    with pytest.raises(ValidationError):
        AlbumFieldEdits(year=-1)


def test_year_in_range_accepted() -> None:
    assert AlbumFieldEdits(year=0).year == 0
    assert AlbumFieldEdits(year=9999).year == 9999


def test_track_number_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        TrackFieldEdits(item_id=1, track=100001)
    with pytest.raises(ValidationError):
        TrackFieldEdits(item_id=1, track=-1)


def test_album_string_fields_max_length_enforced() -> None:
    too_long = "x" * 1001
    with pytest.raises(ValidationError):
        AlbumFieldEdits(title=too_long)
    with pytest.raises(ValidationError):
        AlbumFieldEdits(album_artist=too_long)
    with pytest.raises(ValidationError):
        AlbumFieldEdits(genre=too_long)


def test_track_string_fields_max_length_enforced() -> None:
    too_long = "x" * 1001
    with pytest.raises(ValidationError):
        TrackFieldEdits(item_id=1, title=too_long)
    with pytest.raises(ValidationError):
        TrackFieldEdits(item_id=1, artist=too_long)
