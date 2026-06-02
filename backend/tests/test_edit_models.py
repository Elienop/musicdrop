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
