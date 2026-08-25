import pytest
from pydantic import ValidationError

from app.models.playlist import (
    PendingTrack,
    PlaylistCreateRequest,
    PlaylistDetail,
    PlaylistReorderRequest,
    PlaylistTrack,
    PlaylistUpdateRequest,
)


def test_playlist_detail_extends_playlist() -> None:
    detail = PlaylistDetail(
        id="abc",
        name="Late night",
        description="",
        track_count=0,
        pending_count=0,
        target_plex_users=[],
        plex={},
        created_at="2026-06-06T00:00:00+00:00",
        updated_at="2026-06-06T00:00:00+00:00",
        tracks=[],
    )
    assert detail.tracks == []
    assert detail.track_count == 0
    assert detail.pending_count == 0


def test_pending_track_defaults() -> None:
    pending = PendingTrack()
    assert pending.artist is None
    assert pending.title is None
    assert pending.album is None
    assert pending.duration_seconds is None
    assert pending.source == ""


def test_playlist_track_pending_defaults_false() -> None:
    track = PlaylistTrack(
        uid="u1", id=1, title="T", artist="A", album="B", duration_seconds=None, available=True
    )
    assert track.pending is False


def test_reorder_request_rejects_duplicate_uids() -> None:
    with pytest.raises(ValidationError):
        PlaylistReorderRequest(entry_uids=["a", "a"])


def test_reorder_request_rejects_too_many_uids() -> None:
    with pytest.raises(ValidationError):
        PlaylistReorderRequest(entry_uids=[str(i) for i in range(10_001)])


def test_reorder_request_allows_empty() -> None:
    # An empty list is a valid full-replacement "clear".
    assert PlaylistReorderRequest(entry_uids=[]).entry_uids == []


def test_create_request_rejects_blank_name() -> None:
    with pytest.raises(ValidationError):
        PlaylistCreateRequest(name="   ")


def test_create_request_strips_name() -> None:
    assert PlaylistCreateRequest(name="  Jazz  ").name == "Jazz"


def test_update_request_allows_all_none() -> None:
    body = PlaylistUpdateRequest()
    assert body.name is None
    assert body.description is None


def test_update_request_rejects_blank_name() -> None:
    with pytest.raises(ValidationError):
        PlaylistUpdateRequest(name="  ")
