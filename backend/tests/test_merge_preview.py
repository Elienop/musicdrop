"""Tests for the duplicate comparison builder (app/beets/merge_preview.py).

The builder diffs the matched release tracklist against the existing library
copy AND the incoming import, per position. beets objects are faked with
SimpleNamespace (only the attributes the builder reads): release TrackInfo
(track_id/index/medium/title), the item->track mapping, and library Albums whose
items carry mb_trackid + format + bitrate (bps). No beets, no network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.models.import_models import DuplicateTrackState


def _track(track_id: Any, index: int, medium: int = 1, title: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        track_id=track_id, index=index, medium=medium, title=title or f"T{index}"
    )


class _FakeItem:
    """A hashable stand-in for a beets Item (SimpleNamespace can't be a dict key:
    it defines __eq__ so it's unhashable, and import items key the match mapping)."""

    def __init__(
        self,
        *,
        mb_trackid: str | None = None,
        track: int | None = None,
        disc: int = 1,
        fmt: str = "FLAC",
        bitrate: int = 1_000_000,
    ):
        self.mb_trackid = mb_trackid
        self.track = track  # beets per-disc track number
        self.disc = disc
        self.format = fmt  # beets reports e.g. "FLAC"/"MP3"
        self.bitrate = bitrate  # beets stores bitrate in bps


def _item(
    *,
    mb_trackid: str | None = None,
    track: int | None = None,
    disc: int = 1,
    fmt: str = "FLAC",
    bitrate: int = 1_000_000,
) -> _FakeItem:
    return _FakeItem(mb_trackid=mb_trackid, track=track, disc=disc, fmt=fmt, bitrate=bitrate)


def _album(items: list[_FakeItem]) -> SimpleNamespace:
    return SimpleNamespace(items=lambda: list(items))


def _task(
    tracks: list[SimpleNamespace], mapping: dict[_FakeItem, SimpleNamespace]
) -> SimpleNamespace:
    info = SimpleNamespace(tracks=tracks)
    return SimpleNamespace(match=SimpleNamespace(info=info, mapping=mapping))


def test_complementary_multidisc_library_only_plus_added() -> None:
    from app.beets.merge_preview import build_merge_preview

    # 4-track release: disc 1 (1-2) in library, disc 2 (3-4) is the import.
    tracks = [_track("t1", 1, 1), _track("t2", 2, 1), _track("t3", 3, 2), _track("t4", 4, 2)]
    lib = _album([_item(mb_trackid="t1"), _item(mb_trackid="t2")])
    imp3, imp4 = _item(), _item()
    task = _task(tracks, {imp3: tracks[2], imp4: tracks[3]})

    preview = build_merge_preview(task, [lib])
    assert preview is not None
    states = [r.state for r in preview.rows]
    assert states == [
        DuplicateTrackState.library_only,
        DuplicateTrackState.library_only,
        DuplicateTrackState.added,
        DuplicateTrackState.added,
    ]
    assert preview.total == 4
    assert preview.in_library_count == 2
    assert preview.added_count == 2
    assert preview.missing_count == 0
    assert preview.rows[2].disc == 2  # disc carried through


def test_same_tracks_higher_quality_is_upgrade() -> None:
    from app.beets.merge_preview import build_merge_preview

    tracks = [_track("t1", 1), _track("t2", 2)]
    lib = _album(
        [
            _item(mb_trackid="t1", fmt="MP3", bitrate=320_000),
            _item(mb_trackid="t2", fmt="MP3", bitrate=320_000),
        ]
    )
    i1, i2 = _item(fmt="FLAC", bitrate=1_000_000), _item(fmt="FLAC", bitrate=1_000_000)
    task = _task(tracks, {i1: tracks[0], i2: tracks[1]})

    preview = build_merge_preview(task, [lib])
    assert preview is not None
    assert [r.state for r in preview.rows] == [
        DuplicateTrackState.upgrade,
        DuplicateTrackState.upgrade,
    ]
    assert preview.upgrade_count == 2
    assert preview.added_count == 0 and preview.missing_count == 0
    row = preview.rows[0]
    assert row.library_format == "MP3" and row.library_bitrate_kbps == 320
    assert row.import_format == "FLAC" and row.import_bitrate_kbps == 1000


def test_same_tracks_lower_quality_is_downgrade() -> None:
    from app.beets.merge_preview import build_merge_preview

    tracks = [_track("t1", 1)]
    lib = _album([_item(mb_trackid="t1", fmt="FLAC", bitrate=1_000_000)])
    i1 = _item(fmt="MP3", bitrate=320_000)
    preview = build_merge_preview(_task(tracks, {i1: tracks[0]}), [lib])
    assert preview is not None
    assert preview.rows[0].state == DuplicateTrackState.downgrade


def test_same_tracks_equal_quality_is_same() -> None:
    from app.beets.merge_preview import build_merge_preview

    tracks = [_track("t1", 1)]
    lib = _album([_item(mb_trackid="t1", fmt="FLAC", bitrate=1_000_000)])
    i1 = _item(fmt="FLAC", bitrate=1_000_000)
    preview = build_merge_preview(_task(tracks, {i1: tracks[0]}), [lib])
    assert preview is not None
    assert preview.rows[0].state == DuplicateTrackState.same


def test_gap_leaves_missing_after_merge() -> None:
    from app.beets.merge_preview import build_merge_preview

    # 6-track release; library has disc 1 (1-2), import is disc 3 (5-6); disc 2
    # (3-4) is in NEITHER -> still missing.
    tracks = [_track(f"t{i}", i, (i + 1) // 2) for i in range(1, 7)]
    lib = _album([_item(mb_trackid="t1"), _item(mb_trackid="t2")])
    i5, i6 = _item(), _item()
    preview = build_merge_preview(_task(tracks, {i5: tracks[4], i6: tracks[5]}), [lib])
    assert preview is not None
    assert [r.state for r in preview.rows] == [
        DuplicateTrackState.library_only,
        DuplicateTrackState.library_only,
        DuplicateTrackState.missing,
        DuplicateTrackState.missing,
        DuplicateTrackState.added,
        DuplicateTrackState.added,
    ]
    assert preview.missing_count == 2 and preview.added_count == 2
    assert preview.in_library_count + preview.added_count + preview.missing_count == preview.total


def test_numeric_release_id_matches_string_mb_trackid() -> None:
    # Deezer returns an integer track_id; the library stores mb_trackid as a
    # string. A raw compare would mis-flag the owned track as "added".
    from app.beets.merge_preview import build_merge_preview

    tracks = [_track(1421196172, 1)]  # int, as Deezer yields
    lib = _album([_item(mb_trackid="1421196172", fmt="FLAC", bitrate=1_000_000)])
    preview = build_merge_preview(_task(tracks, {}), [lib])
    assert preview is not None
    assert preview.rows[0].state == DuplicateTrackState.library_only  # NOT added


def test_cross_source_matches_library_by_track_number() -> None:
    # The import matched a release from a DIFFERENT source than the library copy
    # (e.g. a Deezer download dup'ing a MusicBrainz library album): the release
    # track_ids (Deezer) and the library mb_trackids (MB UUIDs) are different id
    # namespaces and never match. The owned tracks must still be matched by
    # (disc, track number), not reported as "missing".
    from app.beets.merge_preview import build_merge_preview

    tracks = [_track("dz-1", 1), _track("dz-2", 2), _track("dz-3", 3)]  # ids != library ids
    lib = _album(
        [
            _item(mb_trackid="mb-uuid-1", track=1),
            _item(mb_trackid="mb-uuid-2", track=2),
            _item(mb_trackid="mb-uuid-3", track=3),
        ]
    )
    imp2 = _item(track=2)  # the import brings only track 2
    preview = build_merge_preview(_task(tracks, {imp2: tracks[1]}), [lib])
    assert preview is not None
    assert [r.state for r in preview.rows] == [
        DuplicateTrackState.library_only,
        DuplicateTrackState.same,
        DuplicateTrackState.library_only,
    ]
    assert preview.missing_count == 0
    assert preview.added_count == 0
    assert preview.in_library_count == 3


def test_no_match_returns_none() -> None:
    # An as-is import has no matched release -> no table to anchor.
    from app.beets.merge_preview import build_merge_preview

    task = SimpleNamespace(match=None)
    assert build_merge_preview(task, []) is None
