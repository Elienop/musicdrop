"""Tests for the album/track tag-edit adapter (app/beets/edit.py)."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from beets.library import Library
from mediafile import MediaFile

from app.beets.library import _require_id
from app.models.edit import AlbumEditRequest, AlbumFieldEdits, TrackFieldEdits


def _album_id(lib: Library) -> int:
    albums = list(lib.albums())
    assert len(albums) == 1
    return _require_id(albums[0].id)


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
    req = AlbumEditRequest(
        tracks=[TrackFieldEdits(item_id=_require_id(first.id), title="15 Step (edit)")]
    )
    preview = preview_album_edit(edit_lib, album_id=aid, request=req, move_enabled=False)

    assert len(preview.tracks) == 1
    row = preview.tracks[0]
    assert row.item_id == _require_id(first.id)
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


# --- edit-driven moves must never mint a `.N` sibling --------------------------
#
# A tag edit renames files, so it can manufacture a filename collision out of a
# title/track change. beets' ``Item.move_file`` then diverts the file to a `.N`
# sibling (``util.unique_path``) and says nothing, so the edit reports "moved"
# while the library grows a "01 Song.1.flac" that no later run ever settles. The
# same pre-flight reorganize uses (``reorganize.collisions_by_dest``) refuses the
# offending track's MOVE before anything is touched; its tag write still stands.


def _names(directory: Path) -> set[str]:
    """Every file name in ``directory`` — the churn detector.

    Names, not bytes: a successful tag write changes the audio file's contents,
    so only the NAMES prove that nothing was renamed or diverted.
    """
    return {p.name for p in directory.iterdir() if p.is_file()}


def _album_dir(lib: Library, album_id: int) -> Path:
    return Path(os.path.dirname(os.fsdecode(_items(lib, album_id)[0].path)))


def _sorted_items(lib: Library, album_id: int) -> list[Any]:
    return sorted(_items(lib, album_id), key=lambda i: int(i.track))


def test_apply_refuses_a_move_onto_a_name_held_on_disk(edit_lib: Library) -> None:
    """The destination is occupied by a file the library knows nothing about.
    That track's move is refused, the file stays put, and no `.1` is minted."""
    from app.beets.edit import apply_album_edit

    aid = _album_id(edit_lib)
    target = _sorted_items(edit_lib, aid)[2]  # "Nude"
    tid = _require_id(target.id)
    base = _album_dir(edit_lib, aid)
    (base / "03 Nude (Live).flac").write_bytes(b"squatter")  # not in the library
    before = _names(base)

    req = AlbumEditRequest(tracks=[TrackFieldEdits(item_id=tid, title="Nude (Live)")])
    result = apply_album_edit(edit_lib, album_id=aid, request=req, write=True, move=True)

    row = next(r for r in result.items if r.item_id == tid)
    assert result.move_failures == 1
    assert row.moved is False
    assert row.written is True  # the tag write stands; only the move was refused
    assert "already exists on disk" in (row.error or "")
    assert _names(base) == before  # nothing renamed, no `.1` sibling
    paths = {int(i.id): os.fsdecode(i.path) for i in _items(edit_lib, aid)}
    assert paths[tid].endswith("03 Nude.flac")  # the DB still points at the real file
    assert (base / "03 Nude (Live).flac").read_bytes() == b"squatter"  # never overwritten


def test_apply_refuses_two_tracks_that_render_to_the_same_name(edit_lib: Library) -> None:
    """One edit, two tracks, one destination: whichever moves second is diverted,
    so BOTH are refused and the album is left exactly as it was."""
    from app.beets.edit import apply_album_edit

    aid = _album_id(edit_lib)
    items = _sorted_items(edit_lib, aid)
    ids = [_require_id(items[0].id), _require_id(items[1].id)]
    base = _album_dir(edit_lib, aid)
    before = _names(base)

    req = AlbumEditRequest(
        tracks=[TrackFieldEdits(item_id=iid, title="Twin", track=9) for iid in ids]
    )
    result = apply_album_edit(edit_lib, album_id=aid, request=req, write=True, move=True)

    assert result.move_failures == 2
    assert {r.item_id for r in result.items if r.error} == set(ids)
    assert not any(r.moved for r in result.items)
    row = next(r for r in result.items if r.item_id == ids[0])
    assert "resolve to this same name" in (row.error or "")
    assert _names(base) == before


def test_apply_refuses_a_move_onto_a_settled_mates_name(edit_lib: Library) -> None:
    """The steady state that made reorganize churn forever: the track holding the
    destination is an album-mate that is NOT moving, so a movers-only view never
    sees it. The mate is untouched and never blamed; only the mover is refused."""
    from app.beets.edit import apply_album_edit

    aid = _album_id(edit_lib)
    items = _sorted_items(edit_lib, aid)
    settled, mover = _require_id(items[1].id), _require_id(items[2].id)
    base = _album_dir(edit_lib, aid)
    before = _names(base)

    # Track 3 edited into track 2's identity -> track 2's settled file name.
    req = AlbumEditRequest(tracks=[TrackFieldEdits(item_id=mover, title="Bodysnatchers", track=2)])
    result = apply_album_edit(edit_lib, album_id=aid, request=req, write=True, move=True)

    rows = {r.item_id: r for r in result.items}
    assert result.move_failures == 1
    assert rows[mover].moved is False and rows[mover].error is not None
    assert rows[settled].error is None and rows[settled].moved is False
    assert _names(base) == before


def test_apply_refuses_a_move_onto_a_refused_tracks_file(edit_lib: Library) -> None:
    """Refusing one track un-exempts another. Track 3's destination is taken by a
    stranger, so it is refused and never vacates its own file — and track 2 was
    renamed onto exactly that file. The name is only free while track 3 moves, so
    track 2 has to be refused too, on a second look."""
    from app.beets.edit import apply_album_edit

    aid = _album_id(edit_lib)
    items = _sorted_items(edit_lib, aid)
    follower, blocked = _require_id(items[1].id), _require_id(items[2].id)
    base = _album_dir(edit_lib, aid)
    (base / "03 Nude (Live).flac").write_bytes(b"squatter")  # blocks track 3
    before = _names(base)

    req = AlbumEditRequest(
        tracks=[
            TrackFieldEdits(item_id=blocked, title="Nude (Live)"),  # -> the squatter's name
            TrackFieldEdits(item_id=follower, title="Nude", track=3),  # -> track 3's own file
        ]
    )
    result = apply_album_edit(edit_lib, album_id=aid, request=req, write=True, move=True)

    rows = {r.item_id: r for r in result.items}
    assert result.move_failures == 2
    assert rows[blocked].moved is False and rows[blocked].error is not None
    assert rows[follower].moved is False
    assert "already exists on disk" in (rows[follower].error or "")
    assert _names(base) == before  # no `.1`, and the squatter is untouched


def test_apply_settles_a_swap_between_two_tracks(edit_lib: Library) -> None:
    """Two tracks renamed INTO each other's current names must still land on their
    own destinations. The pre-flight lets the batch through because each occupied
    name is vacated by the same edit; the first mover is diverted all the same, so
    the move phase retries it once the mate has freed the name."""
    from app.beets.edit import apply_album_edit

    aid = _album_id(edit_lib)
    items = _sorted_items(edit_lib, aid)
    one, two = _require_id(items[0].id), _require_id(items[1].id)
    base = _album_dir(edit_lib, aid)
    (base / "01 15 Step.lrc").write_text("lyrics-one\n", encoding="utf-8")
    (base / "02 Bodysnatchers.lrc").write_text("lyrics-two\n", encoding="utf-8")

    req = AlbumEditRequest(
        tracks=[
            TrackFieldEdits(item_id=one, title="Bodysnatchers", track=2),
            TrackFieldEdits(item_id=two, title="15 Step", track=1),
        ]
    )
    result = apply_album_edit(edit_lib, album_id=aid, request=req, write=True, move=True)

    assert result.move_failures == 0
    assert all(r.error is None for r in result.items)
    assert {r.item_id for r in result.items if r.moved} == {one, two}
    assert _names(base) == {
        "01 15 Step.flac",
        "02 Bodysnatchers.flac",
        "03 Nude.flac",
        "01 15 Step.lrc",
        "02 Bodysnatchers.lrc",
    }
    paths = {int(i.id): os.fsdecode(i.path) for i in _items(edit_lib, aid)}
    assert paths[one].endswith("02 Bodysnatchers.flac")
    assert paths[two].endswith("01 15 Step.flac")
    # Each track's lyrics followed ITS OWN audio across the swap.
    assert (base / "02 Bodysnatchers.lrc").read_text(encoding="utf-8") == "lyrics-one\n"
    assert (base / "01 15 Step.lrc").read_text(encoding="utf-8") == "lyrics-two\n"


def test_apply_does_not_rerename_when_a_swap_mates_move_keeps_failing(
    edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mate whose move RAISED never vacated its name, so the retry pass must not
    count it as vacating. Pre-fix, the diverted first mover retried against the
    still-occupied name and beets renamed it AGAIN (.1 -> .2) — one extra rename
    per apply while the blocker persists. The divert is kept, reported truthfully
    ('landed at'), and never re-renamed."""
    from beets import util as beets_util

    from app.beets.edit import apply_album_edit

    aid = _album_id(edit_lib)
    items = _sorted_items(edit_lib, aid)
    one, two = _require_id(items[0].id), _require_id(items[1].id)
    base = _album_dir(edit_lib, aid)

    real_move = beets_util.move

    def move_unless_blocked(sour: bytes, dest: bytes, replace: bool = False) -> None:
        # Track two's file is stuck (NAS I/O error) on every attempt; everything
        # else moves for real — including track one's first, diverted, hop.
        if os.path.basename(os.fsdecode(sour)) == "02 Bodysnatchers.flac":
            raise beets_util.FilesystemError(OSError(5, "Input/output error"), "move", (sour, dest))
        real_move(sour, dest, replace)

    monkeypatch.setattr(beets_util, "move", move_unless_blocked)

    req = AlbumEditRequest(
        tracks=[
            TrackFieldEdits(item_id=one, title="Bodysnatchers", track=2),
            TrackFieldEdits(item_id=two, title="15 Step", track=1),
        ]
    )
    result = apply_album_edit(edit_lib, album_id=aid, request=req, write=True, move=True)

    rows = {r.item_id: r for r in result.items}
    assert result.move_failures == 2
    assert rows[two].moved is False
    assert "move failed" in (rows[two].error or "")
    # The first mover diverted ONCE and was left there — the truthful outcome...
    assert rows[one].moved is True
    assert "landed at" in (rows[one].error or "")
    assert "move refused" not in (rows[one].error or "")
    names = _names(base)
    assert "02 Bodysnatchers.1.flac" in names  # the single pass-1 divert
    # ...and THE point: the retry did not rename it again while the mate's file
    # still holds the destination.
    assert not any(".2." in n for n in names)
    assert "02 Bodysnatchers.flac" in names  # the stuck mate's file, untouched


def test_apply_move_carries_lyric_sidecars(edit_lib: Library) -> None:
    """beets moves audio + album art only, so an edit-driven rename strands the
    .lrc/.txt Plex actually reads. Both extensions follow the audio, and the
    vacated folder is re-pruned rather than left behind as an empty husk."""
    from app.beets.edit import apply_album_edit

    aid = _album_id(edit_lib)
    base = _album_dir(edit_lib, aid)
    (base / "01 15 Step.lrc").write_text("[00:01.00] step\n", encoding="utf-8")
    (base / "02 Bodysnatchers.txt").write_text("bodysnatchers\n", encoding="utf-8")

    req = AlbumEditRequest(album=AlbumFieldEdits(album_artist="Radiohead (Live)"))
    result = apply_album_edit(edit_lib, album_id=aid, request=req, write=True, move=True)

    assert result.move_failures == 0
    dest = Path(os.fsdecode(edit_lib.directory)) / "Radiohead (Live)" / "In Rainbows"
    assert (dest / "01 15 Step.lrc").read_text(encoding="utf-8") == "[00:01.00] step\n"
    assert (dest / "02 Bodysnatchers.txt").read_text(encoding="utf-8") == "bodysnatchers\n"
    assert not (dest / "03 Nude.lrc").exists()  # none invented for a track without one
    assert not base.exists()  # re-pruned: the sidecars left no empty husk behind


def test_apply_refused_move_carries_no_sidecars(edit_lib: Library) -> None:
    """A refused move touches nothing at all — the track's lyrics stay beside the
    audio they still belong to, and no sidecar is minted at the refused name."""
    from app.beets.edit import apply_album_edit

    aid = _album_id(edit_lib)
    target = _sorted_items(edit_lib, aid)[2]  # "Nude"
    tid = _require_id(target.id)
    base = _album_dir(edit_lib, aid)
    (base / "03 Nude (Live).flac").write_bytes(b"squatter")
    (base / "03 Nude.lrc").write_text("nude-lyrics\n", encoding="utf-8")

    req = AlbumEditRequest(tracks=[TrackFieldEdits(item_id=tid, title="Nude (Live)")])
    result = apply_album_edit(edit_lib, album_id=aid, request=req, write=True, move=True)

    row = next(r for r in result.items if r.item_id == tid)
    assert row.moved is False and row.error is not None
    assert (base / "03 Nude.lrc").read_text(encoding="utf-8") == "nude-lyrics\n"
    assert not (base / "03 Nude (Live).lrc").exists()


def test_apply_sidecar_failure_does_not_fail_the_move(
    edit_lib: Library, monkeypatch: Any, caplog: Any
) -> None:
    """An OSError moving a sidecar is logged, never raised: the audio has already
    moved, so the per-track outcome must stay truthful about the audio."""
    import logging
    import shutil

    from app.beets.edit import apply_album_edit

    aid = _album_id(edit_lib)
    base = _album_dir(edit_lib, aid)
    (base / "01 15 Step.lrc").write_text("[00:01.00] step\n", encoding="utf-8")

    def boom(*a: object, **k: object) -> None:
        raise OSError("read-only file system")

    # beets' own util.move uses os.replace (+ copyfileobj), never shutil.move, so
    # this breaks ONLY the sidecar move.
    monkeypatch.setattr(shutil, "move", boom)

    req = AlbumEditRequest(album=AlbumFieldEdits(album_artist="Radiohead (Live)"))
    with caplog.at_level(logging.WARNING, logger="app.beets.sidecars"):
        result = apply_album_edit(edit_lib, album_id=aid, request=req, write=True, move=True)

    assert result.move_failures == 0
    assert all(r.moved and r.error is None for r in result.items)
    dest = Path(os.fsdecode(edit_lib.directory)) / "Radiohead (Live)" / "In Rainbows"
    assert (dest / "01 15 Step.flac").exists()
    assert (base / "01 15 Step.lrc").exists()  # left behind, not destroyed
    assert "15 Step.lrc" in caplog.text
