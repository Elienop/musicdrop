from beets.autotag import AlbumInfo, AlbumMatch, TrackInfo
from beets.autotag.distance import distance
from beets.autotag.match import assign_items
from beets.library import Item

import app.beets.import_mapping as import_mapping
from app.beets.import_mapping import coverartarchive_front_url, map_album_match
from app.models.import_models import TrackChangeStatus


def test_confidence_clamps_non_finite_distance_to_zero() -> None:
    """A non-finite distance is an unusable distance — 0.0, never a token.

    beets' Distance arithmetic yields ``nan`` on non-finite weight routes
    (``match.distance_weights: {album: .nan}`` survives ``POST /api/config/save``
    straight to beets) and a contrived overflow can still yield ``±inf``. A bare
    NaN/Infinity token would be invalid JSON on the wire and in the bank row.
    """
    assert import_mapping._confidence(float("nan")) == 0.0
    assert import_mapping._confidence(float("inf")) == 0.0
    assert import_mapping._confidence(float("-inf")) == 0.0
    # normal distances are unchanged
    assert import_mapping._confidence(0.0) == 100.0
    assert import_mapping._confidence(0.5) == 50.0
    assert import_mapping._confidence(1.0) == 0.0


def test_caa_url_for_musicbrainz_release() -> None:
    url = coverartarchive_front_url(data_source="MusicBrainz", album_id="abcd-1234")
    assert url == "https://coverartarchive.org/release/abcd-1234/front-500"


def test_caa_url_none_for_non_musicbrainz_or_missing_id() -> None:
    assert coverartarchive_front_url(data_source="Discogs", album_id="x") is None
    assert coverartarchive_front_url(data_source="MusicBrainz", album_id=None) is None
    assert coverartarchive_front_url(data_source=None, album_id="x") is None


def _item(
    *,
    album: str,
    title: str,
    track: int,
    length: float,
    artist: str = "Radiohead",
    fmt: str = "",
) -> Item:
    # In-memory item, no DB add and no real audio file needed: the mapping and
    # the choose_match seam never touch the filesystem (we never call run()).
    item = Item(artist=artist, album=album, title=title, track=track, length=length, format=fmt)
    return item


def _perfect_match() -> AlbumMatch:
    items = [_item(album="OK Computer", title="Airbag", track=1, length=234.0)]
    tracks = [TrackInfo(title="Airbag", track_id="t1", index=1, length=234.0)]
    info = AlbumInfo(
        tracks=tracks,
        album="OK Computer",
        artist="Radiohead",
        album_id="a1",
        data_source="MusicBrainz",
        data_url="https://musicbrainz.org/release/a1",
        year=1997,
        label="Parlophone",
        country="GB",
        media="CD",
        va=False,
    )
    pairs, extra_items, extra_tracks = assign_items(items, info.tracks)
    dist = distance(items, info, pairs)
    return AlbumMatch(dist, info, dict(pairs), extra_items, extra_tracks)


def _diff_match() -> AlbumMatch:
    # 2 local files (one mistitled), 3 release tracks => 1 missing track,
    # 0 unmatched (the assignment maps both items). Spike-verified shape.
    items = [
        _item(album="OK Computr", title="Airbag", track=1, length=234.0),
        _item(album="OK Computr", title="UNKNOWN LOCAL", track=2, length=100.0),
    ]
    tracks = [
        TrackInfo(title="Airbag", track_id="t1", index=1, length=234.0),
        TrackInfo(title="Paranoid Android", track_id="t2", index=2, length=387.0),
        TrackInfo(title="Subterranean", track_id="t3", index=3, length=200.0),
    ]
    info = AlbumInfo(
        tracks=tracks,
        album="OK Computer",
        artist="Radiohead",
        album_id="a1",
        data_source="MusicBrainz",
        data_url="https://musicbrainz.org/release/a1",
        year=1997,
        label="Parlophone",
        va=False,
    )
    pairs, extra_items, extra_tracks = assign_items(items, info.tracks)
    dist = distance(items, info, pairs)
    return AlbumMatch(dist, info, dict(pairs), extra_items, extra_tracks)


def test_map_perfect_match_is_full_confidence_no_changes() -> None:
    candidate = map_album_match(
        _perfect_match(), cur_artist="Radiohead", cur_album="OK Computer", options=[]
    )
    assert candidate.confidence == 100.0
    assert candidate.changed_fields == []
    assert candidate.album_after.album == "OK Computer"
    assert candidate.album_after.year == 1997
    assert candidate.album_after.country == "GB"
    assert candidate.album_before.album == "OK Computer"
    assert len(candidate.tracks) == 1
    assert candidate.tracks[0].status is TrackChangeStatus.unchanged
    assert candidate.missing == []
    assert candidate.unmatched == []


def test_map_carries_current_file_format() -> None:
    items = [_item(album="OK Computer", title="Airbag", track=1, length=234.0, fmt="FLAC")]
    tracks = [TrackInfo(title="Airbag", track_id="t1", index=1, length=234.0)]
    info = AlbumInfo(
        tracks=tracks,
        album="OK Computer",
        artist="Radiohead",
        album_id="a1",
        data_source="MusicBrainz",
        data_url="https://musicbrainz.org/release/a1",
        year=1997,
        va=False,
    )
    pairs, extra_items, extra_tracks = assign_items(items, info.tracks)
    dist = distance(items, info, pairs)
    match = AlbumMatch(dist, info, dict(pairs), extra_items, extra_tracks)
    candidate = map_album_match(match, cur_artist="Radiohead", cur_album="OK Computer", options=[])
    assert candidate.tracks[0].format == "FLAC"


def test_map_missing_format_maps_to_none() -> None:
    # _perfect_match items never set format; beets defaults it to "".
    candidate = map_album_match(
        _perfect_match(), cur_artist="Radiohead", cur_album="OK Computer", options=[]
    )
    assert candidate.tracks[0].format is None


def test_map_diff_match_reports_album_track_missing_and_distance() -> None:
    candidate = map_album_match(
        _diff_match(), cur_artist="Radiohead", cur_album="OK Computr", options=[]
    )
    # Spike measured distance 0.245 => ~75.5%; assert the rounded value.
    assert candidate.confidence == 75.5
    # generic_penalty_keys strips album_/track_ prefixes and underscores.
    assert set(candidate.changed_fields) == {"album", "missing tracks", "tracks"}
    assert candidate.album_before.album == "OK Computr"
    assert candidate.album_after.album == "OK Computer"
    # 2 mapped tracks, ordered by proposed track index.
    assert len(candidate.tracks) == 2
    statuses = {t.title_after: t.status for t in candidate.tracks}
    assert statuses["Airbag"] is TrackChangeStatus.unchanged
    assert statuses["Paranoid Android"] is TrackChangeStatus.changed
    # 1 missing release track, 0 unmatched local files.
    assert [m.title for m in candidate.missing] == ["Subterranean"]
    assert candidate.unmatched == []


def test_map_options_from_candidate_list() -> None:
    from app.beets.import_mapping import map_candidate_options

    top = _perfect_match()
    second = _diff_match()
    options = map_candidate_options([top, second])
    assert [o.index for o in options] == [0, 1]
    assert options[0].confidence == 100.0
    assert options[0].data_source == "MusicBrainz"
    # disambiguation comes from AlbumMatch.disambig_string (may be empty).
    assert isinstance(options[0].disambiguation, str) or options[0].disambiguation is None


def test_candidate_options_carry_release_id() -> None:
    from app.beets.import_mapping import map_candidate_options

    options = map_candidate_options([_perfect_match(), _diff_match()])
    # AlbumInfo.album_id ("a1" in both fixtures) is the metadata-backend id
    # beets' search_ids lookup consumes; every ranked option carries its own.
    assert [o.release_id for o in options] == ["a1", "a1"]


def test_candidate_option_release_id_defaults_none() -> None:
    from app.models.import_models import CandidateOption

    # Additive + defaulted: every existing constructor keeps compiling, and
    # rows banked before this field simply read None (the unpinned fallback).
    option = CandidateOption(index=0, confidence=50.0, data_source=None, disambiguation=None)
    assert option.release_id is None


def _unmatched_match() -> AlbumMatch:
    # 3 local files but only 2 release tracks => the surplus junk file is
    # unmatched (extra_items), 0 missing. Verified against beets 2.11.0.
    items = [
        _item(album="OK Computer", title="Airbag", track=1, length=234.0),
        _item(album="OK Computer", title="Paranoid Android", track=2, length=387.0),
        _item(album="OK Computer", title="ZZZ BONUS JUNK", track=3, length=99.0, fmt="MP3"),
    ]
    tracks = [
        TrackInfo(title="Airbag", track_id="t1", index=1, length=234.0),
        TrackInfo(title="Paranoid Android", track_id="t2", index=2, length=387.0),
    ]
    info = AlbumInfo(
        tracks=tracks,
        album="OK Computer",
        artist="Radiohead",
        album_id="a1",
        data_source="MusicBrainz",
        data_url="https://musicbrainz.org/release/a1",
        year=1997,
        label="Parlophone",
        va=False,
    )
    pairs, extra_items, extra_tracks = assign_items(items, info.tracks)
    dist = distance(items, info, pairs)
    return AlbumMatch(dist, info, dict(pairs), extra_items, extra_tracks)


def test_candidate_options_carry_per_release_diff() -> None:
    from app.beets.import_mapping import map_candidate_options

    options = map_candidate_options([_perfect_match(), _diff_match()])
    # Every option carries its OWN full diff now, not just identity — so the
    # switcher can re-render the whole preview for the selected release.
    top, alt = options
    assert top.album_after is not None
    assert top.album_after.album == "OK Computer"
    assert top.album_after.year == 1997
    assert top.changed_fields == []
    assert len(top.tracks) == 1
    assert top.missing == []
    assert top.unmatched == []
    assert top.cover_after_url == "https://coverartarchive.org/release/a1/front-500"
    assert top.data_url == "https://musicbrainz.org/release/a1"
    # The alternate's diff differs from the top's — proving it is per-release.
    assert alt.album_after is not None
    assert "album" in alt.changed_fields
    assert len(alt.tracks) == 2
    assert [m.title for m in alt.missing] == ["Subterranean"]


def test_options_top_diff_equals_candidate_top_diff() -> None:
    from app.beets.import_mapping import map_candidate_options

    top = _diff_match()
    options = map_candidate_options([top, _perfect_match()])
    candidate = map_album_match(
        top, cur_artist="Radiohead", cur_album="OK Computr", options=options
    )
    # options[0] is the canonical top: its diff must equal the Candidate's top
    # diff (single source of truth — both go through the same block builders).
    assert options[0].album_after == candidate.album_after
    assert options[0].changed_fields == candidate.changed_fields
    assert options[0].tracks == candidate.tracks
    assert options[0].missing == candidate.missing
    assert options[0].unmatched == candidate.unmatched
    assert options[0].cover_after_url == candidate.cover_after_url
    assert options[0].data_url == candidate.data_url
    assert options[0].confidence == candidate.confidence


def test_candidate_options_pass_through_all_beets_returned() -> None:
    from app.beets.import_mapping import map_candidate_options

    # beets owns the candidate count via its per-source search_limit and returns
    # the full deduped, distance-sorted union. The mapping boundary must NOT cap
    # below that: every release beets handed us becomes a switcher option, in
    # order, so the UI mirrors beets exactly (no MusicDrop-side truncation).
    options = map_candidate_options([_perfect_match() for _ in range(8)])
    assert len(options) == 8
    assert [o.index for o in options] == list(range(8))


def test_legacy_bare_option_still_validates() -> None:
    from app.models.import_models import CandidateOption

    # A row banked before the per-release diff existed: bare options, no diff.
    # It must still validate, with the diff fields defaulting empty/None so the
    # frontend falls back to the top match for non-top selections.
    bare = CandidateOption(index=1, confidence=60.0, data_source="MusicBrainz", disambiguation=None)
    assert bare.album_after is None
    assert bare.changed_fields == []
    assert bare.tracks == []
    assert bare.missing == []
    assert bare.unmatched == []
    assert bare.cover_after_url is None
    assert bare.data_url is None
    # And it slots into a full banked Candidate payload without error.
    candidate = map_album_match(
        _perfect_match(), cur_artist="Radiohead", cur_album="OK Computer", options=[bare]
    )
    assert candidate.options[0].album_after is None


def test_map_unmatched_local_file_is_reported() -> None:
    candidate = map_album_match(
        _unmatched_match(), cur_artist="Radiohead", cur_album="OK Computer", options=[]
    )
    # 2 release tracks matched; the 3rd local file has no counterpart.
    assert len(candidate.tracks) == 2
    assert candidate.missing == []
    assert len(candidate.unmatched) == 1
    assert candidate.unmatched[0].title == "ZZZ BONUS JUNK"
    assert candidate.unmatched[0].track == 3
    assert candidate.unmatched[0].format == "MP3"
    # beets flags the surplus local file as an "unmatched tracks" penalty.
    assert "unmatched tracks" in candidate.changed_fields


def _multi_disc_match() -> AlbumMatch:
    # A 2-disc release, 2 tracks per disc. Disc 2's release tracks carry the
    # ABSOLUTE index (3, 4) but per-disc medium_index (1, 2). The local files are
    # already numbered per-disc (each disc restarts at 1), titles match exactly,
    # so the ONLY thing that could differ per row is the track NUMBER.
    items = [
        _item(album="Box", title="A1", track=1, length=100.0),  # disc 1, track 1
        _item(album="Box", title="A2", track=2, length=100.0),  # disc 1, track 2
        _item(album="Box", title="B1", track=1, length=100.0),  # disc 2, track 1
        _item(album="Box", title="B2", track=2, length=100.0),  # disc 2, track 2
    ]
    tracks = [
        TrackInfo(title="A1", track_id="t1", index=1, medium=1, medium_index=1, length=100.0),
        TrackInfo(title="A2", track_id="t2", index=2, medium=1, medium_index=2, length=100.0),
        TrackInfo(title="B1", track_id="t3", index=3, medium=2, medium_index=1, length=100.0),
        TrackInfo(title="B2", track_id="t4", index=4, medium=2, medium_index=2, length=100.0),
    ]
    info = AlbumInfo(
        tracks=tracks,
        album="Box",
        artist="Radiohead",
        album_id="a1",
        data_source="MusicBrainz",
        data_url="https://musicbrainz.org/release/a1",
        va=False,
    )
    pairs, extra_items, extra_tracks = assign_items(items, info.tracks)
    dist = distance(items, info, pairs)
    return AlbumMatch(dist, info, dict(pairs), extra_items, extra_tracks)


def test_track_after_uses_per_disc_number_when_configured() -> None:
    """With per_disc_numbering: yes (honored beets config), beets writes the
    per-disc number (medium_index) as the track tag, so disc 2 track 1 stays 1
    — NOT the absolute index 3. The review must show 'track_after == 1' and flag
    the row unchanged, not the bogus '1 -> 3' the absolute index produced."""
    from beets import config as beets_config

    beets_config["per_disc_numbering"] = True
    try:
        candidate = map_album_match(
            _multi_disc_match(), cur_artist="Radiohead", cur_album="Box", options=[]
        )
    finally:
        beets_config["per_disc_numbering"] = False

    by_title = {t.title_after: t for t in candidate.tracks}
    # Disc 2 rows keep the per-disc number that beets actually writes.
    assert by_title["B1"].track_after == 1
    assert by_title["B2"].track_after == 2
    # ... and since the local files are already numbered per-disc, nothing changed.
    assert by_title["B1"].track_before == 1
    assert by_title["B1"].status is TrackChangeStatus.unchanged
    assert by_title["B2"].status is TrackChangeStatus.unchanged


def test_track_after_uses_absolute_index_by_default() -> None:
    """Default beets config (per_disc_numbering: no) writes the absolute release
    index as the track tag — unchanged behavior. Disc 2 track 1 becomes 3, so a
    per-disc-numbered local file legitimately shows '1 -> 3' / changed."""
    candidate = map_album_match(
        _multi_disc_match(), cur_artist="Radiohead", cur_album="Box", options=[]
    )
    by_title = {t.title_after: t for t in candidate.tracks}
    assert by_title["B1"].track_after == 3
    assert by_title["B2"].track_after == 4
    assert by_title["B1"].status is TrackChangeStatus.changed


def test_multi_disc_rows_keep_release_order_under_per_disc() -> None:
    """The row's `index` is the release-order sort key and must stay ABSOLUTE
    even under per_disc_numbering, or disc 1 and disc 2 (both restarting at 1)
    would interleave. Only the displayed/written track_after is per-disc."""
    from beets import config as beets_config

    beets_config["per_disc_numbering"] = True
    try:
        candidate = map_album_match(
            _multi_disc_match(), cur_artist="Radiohead", cur_album="Box", options=[]
        )
    finally:
        beets_config["per_disc_numbering"] = False

    # Rows read in release order A1, A2, B1, B2 (absolute index 1..4).
    assert [t.title_after for t in candidate.tracks] == ["A1", "A2", "B1", "B2"]
    assert [t.index for t in candidate.tracks] == [1, 2, 3, 4]
    # index (sort key) and track_after (per-disc) diverge on disc 2.
    b1 = candidate.tracks[2]
    assert b1.index == 3
    assert b1.track_after == 1


def test_clean_disambig_drops_none_segments() -> None:
    # beets' Match.disambig_string str()-joins its disambig fields, so missing
    # values arrive as literal "None" segments — the FE dropdown showed
    # "Deezer, None, 2024, None, …". The adapter strips them.
    from app.beets.import_mapping import _clean_disambig

    assert (
        _clean_disambig("Deezer, None, 2024, None, Hit Wave Music, None")
        == "Deezer, 2024, Hit Wave Music"
    )
    assert _clean_disambig("None, None") is None
    assert _clean_disambig("2024, CD") == "2024, CD"
    assert _clean_disambig("") is None
    assert _clean_disambig(None) is None


def test_candidate_options_disambiguation_never_carries_none_segments() -> None:
    from app.beets.import_mapping import map_candidate_options

    options = map_candidate_options([_perfect_match(), _diff_match()])
    for option in options:
        assert "None" not in (option.disambiguation or "")
