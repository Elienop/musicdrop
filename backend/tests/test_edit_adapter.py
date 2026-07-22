"""Tests for the album/track tag-edit adapter (app/beets/edit.py)."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from beets.library import Library
from mediafile import MediaFile

from app.models.edit import AlbumEditRequest, AlbumFieldEdits, TrackFieldEdits


def _album_id(lib: Library) -> int:
    albums = list(lib.albums())
    assert len(albums) == 1
    return int(albums[0].id)


def _items(lib: Library, album_id: int) -> list[Any]:
    album = lib.get_album(album_id)
    assert album is not None
    return list(album.items())


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


def test_apply_writes_album_field_to_every_track(edit_lib: Library) -> None:
    from app.beets.edit import apply_album_edit

    aid = _album_id(edit_lib)
    req = AlbumEditRequest(album=AlbumFieldEdits(title="In Rainbows (Deluxe)"))
    result = apply_album_edit(edit_lib, album_id=aid, request=req, write=True, move=False)

    assert result.album.title == "In Rainbows (Deluxe)"
    assert result.write_failures == 0
    # DB: every track inherited the album title.
    for item in _items(edit_lib, aid):
        assert item.album == "In Rainbows (Deluxe)"
    # File: tags were written to disk (read back via MediaFile).
    first = next(iter(_items(edit_lib, aid)))
    assert MediaFile(os.fsdecode(first.path)).album == "In Rainbows (Deluxe)"


def test_apply_per_track_title_changes_only_that_track(edit_lib: Library) -> None:
    from app.beets.edit import apply_album_edit

    aid = _album_id(edit_lib)
    items = sorted(_items(edit_lib, aid), key=lambda i: i.track)
    target = int(items[0].id)
    req = AlbumEditRequest(tracks=[TrackFieldEdits(item_id=target, title="15 Step (alt)")])
    apply_album_edit(edit_lib, album_id=aid, request=req, write=True, move=False)

    refreshed = {int(i.id): i for i in _items(edit_lib, aid)}
    assert refreshed[target].title == "15 Step (alt)"
    assert MediaFile(os.fsdecode(refreshed[target].path)).title == "15 Step (alt)"
    # other tracks untouched
    other = int(items[1].id)
    assert refreshed[other].title == "Bodysnatchers"


def test_apply_move_relocates_files_when_move_true(edit_lib: Library) -> None:
    from app.beets.edit import apply_album_edit

    aid = _album_id(edit_lib)
    req = AlbumEditRequest(album=AlbumFieldEdits(album_artist="Radiohead (Live)"))
    result = apply_album_edit(edit_lib, album_id=aid, request=req, write=True, move=True)

    assert result.move_failures == 0
    for item in _items(edit_lib, aid):
        path = os.fsdecode(item.path)
        assert "Radiohead (Live)" in path
        assert os.path.isfile(path)


def test_apply_no_move_keeps_files_in_place(edit_lib: Library) -> None:
    from app.beets.edit import apply_album_edit

    aid = _album_id(edit_lib)
    before = {int(i.id): os.fsdecode(i.path) for i in _items(edit_lib, aid)}
    req = AlbumEditRequest(album=AlbumFieldEdits(album_artist="Radiohead (Live)"))
    apply_album_edit(edit_lib, album_id=aid, request=req, write=True, move=False)

    after = {int(i.id): os.fsdecode(i.path) for i in _items(edit_lib, aid)}
    assert after == before  # tags/DB changed, files did not move


def test_apply_foreign_track_id_raises(edit_lib: Library) -> None:
    import pytest

    from app.beets.edit import ForeignTrackError, apply_album_edit

    aid = _album_id(edit_lib)
    with pytest.raises(ForeignTrackError):
        apply_album_edit(
            edit_lib,
            album_id=aid,
            request=AlbumEditRequest(tracks=[TrackFieldEdits(item_id=424242, title="x")]),
            write=True,
            move=False,
        )


def test_apply_foreign_track_id_with_no_fields_raises(edit_lib: Library) -> None:
    """A foreign item_id must 422 even when it carries no field edits."""
    import pytest

    from app.beets.edit import ForeignTrackError, apply_album_edit

    aid = _album_id(edit_lib)
    with pytest.raises(ForeignTrackError):
        apply_album_edit(
            edit_lib,
            album_id=aid,
            request=AlbumEditRequest(tracks=[TrackFieldEdits(item_id=424242)]),
            write=True,
            move=False,
        )


def test_preview_foreign_track_id_with_no_fields_raises(edit_lib: Library) -> None:
    import pytest

    from app.beets.edit import ForeignTrackError, preview_album_edit

    aid = _album_id(edit_lib)
    with pytest.raises(ForeignTrackError):
        preview_album_edit(
            edit_lib,
            album_id=aid,
            request=AlbumEditRequest(tracks=[TrackFieldEdits(item_id=424242)]),
            move_enabled=False,
        )


def test_apply_reports_per_item_write_failure(edit_lib: Library, tmp_path: Path) -> None:
    from app.beets.edit import apply_album_edit

    aid = _album_id(edit_lib)
    items = sorted(_items(edit_lib, aid), key=lambda i: i.track)
    # Corrupt the first track's file so try_write fails for it alone.
    Path(os.fsdecode(items[0].path)).write_bytes(b"not audio")
    req = AlbumEditRequest(album=AlbumFieldEdits(title="In Rainbows (X)"))
    result = apply_album_edit(edit_lib, album_id=aid, request=req, write=True, move=False)

    assert result.write_failures == 1
    failed = [r for r in result.items if not r.written]
    assert len(failed) == 1 and failed[0].error is not None
    # The other tracks still wrote — no total rollback.
    assert sum(1 for r in result.items if r.written) == 2


def test_apply_reports_both_write_and_move_failure_for_one_item(
    edit_lib: Library,
) -> None:
    """When a track fails BOTH write and move, both errors are reported."""
    from app.beets.edit import apply_album_edit

    aid = _album_id(edit_lib)
    items = sorted(_items(edit_lib, aid), key=lambda i: i.track)
    # Remove the first track's file: try_write fails (no file to tag) and
    # item.move fails (no source to relocate) for the same item.
    os.remove(os.fsdecode(items[0].path))
    # album_artist is in the path template -> a move is attempted for every item.
    req = AlbumEditRequest(album=AlbumFieldEdits(album_artist="Radiohead (Live)"))
    result = apply_album_edit(edit_lib, album_id=aid, request=req, write=True, move=True)

    assert result.write_failures == 1
    assert result.move_failures == 1
    failed = [r for r in result.items if not r.written]
    assert len(failed) == 1
    assert failed[0].error is not None
    # Both the write and the move failure are surfaced, not just the last one.
    # Match the fixed markers, not the file path (which can itself contain
    # "write" via the pytest tmp dir name).
    assert "tag write failed" in failed[0].error
    assert "move failed" in failed[0].error


def test_apply_move_relocates_album_art_and_updates_artpath(edit_lib: Library) -> None:
    """An album edit with move must relocate the cover AND persist the new artpath.
    item.move(with_album=True, store=False) moved the art on disk but discarded the
    transient album carrying the updated artpath, so the DB pointed at the vacated
    (pruned) old dir and the cover silently vanished."""
    from app.beets.edit import apply_album_edit

    aid = _album_id(edit_lib)
    album = edit_lib.get_album(aid)
    assert album is not None
    old_dir = os.path.dirname(os.fsdecode(next(iter(album.items())).path))
    art = os.path.join(old_dir, "cover.jpg")
    with open(art, "wb") as fh:
        fh.write(b"\xff\xd8\xff\xe0JFIF-fake-cover")  # bytes; content irrelevant
    album.artpath = os.fsencode(art)
    album.store()

    req = AlbumEditRequest(album=AlbumFieldEdits(album_artist="Radiohead (Live)"))
    result = apply_album_edit(edit_lib, album_id=aid, request=req, write=True, move=True)
    assert result.move_failures == 0

    refetched = edit_lib.get_album(aid)
    assert refetched is not None
    assert refetched.artpath is not None
    art_path = os.fsdecode(refetched.artpath)
    assert os.path.isfile(art_path)  # cover survived (DB points at a real file)
    # The cover sits in the NEW album dir alongside the moved tracks.
    new_dir = os.path.dirname(os.fsdecode(next(iter(refetched.items())).path))
    assert os.path.dirname(art_path) == new_dir
    assert "Radiohead (Live)" in art_path  # followed the album to its new home


def test_apply_runs_from_a_worker_thread(edit_lib: Library) -> None:
    """apply must bind music_dir_context so paths expand off the main thread."""
    from app.beets.edit import apply_album_edit

    aid = _album_id(edit_lib)
    req = AlbumEditRequest(album=AlbumFieldEdits(album_artist="Radiohead (Live)"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            apply_album_edit, edit_lib, album_id=aid, request=req, write=True, move=True
        ).result()

    assert result.move_failures == 0
    for item in _items(edit_lib, aid):
        assert "Radiohead (Live)" in os.fsdecode(item.path)
        assert os.path.isfile(os.fsdecode(item.path))


def test_apply_path_traversal_album_artist_stays_in_library(edit_lib: Library) -> None:
    """A malicious album_artist cannot escape the library directory.

    beets sanitizes path components, so even ``../../escape`` must resolve to a
    destination that is still under ``lib.directory``; no file may land outside.
    """
    from app.beets.edit import apply_album_edit

    aid = _album_id(edit_lib)
    libdir = os.path.abspath(os.fsdecode(edit_lib.directory))
    req = AlbumEditRequest(album=AlbumFieldEdits(album_artist="../../escape"))
    result = apply_album_edit(edit_lib, album_id=aid, request=req, write=True, move=True)

    assert result.move_failures == 0
    for item in _items(edit_lib, aid):
        path = os.path.abspath(os.fsdecode(item.path))
        assert os.path.commonpath([path, libdir]) == libdir
        assert os.path.isfile(path)
