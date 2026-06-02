"""Tests for the album/track tag-edit adapter (app/beets/edit.py)."""

from __future__ import annotations

import os

from beets.library import Library
from mediafile import MediaFile


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
