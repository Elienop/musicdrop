"""Tests for the artist-rename adapter (app/beets/rename.py)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from beets.library import Library

from app.models.rename import ArtistRenameRequest


def _req(name: str = "Fayrouz", new_name: str = "Fairuz") -> ArtistRenameRequest:
    return ArtistRenameRequest(name=name, new_name=new_name)


def test_preview_covers_every_album_of_the_artist(rename_lib: Library) -> None:
    from app.beets.rename import preview_artist_rename

    preview = preview_artist_rename(rename_lib, request=_req(), move_enabled=True)
    assert preview.name == "Fayrouz"
    assert preview.new_name == "Fairuz"
    assert sorted(a.title for a in preview.albums) == ["Best Of", "Live"]
    # Every track relocates: the path format starts with $albumartist.
    assert [a.move_count for a in sorted(preview.albums, key=lambda a: a.title)] == [2, 1]
    assert all(a.refusals == [] for a in preview.albums)


def test_preview_reports_merge_into_existing_artist(rename_lib: Library) -> None:
    from app.beets.rename import preview_artist_rename

    preview = preview_artist_rename(rename_lib, request=_req(), move_enabled=False)
    assert preview.merge is not None
    assert preview.merge.existing_album_count == 1


def test_preview_no_merge_for_fresh_name(rename_lib: Library) -> None:
    from app.beets.rename import preview_artist_rename

    preview = preview_artist_rename(rename_lib, request=_req(new_name="Feiruz"), move_enabled=False)
    assert preview.merge is None


def test_preview_move_disabled_reports_zero_moves(rename_lib: Library) -> None:
    from app.beets.rename import preview_artist_rename

    preview = preview_artist_rename(rename_lib, request=_req(), move_enabled=False)
    assert preview.move_enabled is False
    assert all(a.move_count == 0 for a in preview.albums)


def test_preview_unknown_artist_raises(rename_lib: Library) -> None:
    from app.beets.rename import ArtistNotFoundError, preview_artist_rename

    request = _req(name="Nobody")
    with pytest.raises(ArtistNotFoundError):
        preview_artist_rename(rename_lib, request=request, move_enabled=False)


def test_preview_selection_is_exact_and_case_sensitive(rename_lib: Library) -> None:
    """A case variant is a DIFFERENT artist; the fan-out must not catch it."""
    from app.beets.rename import ArtistNotFoundError, preview_artist_rename

    request = _req(name="fayrouz")
    with pytest.raises(ArtistNotFoundError):
        preview_artist_rename(rename_lib, request=request, move_enabled=False)


def test_preview_persists_nothing(rename_lib: Library) -> None:
    from app.beets.rename import preview_artist_rename

    preview_artist_rename(rename_lib, request=_req(), move_enabled=True)
    assert sorted({str(a.albumartist) for a in rename_lib.albums()}) == ["Fairuz", "Fayrouz"]


def test_preview_does_not_strip_when_selecting(rename_lib: Library) -> None:
    """A padded albumartist is a DIFFERENT artist (delete_artist strips; rename must not)."""
    import shutil

    from beets.library import Item

    from app.beets.rename import preview_artist_rename

    base = Path(os.fsdecode(rename_lib.directory)) / "Fayrouz " / "Rarities"
    base.mkdir(parents=True, exist_ok=True)
    f = base / "01 Padded.flac"
    shutil.copyfile(Path(__file__).parent / "fixtures" / "silent.flac", f)
    it = Item(
        album="Rarities", albumartist="Fayrouz ", artist="Fayrouz ", title="Padded", track=1, disc=1
    )
    it.path = os.fsencode(str(f))
    rename_lib.add_album([it])

    preview = preview_artist_rename(rename_lib, request=_req(), move_enabled=False)
    assert sorted(a.title for a in preview.albums) == ["Best Of", "Live"]  # NOT "Rarities"


def test_preview_survives_a_vanished_album(rename_lib: Library) -> None:
    """An album gone by the time the per-album loop reaches it must not abort
    the whole preview (concurrent delete) — it simply drops out of the list."""
    from unittest.mock import patch

    from app.beets import rename as rename_mod
    from app.beets.rename import preview_artist_rename

    real = rename_mod._artist_albums

    def with_ghost(lib: Library, name: str) -> list[tuple[int, str]]:
        result = real(lib, name)
        if name == "Fayrouz":
            result = [*result, (999999, "Ghost")]
        return result

    with patch.object(rename_mod, "_artist_albums", side_effect=with_ghost):
        preview = preview_artist_rename(rename_lib, request=_req(), move_enabled=False)

    assert sorted(a.title for a in preview.albums) == ["Best Of", "Live"]  # no "Ghost" row


def test_padded_request_name_is_a_different_artist(rename_lib: Library) -> None:
    """The request's `name` is never stripped either — a padded name matches nothing."""
    from app.beets.rename import ArtistNotFoundError, preview_artist_rename

    request = _req(name="Fayrouz ")
    with pytest.raises(ArtistNotFoundError):
        preview_artist_rename(rename_lib, request=request, move_enabled=False)


def _paths(lib: Library) -> set[str]:
    return {os.fsdecode(it.path) for it in lib.items()}


def test_apply_renames_every_album_and_moves_files(rename_lib: Library) -> None:
    from app.beets.rename import apply_artist_rename

    outcome = apply_artist_rename(rename_lib, request=_req(), write=True, move=True)

    assert [a.outcome for a in outcome.albums] == ["renamed", "renamed"]
    assert outcome.old_name_remaining_albums == 0
    # The library agrees: all 3 albums (Best Of, Live, Legend) now file under Fairuz.
    assert {str(a.albumartist) for a in rename_lib.albums()} == {"Fairuz"}
    # Files physically moved into the new artist folder.
    music = Path(os.fsdecode(rename_lib.directory))
    assert all(p.startswith(str(music / "Fairuz")) for p in _paths(rename_lib))
    assert not (music / "Fayrouz" / "Best Of").exists() or not any(
        (music / "Fayrouz" / "Best Of").iterdir()
    )
    # Every moved item id is reported (2 + 1 tracks).
    assert len(outcome.moved_item_ids) == 3


def test_apply_writes_the_tag_into_the_files(rename_lib: Library) -> None:
    from mediafile import MediaFile

    from app.beets.rename import apply_artist_rename

    outcome = apply_artist_rename(rename_lib, request=_req(), write=True, move=False)
    assert outcome.moved_item_ids == []  # move=False moves nothing
    for it in rename_lib.items():
        if str(it.album) in ("Best Of", "Live"):
            mf = MediaFile(os.fsdecode(it.path))
            assert mf.albumartist == "Fairuz"
            # Owner decision: the per-track artist NEVER follows.
            assert mf.artist == "Fayrouz"


def test_apply_skips_a_drifted_album(rename_lib: Library) -> None:
    """An album whose artist changed after the snapshot is recorded, not renamed."""
    from unittest.mock import patch

    from app.beets import rename as rename_mod
    from app.beets.rename import apply_artist_rename

    # Simulate concurrent drift: after _artist_albums snapshots, flip one album.
    real = rename_mod._artist_albums
    flipped: dict[str, bool] = {"done": False}

    def snapshot_then_drift(lib: Library, name: str) -> list[tuple[int, str]]:
        result = real(lib, name)
        if name == "Fayrouz" and not flipped["done"] and result:
            flipped["done"] = True
            album = lib.get_album(result[0][0])
            assert album is not None
            album.albumartist = "Somebody Else"
            album.store()
        return result

    with patch.object(rename_mod, "_artist_albums", side_effect=snapshot_then_drift):
        outcome = apply_artist_rename(rename_lib, request=_req(), write=False, move=False)

    outcomes = sorted(a.outcome for a in outcome.albums)
    assert outcomes == ["renamed", "skipped_drifted"]
    skipped = next(a for a in outcome.albums if a.outcome == "skipped_drifted")
    assert skipped.error is not None


def test_apply_reports_a_vanished_album_as_drifted(rename_lib: Library) -> None:
    """An album gone between the drift check and the apply is skipped_drifted,
    not failed — same verdict as the preview's drop."""
    from unittest.mock import patch

    from app.beets import rename as rename_mod
    from app.beets.edit import AlbumNotFoundError
    from app.beets.edit import apply_album_edit as real_apply

    calls: dict[str, int] = {"n": 0}

    def flaky(lib: Library, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise AlbumNotFoundError("album 1 not found")
        return real_apply(lib, **kwargs)  # type: ignore[arg-type]  # kwargs mirror the real signature

    with patch.object(rename_mod, "apply_album_edit", side_effect=flaky):
        outcome = rename_mod.apply_artist_rename(
            rename_lib, request=_req(), write=False, move=False
        )

    assert sorted(a.outcome for a in outcome.albums) == ["renamed", "skipped_drifted"]
    skipped = next(a for a in outcome.albums if a.outcome == "skipped_drifted")
    assert skipped.error == "album no longer exists"


def test_apply_one_failure_does_not_abort_the_batch(rename_lib: Library) -> None:
    from unittest.mock import patch

    from app.beets import rename as rename_mod

    calls: dict[str, int] = {"n": 0}
    from app.beets.edit import apply_album_edit as real_apply

    def flaky(lib: Library, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk on fire")
        return real_apply(lib, **kwargs)  # type: ignore[arg-type]  # kwargs mirror the real signature

    with patch.object(rename_mod, "apply_album_edit", side_effect=flaky):
        outcome = rename_mod.apply_artist_rename(
            rename_lib, request=_req(), write=False, move=False
        )

    assert sorted(a.outcome for a in outcome.albums) == ["failed", "renamed"]
    failed = next(a for a in outcome.albums if a.outcome == "failed")
    assert failed.error == "disk on fire"
    # The failed album keeps the old name, so the old artist still exists.
    assert outcome.old_name_remaining_albums == 1


def test_apply_unknown_artist_raises(rename_lib: Library) -> None:
    from app.beets.rename import ArtistNotFoundError, apply_artist_rename

    request = _req(name="Nobody")
    with pytest.raises(ArtistNotFoundError):
        apply_artist_rename(rename_lib, request=request, write=False, move=False)
