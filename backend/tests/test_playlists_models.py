import pytest
from pydantic import ValidationError

from app.models.playlist import (
    PlaylistCreateRequest,
    PlaylistDetail,
    PlaylistUpdateRequest,
)


def test_playlist_detail_extends_playlist() -> None:
    detail = PlaylistDetail(
        id="abc",
        name="Late night",
        description="",
        track_count=0,
        target_plex_users=[],
        created_at="2026-06-06T00:00:00+00:00",
        updated_at="2026-06-06T00:00:00+00:00",
        track_ids=[],
    )
    assert detail.track_ids == []
    assert detail.track_count == 0


def test_create_request_rejects_blank_name() -> None:
    with pytest.raises(ValidationError):
        PlaylistCreateRequest(name="   ")


def test_create_request_strips_name() -> None:
    assert PlaylistCreateRequest(name="  Jazz  ").name == "Jazz"


def test_update_request_allows_all_none() -> None:
    body = PlaylistUpdateRequest()
    assert body.name is None and body.description is None


def test_update_request_rejects_blank_name() -> None:
    with pytest.raises(ValidationError):
        PlaylistUpdateRequest(name="  ")
