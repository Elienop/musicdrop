"""Tests for the album/track tag-edit adapter (app/beets/edit.py)."""

from __future__ import annotations

import os

from beets.library import Library
from mediafile import MediaFile

from app.models.edit import AlbumEditRequest, AlbumFieldEdits, TrackFieldEdits


def _album_id(lib: Library) -> int:
    albums = list(lib.albums())
    assert len(albums) == 1
    return int(albums[0].id)


def test_edit_lib_seeds_real_taggable_flacs(edit_lib: Library) -> None:
    album = edit_lib.get_album(_album_id(edit_lib))
    assert album is not None
    items = list(album.items())
    assert len(items) == 3
    # Each seed file is a real FLAC mutagen can open.
    for item in items:
        mf = MediaFile(os.fsdecode(item.path))
        assert mf.type == "flac"


def test_preview_reports_album_field_diff(edit_lib: Library) -> None:
    from app.beets.edit import preview_album_edit

    aid = _album_id(edit_lib)
    req = AlbumEditRequest(album=AlbumFieldEdits(title="In Rainbows (Remaster)"))
    preview = preview_album_edit(edit_lib, album_id=aid, request=req, move_enabled=False)

    assert "title" in preview.changed_fields
    assert preview.album_before.title == "In Rainbows"
    assert preview.album_after.title == "In Rainbows (Remaster)"
    assert preview.tracks == []  # no per-track edits
    assert preview.move_plan == []  # move disabled


def test_preview_reports_track_diff(edit_lib: Library) -> None:
    from app.beets.edit import preview_album_edit

    aid = _album_id(edit_lib)
    album = edit_lib.get_album(aid)
    assert album is not None
    first = sorted(album.items(), key=lambda i: i.track)[0]
    req = AlbumEditRequest(tracks=[TrackFieldEdits(item_id=int(first.id), title="15 Step (edit)")])
    preview = preview_album_edit(edit_lib, album_id=aid, request=req, move_enabled=False)

    assert len(preview.tracks) == 1
    row = preview.tracks[0]
    assert row.item_id == int(first.id)
    assert row.title_before == "15 Step"
    assert row.title_after == "15 Step (edit)"


def test_preview_move_plan_when_enabled_and_path_field_changes(edit_lib: Library) -> None:
    from app.beets.edit import preview_album_edit

    aid = _album_id(edit_lib)
    # albumartist is in the path template -> changing it relocates every file.
    req = AlbumEditRequest(album=AlbumFieldEdits(album_artist="Radiohead (Live)"))
    preview = preview_album_edit(edit_lib, album_id=aid, request=req, move_enabled=True)

    assert preview.move_enabled is True
    assert len(preview.move_plan) == 3
    for change in preview.move_plan:
        assert "Radiohead (Live)" in change.new_path
        assert change.old_path != change.new_path


def test_preview_unknown_album_raises(edit_lib: Library) -> None:
    import pytest

    from app.beets.edit import AlbumNotFoundError, preview_album_edit

    with pytest.raises(AlbumNotFoundError):
        preview_album_edit(
            edit_lib, album_id=999999, request=AlbumEditRequest(), move_enabled=False
        )
