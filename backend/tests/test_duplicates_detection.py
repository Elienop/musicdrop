"""Tests for the duplicate-albums contract + detection."""

from __future__ import annotations

from pathlib import Path

import pytest
from beets.library import Library

from app.beets.duplicates import find_duplicate_albums, normalize
from app.models.duplicates import (
    DuplicateAlbum,
    DuplicateGroup,
    DuplicateMode,
    DuplicatesReport,
    MovedAlbum,
    ResolveRequest,
    ResolveResult,
)


def test_duplicate_album_extends_album_fields() -> None:
    dup = DuplicateAlbum(
        id=1,
        album_artist="Radiohead",
        title="In Rainbows",
        year=2007,
        track_count=10,
        genre=None,
        mb_albumid=None,
        format="FLAC",
        bitrate_kbps=900,
        folder="/music/Radiohead/In Rainbows",
        is_suggested_keeper=True,
    )
    # Reuses the shared Album contract (album_artist/title), adds per-copy fields.
    assert dup.album_artist == "Radiohead"
    assert dup.is_suggested_keeper is True
    assert dup.bitrate_kbps == 900


def test_report_and_resolve_models_round_trip() -> None:
    report = DuplicatesReport(mode=DuplicateMode.strict, group_count=0, album_count=0, groups=[])
    assert report.mode == "strict"

    req = ResolveRequest(mode=DuplicateMode.fuzzy, keep_album_id=1, remove_album_ids=[2, 3])
    assert req.remove_album_ids == [2, 3]

    group = DuplicateGroup(
        match_reason="MusicBrainz album id",
        suggested_keeper_id=1,
        members=[
            DuplicateAlbum(
                id=1,
                album_artist="A",
                title="B",
                year=None,
                track_count=10,
                genre=None,
                mb_albumid=None,
                format="FLAC",
                bitrate_kbps=900,
                folder="/m/B",
                is_suggested_keeper=True,
            )
        ],
    )
    assert group.suggested_keeper_id == group.members[0].id

    result = ResolveResult(
        kept_album_id=1,
        moved=[MovedAlbum(id=2, album_artist="A", title="B", trash_path="/t/B")],
    )
    assert result.moved[0].trash_path == "/t/B"


def test_resolve_request_rejects_empty_removes() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ResolveRequest(mode=DuplicateMode.strict, keep_album_id=1, remove_album_ids=[])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("In Rainbows", "in rainbows"),
        ("In Rainbows (Deluxe Edition)", "in rainbows"),
        ("Discovery [Remastered]", "discovery"),
        ("Get Lucky feat. Pharrell", "get lucky"),
        ("  Multiple   Spaces  ", "multiple spaces"),
        ("Punk!? & Roll", "punk roll"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


def test_detection_strict_groups_by_mb_albumid(duplicates_lib: Library) -> None:
    # Fixture builds: two albums sharing mb_albumid "mb-1" (10 + 9 tracks),
    # one album with mb_albumid "mb-2" (unique), one untagged copy pair.
    report = find_duplicate_albums(duplicates_lib, mode=DuplicateMode.strict)
    keys = {g.match_reason for g in report.groups}
    assert keys == {"MusicBrainz album id"}
    # Only the mb-1 pair is a strict duplicate group.
    assert report.group_count == 1
    group = report.groups[0]
    assert len(group.members) == 2
    # Keeper = most tracks (10 > 9), and it is members[0].
    assert group.members[0].is_suggested_keeper is True
    assert group.members[0].track_count == 10
    assert group.suggested_keeper_id == group.members[0].id


def test_detection_fuzzy_catches_untagged_copies(duplicates_lib: Library) -> None:
    report = find_duplicate_albums(duplicates_lib, mode=DuplicateMode.fuzzy)
    reasons = sorted(g.match_reason for g in report.groups)
    # The mb-1 pair (MB id) AND the untagged pair (artist+title) both surface.
    assert reasons == ["MusicBrainz album id", "artist + album title"]
    assert report.group_count == 2


def test_detection_clean_library_is_empty(empty_lib: Library) -> None:
    report = find_duplicate_albums(empty_lib, mode=DuplicateMode.fuzzy)
    assert report.group_count == 0
    assert report.album_count == 0
    assert report.groups == []


def _mixed_dup_lib(tmp_path: Path) -> Library:
    """A library with the two real-world dup shapes fuzzy must catch but the
    old mb-precedence keying missed:

      - "Air / Moon Safari" tagged with mb "mb-air" (10 tracks)
      - "Air / Moon Safari" with NO mb id (an as-is re-rip, 9 tracks)
        -> an MB-tagged copy + an untagged copy of the same album
      - "Portishead / Dummy" tagged mb "mb-d1" (11 tracks)
      - "Portishead / Dummy (Remastered)" tagged mb "mb-d2" (11 tracks)
        -> two DIFFERENT releases (distinct MBIDs) of the same album
    """
    import os

    from beets.library import Item

    from tests.conftest import build_library

    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def add_album(*, mb: str, artist: str, album: str, n: int, folder: str) -> None:
        items = []
        root = music / folder
        root.mkdir(parents=True, exist_ok=True)
        for i in range(1, n + 1):
            f = root / f"{i:02d} Track {i}.mp3"
            f.write_bytes(b"\x00")
            it = Item(album=album, albumartist=artist, artist=artist, title=f"Track {i}", track=i)
            it.path = os.fsencode(str(f))
            items.append(it)
        al = lib.add_album(items)
        if mb:
            al["mb_albumid"] = mb
        al.store()

    add_album(mb="mb-air", artist="Air", album="Moon Safari", n=10, folder="Air/Moon Safari")
    add_album(mb="", artist="Air", album="Moon Safari", n=9, folder="Air/Moon Safari (rip)")
    add_album(mb="mb-d1", artist="Portishead", album="Dummy", n=11, folder="Portishead/Dummy")
    add_album(
        mb="mb-d2",
        artist="Portishead",
        album="Dummy (Remastered)",
        n=11,
        folder="Portishead/Dummy (Remastered)",
    )
    return lib


def test_fuzzy_groups_mb_tagged_with_untagged_copy(tmp_path: Path) -> None:
    """The commonest real dup: one copy tagged via MusicBrainz, one imported
    as-is (no mb id). The old keying gave them keys ``mb:X`` vs ``fuzzy:...``
    which never collide, so fuzzy silently missed them."""
    lib = _mixed_dup_lib(tmp_path)
    report = find_duplicate_albums(lib, mode=DuplicateMode.fuzzy)
    air = next(g for g in report.groups if any(m.album_artist == "Air" for m in g.members))
    ids = {m.id for m in air.members}
    assert len(ids) == 2  # the MB-tagged copy AND the untagged copy grouped
    assert air.match_reason == "artist + album title"  # what actually bridged them
    # Keeper is the fuller copy (10 > 9 tracks).
    assert air.members[0].track_count == 10
    assert air.suggested_keeper_id == air.members[0].id


def test_fuzzy_groups_two_different_releases_same_album(tmp_path: Path) -> None:
    """Two distinct releases (standard vs remastered, different MBIDs) of the
    same album. normalize() strips '(remastered)' to catch exactly this, but the
    old keying gave each its own ``mb:`` key so they never grouped in fuzzy."""
    lib = _mixed_dup_lib(tmp_path)
    report = find_duplicate_albums(lib, mode=DuplicateMode.fuzzy)
    dummy = next(g for g in report.groups if any(m.album_artist == "Portishead" for m in g.members))
    ids = {m.id for m in dummy.members}
    assert len(ids) == 2  # both releases grouped despite distinct MBIDs
    assert dummy.match_reason == "artist + album title"


def test_strict_leaves_mixed_shapes_as_singletons(tmp_path: Path) -> None:
    """Strict is MB-id only and unchanged by the fuzzy fix: the untagged Air copy
    is dropped, the lone tagged Air copy is a singleton, and the two distinct-MBID
    Portishead releases are two singletons — so NO strict group survives."""
    lib = _mixed_dup_lib(tmp_path)
    report = find_duplicate_albums(lib, mode=DuplicateMode.strict)
    assert report.group_count == 0
