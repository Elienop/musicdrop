"""Tests for the artist-rename adapter (app/beets/rename.py)."""

from __future__ import annotations

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

    with pytest.raises(ArtistNotFoundError):
        preview_artist_rename(rename_lib, request=_req(name="Nobody"), move_enabled=False)


def test_preview_selection_is_exact_and_case_sensitive(rename_lib: Library) -> None:
    """A case variant is a DIFFERENT artist; the fan-out must not catch it."""
    from app.beets.rename import ArtistNotFoundError, preview_artist_rename

    with pytest.raises(ArtistNotFoundError):
        preview_artist_rename(rename_lib, request=_req(name="fayrouz"), move_enabled=False)


def test_preview_persists_nothing(rename_lib: Library) -> None:
    from app.beets.rename import preview_artist_rename

    preview_artist_rename(rename_lib, request=_req(), move_enabled=True)
    assert sorted({str(a.albumartist) for a in rename_lib.albums()}) == ["Fairuz", "Fayrouz"]
