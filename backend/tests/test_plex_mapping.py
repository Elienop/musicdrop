from typing import get_args

import pytest

from app.models.plex import PlexMatchCounts, PlexMatchMethod
from app.plex.client import music_section
from app.plex.errors import PlexConnectionError
from app.plex.mapping import (
    PlexResolution,
    PlexTrackSpec,
    _plex_seconds,
    resolve_ordered_tracks,
)
from tests.plex_fakes import FakeSection, FakeTrack


class _FakeOtherSection:
    TYPE = "movie"


class _FakeLibrary:
    def __init__(self, sections: list[object]) -> None:
        self._sections = sections

    def sections(self) -> list[object]:
        return self._sections


class _FakeServer:
    def __init__(self, sections: list[object]) -> None:
        self.library = _FakeLibrary(sections)


def test_music_section_picks_artist_type() -> None:
    artist = FakeSection([])
    server = _FakeServer([_FakeOtherSection(), artist])
    assert music_section(server) is artist


def test_music_section_none_when_absent() -> None:
    server = _FakeServer([_FakeOtherSection()])
    assert music_section(server) is None


class _NamedSection:
    def __init__(self, title: str, type_: str = "artist") -> None:
        self.TYPE = type_
        self.title = title


def test_music_section_empty_title_ambiguous_when_multiple_artist_sections() -> None:
    # Two music libraries + no configured section: silently taking the first would
    # land on the wrong one, so refuse and tell the user to pick a section.
    server = _FakeServer([_NamedSection("Music"), _NamedSection("MusicDrop")])
    with pytest.raises(PlexConnectionError):
        music_section(server, "")


def test_music_section_empty_title_ok_with_single_artist_section() -> None:
    only = _NamedSection("Music")
    server = _FakeServer([only, _NamedSection("Films", type_="movie")])
    assert music_section(server, "") is only


def test_music_section_named_title_matches_even_with_multiple() -> None:
    music, drop = _NamedSection("Music"), _NamedSection("MusicDrop")
    server = _FakeServer([music, drop])
    assert music_section(server, "musicdrop") is drop  # case-insensitive, unambiguous


def _track(
    rating_key: int,
    path: str,
    *,
    artist: str = "",
    album: str = "",
    title: str = "",
    index: int | None = None,
    duration: int | float | None = None,
) -> FakeTrack:
    """One Plex track at ``path``, its plexapi fields named the way a playlist
    spec talks about them (grandparentTitle = album-artist, parentTitle = album).

    ``duration`` is MILLISECONDS, as Plex reports it — the spec's length is
    seconds, and the whole point of the duration guard is that the two units
    never get confused."""
    return FakeTrack(
        rating_key,
        [path],
        grandparentTitle=artist,
        parentTitle=album,
        title=title,
        index=index,
        duration=duration,
    )


def _section(tracks: list[FakeTrack]) -> FakeSection:
    return FakeSection(tracks)


def _opt_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _spec(path: str, item_id: int = 1, **meta: object) -> PlexTrackSpec:
    return PlexTrackSpec(
        item_id=item_id,
        path=path,
        albumartist=str(meta.get("albumartist", "")),
        album=str(meta.get("album", "")),
        title=str(meta.get("title", "")),
        track=meta.get("track"),  # type: ignore[arg-type]  # int | None
        length_seconds=_opt_float(meta.get("length_seconds")),
    )


def test_resolve_ordered_tracks_preserves_order_and_counts_missing() -> None:
    section = _section([_track(10, "/data/music/A/1.flac"), _track(20, "/data/music/B/2.flac")])
    res = resolve_ordered_tracks(
        section,
        [
            _spec("/data/music/B/2.flac", item_id=2),
            _spec("/data/music/GONE.flac", item_id=3),
            _spec("/data/music/A/1.flac", item_id=1),
        ],
    )
    assert [t.ratingKey for t in res.tracks] == [20, 10]
    assert [m.item_id for m in res.missing] == [3]  # the DROPPED spec, not just a count


def test_path_miss_falls_back_to_unique_artist_title() -> None:
    # Plex file is named differently; only metadata can bridge it.
    section = _section(
        [
            _track(
                10,
                "/plex/Adele_19_01_Daydreamer.flac",
                artist="Adele",
                album="19",
                title="Daydreamer",
                index=1,
            )
        ]
    )
    spec = _spec(
        "/beets/Adele/19/01 Daydreamer.flac",
        albumartist="Adele",
        album="19",
        title="Daydreamer",
        track=1,
    )
    res = resolve_ordered_tracks(section, [spec])
    assert [x.ratingKey for x in res.tracks] == [10]
    assert len(res.missing) == 0


def test_fallback_is_case_and_whitespace_insensitive() -> None:
    section = _section([_track(11, "/plex/x.flac", artist="Adele", title="Daydreamer")])
    spec = _spec("/beets/none.flac", albumartist="  ADELE ", title="DAYDREAMER")
    res = resolve_ordered_tracks(section, [spec])
    assert [x.ratingKey for x in res.tracks] == [11]
    assert len(res.missing) == 0


def test_fallback_disambiguates_same_title_by_artist() -> None:
    # "What's Up" exists under two album-artists; the albumartist key disambiguates.
    section = _section(
        [
            _track(1, "/plex/a.flac", artist="4 Non Blondes", title="What's Up"),
            _track(2, "/plex/b.flac", artist="The Cast of 101 Dalmatians", title="What's Up"),
        ]
    )
    spec = _spec("/beets/none.flac", albumartist="4 Non Blondes", title="What's Up")
    res = resolve_ordered_tracks(section, [spec])
    assert [x.ratingKey for x in res.tracks] == [1]
    assert len(res.missing) == 0


def test_fallback_album_then_track_tiebreak() -> None:
    # Same artist+title twice -> album breaks the tie.
    section = _section(
        [
            _track(1, "/plex/s.flac", artist="X", album="Album", title="Song", index=3),
            _track(2, "/plex/l.flac", artist="X", album="Live", title="Song", index=9),
        ]
    )
    spec = _spec("/beets/none.flac", albumartist="X", album="Album", title="Song", track=3)
    res = resolve_ordered_tracks(section, [spec])
    assert [x.ratingKey for x in res.tracks] == [1]
    assert len(res.missing) == 0


def test_fallback_ambiguous_is_missing_not_guessed() -> None:
    # Same artist+title+album, no track to break the tie -> never guess.
    section = _section(
        [
            _track(1, "/plex/a.flac", artist="X", album="Al", title="Song"),
            _track(2, "/plex/b.flac", artist="X", album="Al", title="Song"),
        ]
    )
    spec = _spec("/beets/none.flac", albumartist="X", album="Al", title="Song")
    res = resolve_ordered_tracks(section, [spec])
    assert res.tracks == []
    assert len(res.missing) == 1


def test_no_metadata_hit_is_missing() -> None:
    section = _section([_track(1, "/plex/a.flac", artist="X", title="Other")])
    spec = _spec("/beets/none.flac", albumartist="X", title="Nope")
    res = resolve_ordered_tracks(section, [spec])
    assert res.tracks == []
    assert len(res.missing) == 1


def test_empty_metadata_spec_never_metadata_matches() -> None:
    # A path-only spec (no album-artist/title) must not collide with a track that
    # also happens to have empty metadata — only an exact path can resolve it.
    section = _section([_track(1, "/plex/a.flac")])
    res = resolve_ordered_tracks(section, [_spec("/beets/gone.flac")])
    assert res.tracks == []
    assert len(res.missing) == 1


def test_empty_albumartist_with_title_never_metadata_matches() -> None:
    # Empty album-artist but a real title must NOT match on title alone — without a
    # known artist a title-only match is a guess and could land a DIFFERENT song in
    # a target user's Plex (the `not all(key)` guard, not just `not any(key)`).
    section = _section([_track(777, "/plex/other.flac", artist="", title="Interlude")])
    spec = _spec("/beets/gone.flac", albumartist="", title="Interlude")
    res = resolve_ordered_tracks(section, [spec])
    assert res.tracks == []
    assert len(res.missing) == 1


def test_empty_title_with_albumartist_never_metadata_matches() -> None:
    # Symmetric guard: a real album-artist but a blank title can't metadata-match.
    section = _section([_track(778, "/plex/other.flac", artist="Adele", title="")])
    spec = _spec("/beets/gone.flac", albumartist="Adele", title="")
    res = resolve_ordered_tracks(section, [spec])
    assert res.tracks == []
    assert len(res.missing) == 1


def test_path_match_wins_over_metadata() -> None:
    # An exact path present -> used directly, metadata never consulted.
    section = _section([_track(10, "/plex/exact.flac", artist="X", title="Song")])
    spec = _spec("/plex/exact.flac", albumartist="DIFFERENT", title="DIFFERENT")
    res = resolve_ordered_tracks(section, [spec])
    assert [x.ratingKey for x in res.tracks] == [10]
    assert len(res.missing) == 0


def test_missing_carries_identity_and_reason_not_found() -> None:
    section = _section([_track(1, "/plex/a.flac", artist="A", title="one")])
    res = resolve_ordered_tracks(
        section,
        [
            PlexTrackSpec(
                item_id=11,
                path="/plex/a.flac",
                albumartist="A",
                album="X",
                title="one",
                track=1,
                length_seconds=None,
            ),
            PlexTrackSpec(
                item_id=12,
                path="/plex/gone.flac",
                albumartist="B",
                album="Y",
                title="two",
                track=2,
                length_seconds=None,
            ),
        ],
    )
    assert isinstance(res, PlexResolution)
    assert [t.ratingKey for t in res.tracks] == [1]
    assert [(m.item_id, m.title, m.albumartist, m.album, m.reason) for m in res.missing] == [
        (12, "two", "B", "Y", "not_found")
    ]


def test_missing_reason_ambiguous_when_metadata_ties() -> None:
    # Two Plex tracks share album-artist+title+album and neither track number matches.
    section = _section(
        [
            _track(1, "/plex/x/one.flac", artist="A", album="Same", title="one", index=1),
            _track(2, "/plex/y/one.flac", artist="A", album="Same", title="one", index=2),
        ]
    )
    res = resolve_ordered_tracks(
        section,
        [
            PlexTrackSpec(
                item_id=5,
                path="/beets/one.flac",
                albumartist="A",
                album="Same",
                title="one",
                track=None,
                length_seconds=None,
            )
        ],
    )
    assert res.tracks == []
    assert [(m.item_id, m.reason) for m in res.missing] == [(5, "ambiguous")]


def test_incomplete_metadata_key_is_not_found_not_ambiguous() -> None:
    section = _section([_track(1, "/plex/a.flac", artist="A", title="one")])
    res = resolve_ordered_tracks(
        section,
        [
            PlexTrackSpec(
                item_id=7,
                path="/beets/z.flac",
                albumartist="",
                album="",
                title="one",
                track=None,
                length_seconds=None,
            )
        ],
    )
    assert [(m.item_id, m.reason) for m in res.missing] == [(7, "not_found")]


# --- the third fallback: (album, title) agreed on DURATION --------------------
#
# Plex's own agent rewrites artist names, sometimes into a DIFFERENT SCRIPT, and
# no casefold bridges "Wael Kfoury" and its Arabic spelling. The album title
# survives that rewrite, so (album, title) can still find the track -- but many
# of these releases are SINGLES where album == title, which degenerates the key
# into a title-only match. A length that agrees is what keeps it from being a
# guess.

_ARABIC_WAEL = "وائل كفوري"  # what Plex's agent writes for beets' "Wael Kfoury"


def test_album_and_duration_bridge_a_rewritten_artist_script() -> None:
    # THE REAL BUG, from the owner's library: beets holds the Latin spelling and
    # Plex the Arabic one, so the (albumartist, title) fallback can never fire.
    section = _section(
        [
            _track(
                90,
                "/musicdrop/W/Kelna Mnenjar/01.flac",
                artist=_ARABIC_WAEL,
                album="Kelna Mnenjar",
                title="Kelna Mnenjar",
                duration=254_000,
            )
        ]
    )
    spec = _spec(
        "/music/Wael Kfoury/Kelna Mnenjar/01.flac",  # the WRONG library_path -> a path miss
        albumartist="Wael Kfoury",
        album="Kelna Mnenjar",
        title="Kelna Mnenjar",
        length_seconds=254.0,
    )
    res = resolve_ordered_tracks(section, [spec])
    assert [t.ratingKey for t in res.tracks] == [90]
    assert res.missing == []


def test_album_and_title_alone_never_match_a_wrong_duration() -> None:
    # THE ANTI-GUESSING GUARD, and the reason this fallback is duration-keyed at
    # all. Two artists both released a single called "Habibi", so album == title
    # == "Habibi" for each of them and the key collides exactly. Only the OTHER
    # artist's copy is in Plex; album+title alone would hand it back, put a
    # stranger's recording in the user's playlist, and report the sync "ok".
    section = _section(
        [
            _track(
                91,
                "/musicdrop/Other/Habibi/01.flac",
                artist="Some Other Singer",
                album="Habibi",
                title="Habibi",
                duration=185_000,
            )
        ]
    )
    spec = _spec(
        "/music/Wael Kfoury/Habibi/01.flac",
        albumartist="Wael Kfoury",
        album="Habibi",
        title="Habibi",
        length_seconds=254.0,  # 69 seconds apart: a different recording entirely
    )
    res = resolve_ordered_tracks(section, [spec])
    assert res.tracks == []
    assert [(m.item_id, m.reason) for m in res.missing] == [(1, "not_found")]


def test_album_fallback_tolerates_a_small_encoder_disagreement() -> None:
    # The same recording in two files: container/encoder rounding puts them a
    # bit over a second apart. Demanding exact equality would refuse every real
    # match and make the fallback useless.
    section = _section(
        [
            _track(
                99,
                "/musicdrop/W/Kelna Mnenjar/01.flac",
                artist=_ARABIC_WAEL,
                album="Kelna Mnenjar",
                title="Kelna Mnenjar",
                duration=255_100,
            )
        ]
    )
    spec = _spec(
        "/music/gone.flac",
        albumartist="Wael Kfoury",
        album="Kelna Mnenjar",
        title="Kelna Mnenjar",
        length_seconds=254.0,
    )
    res = resolve_ordered_tracks(section, [spec])
    assert [t.ratingKey for t in res.tracks] == [99]
    assert res.missing == []


def test_two_candidates_agreeing_on_album_title_and_duration_stay_ambiguous() -> None:
    # Album, title AND length all agree twice over -- the guard has nothing left
    # to separate them with, so the row stays missing. Never pick one.
    #
    # BOTH rivals wear a Plex-rewritten name (the second is "Nancy Ajram" in
    # Arabic), so the artist veto has nothing to read either: neither shares an
    # alphabet with the "Wael Kfoury" we asked for. Give one of them a readable
    # contradicting name and this stops being a tie -- see
    # test_a_contradicting_plex_artist_is_vetoed_not_tied.
    section = _section(
        [
            _track(
                92,
                "/musicdrop/a.flac",
                artist=_ARABIC_WAEL,
                album="Habibi",
                title="Habibi",
                duration=254_000,
            ),
            _track(
                93,
                "/musicdrop/b.flac",
                artist="نانسي عجرم",
                album="Habibi",
                title="Habibi",
                duration=254_500,
            ),
        ]
    )
    spec = _spec(
        "/music/gone.flac",
        albumartist="Wael Kfoury",
        album="Habibi",
        title="Habibi",
        length_seconds=254.0,
    )
    res = resolve_ordered_tracks(section, [spec])
    assert res.tracks == []
    assert [(m.item_id, m.reason) for m in res.missing] == [(1, "ambiguous")]


def test_exact_path_still_wins_over_the_album_fallback() -> None:
    # Order is contract: whatever resolves by PATH today keeps resolving by path.
    # Track 95 is a flawless album+title+duration match, so a fallback that ran
    # first would return IT and silently repoint the row.
    section = _section(
        [
            _track(
                94,
                "/plex/exact.flac",
                artist="X",
                album="Other Album",
                title="Other Title",
                duration=10_000,
            ),
            _track(
                95, "/plex/decoy.flac", artist="X", album="Habibi", title="Habibi", duration=254_000
            ),
        ]
    )
    spec = _spec(
        "/plex/exact.flac",
        albumartist="Wael Kfoury",
        album="Habibi",
        title="Habibi",
        length_seconds=254.0,
    )
    res = resolve_ordered_tracks(section, [spec])
    assert [t.ratingKey for t in res.tracks] == [94]
    assert res.missing == []


def test_albumartist_title_still_wins_over_the_album_fallback() -> None:
    # The middle rung keeps its place too: the (albumartist, title) hit and the
    # (album, title) hit are DIFFERENT tracks here, so a swapped order shows up
    # as a different ratingKey rather than as a passing test.
    section = _section(
        [
            _track(
                96, "/plex/by-artist.flac", artist="Wael Kfoury", title="Habibi", duration=254_000
            ),
            _track(
                97,
                "/plex/by-album.flac",
                artist=_ARABIC_WAEL,
                album="Habibi",
                title="Habibi",
                duration=254_000,
            ),
        ]
    )
    spec = _spec(
        "/music/gone.flac",
        albumartist="Wael Kfoury",
        album="Habibi",
        title="Habibi",
        length_seconds=254.0,
    )
    res = resolve_ordered_tracks(section, [spec])
    assert [t.ratingKey for t in res.tracks] == [96]
    assert res.missing == []


def test_album_fallback_refuses_when_the_spec_has_no_length() -> None:
    # beets never read this file's length, so there is nothing to check the
    # candidate against -- and an unchecked album+title match is the guess.
    section = _section(
        [
            _track(
                98,
                "/musicdrop/a.flac",
                artist=_ARABIC_WAEL,
                album="Habibi",
                title="Habibi",
                duration=254_000,
            )
        ]
    )
    spec = _spec("/music/gone.flac", albumartist="Wael Kfoury", album="Habibi", title="Habibi")
    res = resolve_ordered_tracks(section, [spec])
    assert res.tracks == []
    assert [(m.item_id, m.reason) for m in res.missing] == [(1, "not_found")]


def test_album_fallback_refuses_when_plex_reports_no_duration() -> None:
    # Symmetric: Plex has not analysed the file, so its duration is absent.
    section = _section(
        [_track(100, "/musicdrop/a.flac", artist=_ARABIC_WAEL, album="Habibi", title="Habibi")]
    )
    spec = _spec(
        "/music/gone.flac",
        albumartist="Wael Kfoury",
        album="Habibi",
        title="Habibi",
        length_seconds=254.0,
    )
    res = resolve_ordered_tracks(section, [spec])
    assert res.tracks == []
    assert [(m.item_id, m.reason) for m in res.missing] == [(1, "not_found")]


def test_album_fallback_reads_a_zero_duration_as_absent_on_either_side() -> None:
    # beets stores 0.0 for a length it never read and Plex reports 0 for a track
    # it has not analysed, so zero means ABSENT, not "zero seconds". Taken
    # literally, 0 and 0 agree perfectly -- every unanalysed track would match
    # every unread one that happens to share an album and a title.
    zero_in_plex = _section(
        [
            _track(
                101,
                "/musicdrop/a.flac",
                artist=_ARABIC_WAEL,
                album="Habibi",
                title="Habibi",
                duration=0,
            )
        ]
    )
    both_zero = _spec(
        "/music/gone.flac",
        albumartist="Wael Kfoury",
        album="Habibi",
        title="Habibi",
        length_seconds=0.0,
    )
    res = resolve_ordered_tracks(zero_in_plex, [both_zero])
    assert res.tracks == []
    assert [(m.item_id, m.reason) for m in res.missing] == [(1, "not_found")]

    real_length = _spec(
        "/music/gone.flac",
        albumartist="Wael Kfoury",
        album="Habibi",
        title="Habibi",
        length_seconds=254.0,
    )
    res = resolve_ordered_tracks(zero_in_plex, [real_length])
    assert res.tracks == []

    real_in_plex = _section(
        [
            _track(
                102,
                "/musicdrop/a.flac",
                artist=_ARABIC_WAEL,
                album="Habibi",
                title="Habibi",
                duration=254_000,
            )
        ]
    )
    res = resolve_ordered_tracks(real_in_plex, [both_zero])
    assert res.tracks == []


def test_plex_seconds_converts_milliseconds_and_rules_out_every_non_length() -> None:
    # The unit boundary and the "is there a length at all?" decision, tested
    # where they are decided rather than through a scenario that would refuse
    # for several reasons at once.
    #
    # plexapi's cast has THREE outcomes, not two (utils.py:171-184): None when
    # the server carried no `duration` attrib, the int, or float("nan") when it
    # carried one that would not parse. NaN loses EVERY comparison it takes part
    # in -- including the one meant to reject it -- so it has to be ruled out
    # here, as absent, and never handed to a tolerance check downstream.
    assert _plex_seconds(_track(1, "/a.flac", duration=254_000)) == 254.0
    assert _plex_seconds(_track(2, "/b.flac", duration=None)) is None
    assert _plex_seconds(_track(3, "/c.flac", duration=0)) is None
    assert _plex_seconds(_track(4, "/d.flac", duration=float("nan"))) is None
    assert _plex_seconds(object()) is None  # not even a duration attribute


def test_an_ambiguous_artist_match_is_never_re_answered_by_the_album_fallback() -> None:
    # Two Plex copies share album-artist, album and title and no track number
    # separates them: today that is `ambiguous`, and it stays `ambiguous`. The
    # (album, title) key is WIDER than the one that just refused -- it drops the
    # artist constraint -- so letting a length pick one of these two would be
    # guessing with a weaker key after a stronger one declined, and it would
    # also downgrade an honest "I found it twice" into "not found".
    section = _section(
        [
            _track(1, "/plex/a.flac", artist="X", album="Al", title="Song", duration=254_000),
            _track(2, "/plex/b.flac", artist="X", album="Al", title="Song", duration=185_000),
        ]
    )
    spec = _spec(
        "/beets/none.flac", albumartist="X", album="Al", title="Song", length_seconds=254.0
    )
    res = resolve_ordered_tracks(section, [spec])
    assert res.tracks == []
    assert [(m.item_id, m.reason) for m in res.missing] == [(1, "ambiguous")]


# --- how TIGHT the duration window is ----------------------------------------
#
# The safety of the whole (album, title) rung is the window being NARROW: it is
# what separates "the same recording, encoded twice" from "two different songs
# that share an album and a title". Both edges are asserted against LITERAL
# seconds -- a test that imported `_LENGTH_TOLERANCE_SECONDS` would move with the
# value it is supposed to pin and every widening would sail through it.


def _habibi_in_plex(
    rating_key: int, duration_millis: int | float | None, artist: str = _ARABIC_WAEL
) -> FakeTrack:
    """Plex's copy of the "Habibi" single, under the name PLEX holds it by
    (rewritten into Arabic unless a test says otherwise) and in Plex's unit,
    MILLISECONDS."""
    return _track(
        rating_key,
        f"/musicdrop/{rating_key}.flac",
        artist=artist,
        album="Habibi",
        title="Habibi",
        duration=duration_millis,
    )


def _habibi_spec(item_id: int = 1, length_seconds: float = 254.0) -> PlexTrackSpec:
    """The playlist row for it: beets' Latin spelling, a length in SECONDS, and
    a path Plex does not have (so only a fallback can resolve it)."""
    return _spec(
        f"/music/{item_id}.flac",
        item_id=item_id,
        albumartist="Wael Kfoury",
        album="Habibi",
        title="Habibi",
        length_seconds=length_seconds,
    )


def test_the_duration_window_reaches_exactly_two_seconds_either_side() -> None:
    # The INCLUSIVE edge, in both directions: 2.0s is the widest disagreement a
    # candidate may carry and still be accepted. Fails if the tolerance is ever
    # tightened below two seconds, or if the comparison stops being `<=`.
    for millis in (256_000, 252_000):  # the spec says 254.0s: +2.0s and -2.0s
        res = resolve_ordered_tracks(_section([_habibi_in_plex(80, millis)]), [_habibi_spec()])
        assert [t.ratingKey for t in res.tracks] == [80], millis


def test_the_duration_window_stops_before_two_and_a_half_seconds() -> None:
    # The other edge, in both directions. Together with the test above this pins
    # the window to [2.0, 2.5) seconds: a widening to 5s, 15s or 30s -- each of
    # which the suite used to accept in silence -- fails right here.
    for millis in (256_500, 251_500):  # the spec says 254.0s: +2.5s and -2.5s
        res = resolve_ordered_tracks(_section([_habibi_in_plex(81, millis)]), [_habibi_spec()])
        assert res.tracks == [], millis
        assert [(m.item_id, m.reason) for m in res.missing] == [(1, "not_found")]


# --- what Plex's OWN artist name is allowed to veto ---------------------------


def test_a_plex_artist_naming_someone_else_vetoes_the_duration_coincidence() -> None:
    # REPRODUCED against this module before the veto existed: Plex's "Habibi" is
    # credited to Nancy Ajram, 1.4s from the length we asked for, and that was
    # enough to hand it back and report the sync "ok". Both names are Latin, so
    # this is not the transliteration case the rung exists for -- it is Plex
    # telling us, in a name we can read, that this is a different recording.
    section = _section([_habibi_in_plex(3, 213_400, artist="Nancy Ajram")])
    res = resolve_ordered_tracks(section, [_habibi_spec(length_seconds=212.0)])
    assert res.tracks == []
    assert [(m.item_id, m.reason) for m in res.missing] == [(1, "not_found")]


def test_the_artist_veto_needs_nothing_exotic_to_fire() -> None:
    # The same hole with entirely ordinary names: two "Greatest Hits" albums
    # carrying a "Somebody to Love", a second apart. Album, title and length all
    # agree, so the artist is the only thing that knows better.
    section = _section(
        [
            _track(
                4,
                "/plex/boston.flac",
                artist="Boston",
                album="Greatest Hits",
                title="Somebody To Love",
                duration=285_000,
            )
        ]
    )
    spec = _spec(
        "/music/queen.flac",
        albumartist="Queen",
        album="Greatest Hits",
        title="Somebody to Love",
        length_seconds=284.0,
    )
    res = resolve_ordered_tracks(section, [spec])
    assert res.tracks == []
    assert [(m.item_id, m.reason) for m in res.missing] == [(1, "not_found")]


def test_a_contradicting_plex_artist_is_vetoed_not_tied() -> None:
    # The veto runs BEFORE the candidates are counted, so a rival Plex itself
    # rules out cannot make the row ambiguous. 92 wears the Arabic rewrite (no
    # shared alphabet, nothing to read), 93 says plainly it is somebody else, so
    # exactly one candidate survives and the row resolves to it.
    section = _section(
        [_habibi_in_plex(92, 254_000), _habibi_in_plex(93, 254_500, "Another Singer")]
    )
    res = resolve_ordered_tracks(section, [_habibi_spec()])
    assert [t.ratingKey for t in res.tracks] == [92]
    assert res.missing == []


def test_a_digit_difference_in_the_artist_name_is_vetoed_not_matched() -> None:
    # The rule ``_name_words`` cites: "Blink-182 must not read as Blink-183".
    # Digits are what distinguish two otherwise-identical names, so a Plex track
    # that differs ONLY in a digit is somebody else -- and the failure mode of
    # dropping that digit is a silent WRONG MATCH (the row would still land a
    # recording of the right album, title and length), not a mere miss.
    #
    # The pair is otherwise identical and the artist+title rung misses on the
    # digit alone, so the album rung is the one under test: same album, same
    # title, an agreeing duration -- and the veto is the only thing that knows
    # better. It must refuse, and loudly: the row comes back missing.
    section = _section(
        [
            _track(
                60,
                "/plex/blink183.flac",
                artist="Blink-183",
                album="Dude",
                title="Whatever",
                duration=254_000,
            )
        ]
    )
    spec = _spec(
        "/beets/blink182.flac",
        albumartist="Blink-182",
        album="Dude",
        title="Whatever",
        length_seconds=254.0,
    )
    res = resolve_ordered_tracks(section, [spec])
    assert res.tracks == []
    assert [(m.item_id, m.reason) for m in res.missing] == [(1, "not_found")]


def test_digits_agreeing_and_only_separator_and_case_differing_still_resolve() -> None:
    # The control arm for the rule above: the same digits with only separator
    # and case between the two spellings ("Blink-182" vs "blink 182") is ONE
    # name, and the album rung must still bridge it -- the veto fires on a
    # different name, not on a re-spelled one. The resolving rung is asserted,
    # because the (album-artist, title) rung misses on the hyphen and hiding it
    # in a plain tracks assertion would leave the bridge unproven.
    section = _section(
        [
            _track(
                61,
                "/plex/blink182.flac",
                artist="blink 182",
                album="Dude",
                title="Whatever",
                duration=254_000,
            )
        ]
    )
    spec = _spec(
        "/beets/gone.flac",
        albumartist="Blink-182",
        album="Dude",
        title="Whatever",
        length_seconds=254.0,
    )
    res = resolve_ordered_tracks(section, [spec])
    assert [(m.track.ratingKey, m.method) for m in res.matches] == [(61, "album_length")]
    assert res.missing == []


def test_the_veto_still_allows_the_rewrites_plexs_agent_actually_makes() -> None:
    # What the veto must NOT do: refuse a name Plex merely re-spelled. Each pair
    # is (what beets holds, what Plex holds) and every one of them must still
    # resolve -- a strict same-alphabet equality check would refuse all three.
    for beets_name, plex_name in [
        ("Beyonce", "Beyoncé"),  # a diacritic beets never had
        ("The Beatles", "Beatles, The"),  # the article moved to the back
        ("Ella Fitzgerald", "Fitzgerald, Ella"),  # the same inversion, no article to hide it
        # The article rule ALONE, with nothing else to carry the pair: "Beatles,
        # The" agrees whether or not "the" is dropped (the words are sorted, so
        # both sides hold it), and "Beyoncé" agrees on the NFKD fold. Only a Plex
        # name that simply lost the article shows that dropping it is doing work.
        ("The Doors", "Doors"),
        ("Wael Kfoury", f"{_ARABIC_WAEL} (Wael Kfoury)"),  # bilingual: the Latin halves agree
    ]:
        section = _section(
            [
                _track(
                    70,
                    "/plex/x.flac",
                    artist=plex_name,
                    album="Album",
                    title="Song",
                    duration=200_000,
                )
            ]
        )
        spec = _spec(
            "/music/gone.flac",
            albumartist=beets_name,
            album="Album",
            title="Song",
            length_seconds=200.0,
        )
        res = resolve_ordered_tracks(section, [spec])
        assert [t.ratingKey for t in res.tracks] == [70], plex_name


def test_a_blank_albumartist_is_refused_by_the_album_rung_outright() -> None:
    # The hole the veto could not see, reproduced through the real API: beets
    # stores "" for an untagged singleton and nothing back-fills it, so the veto
    # compares "" against Plex's name, finds no shared alphabet, and waves the
    # candidate through without ever having read a name. Album, title and a
    # length within a couple of seconds were then the whole case — and "Habibi"
    # is a single, so album == title and that key is TITLE-ONLY. Anyone's
    # "Habibi" of about the right length would answer.
    #
    # A blank is not the cross-script case: there the artist evidence exists and
    # merely cannot be read as text, here there is NO artist evidence at all —
    # and tags blank enough to lose the artist cast doubt on the album and title
    # the key is built from too. Both spellings of blank refuse, so the guard
    # cannot be written against the raw string.
    for untagged in ("", "   "):
        section = _section([_habibi_in_plex(3, 212_000, artist="Nancy Ajram")])
        spec = _spec(
            "/music/gone.flac",
            albumartist=untagged,
            album="Habibi",
            title="Habibi",
            length_seconds=212.0,
        )
        res = resolve_ordered_tracks(section, [spec])
        assert res.tracks == [], repr(untagged)
        assert [(m.item_id, m.reason) for m in res.missing] == [(1, "not_found")], repr(untagged)


def test_a_letterless_but_real_artist_name_still_reaches_the_length_guard() -> None:
    # What refusing a blank must NOT sweep up. "!!!", "3" and "+/-" are real
    # bands, and a letterless name stands exactly where "وائل كفوري" stands:
    # artist evidence that cannot be compared as text, not artist evidence that
    # is missing. Refusing them would strand real music, so the rung stays open
    # and the LENGTH decides — the second arm here, and the reason this is more
    # than an assertion that the name got past the door.
    #
    # Plex spells this band out ("Chk Chk Chk"), which is what keeps the
    # (album-artist, title) rung from answering first and hiding the whole
    # question — the rung that resolved it is asserted for the same reason.
    section = _section([_habibi_in_plex(4, 212_000, artist="Chk Chk Chk")])
    agreeing = _spec(
        "/music/gone.flac", albumartist="!!!", album="Habibi", title="Habibi", length_seconds=212.0
    )
    res = resolve_ordered_tracks(section, [agreeing])
    assert [(m.track.ratingKey, m.method) for m in res.matches] == [(4, "album_length")]

    disagreeing = _spec(
        "/music/gone.flac", albumartist="!!!", album="Habibi", title="Habibi", length_seconds=254.0
    )
    res = resolve_ordered_tracks(section, [disagreeing])
    assert res.tracks == []
    assert [(m.item_id, m.reason) for m in res.missing] == [(1, "not_found")]


# --- one Plex track answers at most one playlist row --------------------------


def test_two_playlist_rows_never_resolve_to_the_same_plex_track() -> None:
    # Two DIFFERENT library items, both a "Habibi" of about the same length,
    # against ONE Plex copy: both fit the window, so before the claim rule both
    # rows resolved to ratingKey 3. The user got the same song twice and at
    # least one of those two rows was showing a recording that isn't theirs.
    section = _section([_habibi_in_plex(3, 212_000)])
    res = resolve_ordered_tracks(
        section,
        [
            _habibi_spec(item_id=9, length_seconds=212.0),
            _habibi_spec(item_id=10, length_seconds=212.5),
        ],
    )
    assert [t.ratingKey for t in res.tracks] == [3]
    assert [(m.item_id, m.reason) for m in res.missing] == [(10, "not_found")]


def test_one_library_item_listed_twice_still_resolves_twice() -> None:
    # The control arm: a playlist may hold the same track twice on purpose, and
    # the sync goes out of its way to support it (sync._unique_key_chunks). Only
    # a DIFFERENT item is locked out of a track someone else took.
    section = _section([_habibi_in_plex(3, 212_000)])
    spec = _habibi_spec(item_id=9, length_seconds=212.0)
    res = resolve_ordered_tracks(section, [spec, spec])
    assert [t.ratingKey for t in res.tracks] == [3, 3]
    assert res.missing == []


def test_an_exact_path_owns_its_track_even_from_a_later_row() -> None:
    # Row one can only fall back; row two holds the FILE. Claiming as we went
    # would let the fallback take the very track row two is the file for, and
    # the playlist would carry it twice with row two looking perfectly fine.
    section = _section([_habibi_in_plex(3, 212_000)])
    holds_the_file = _spec(
        "/musicdrop/3.flac",
        item_id=10,
        albumartist="Wael Kfoury",
        album="Habibi",
        title="Habibi",
        length_seconds=212.0,
    )
    res = resolve_ordered_tracks(
        section, [_habibi_spec(item_id=9, length_seconds=212.0), holds_the_file]
    )
    assert [t.ratingKey for t in res.tracks] == [3]
    assert [(m.item_id, m.reason) for m in res.missing] == [(9, "not_found")]


def test_the_album_artist_rung_locks_its_track_too() -> None:
    # The same collision one rung up -- two library items sharing an album-artist
    # and a title (a studio copy and a compilation copy) against one Plex track.
    # Fixed in BOTH fallbacks: the doubled row is the same bug for the user
    # wherever the inexact match came from.
    section = _section([_track(12, "/plex/x.flac", artist="Adele", title="Hello")])
    res = resolve_ordered_tracks(
        section,
        [
            _spec("/music/a.flac", item_id=1, albumartist="Adele", title="Hello"),
            _spec("/music/b.flac", item_id=2, albumartist="Adele", title="Hello"),
        ],
    )
    assert [t.ratingKey for t in res.tracks] == [12]
    assert [(m.item_id, m.reason) for m in res.missing] == [(2, "not_found")]


# --- which rung did the work --------------------------------------------------


def test_every_match_records_the_rung_that_resolved_it() -> None:
    # Why this is worth carrying: this app's path matching was broken for its
    # ENTIRE life and nobody noticed, because every sync reported a flat "ok"
    # while the metadata fallback quietly carried 100% of the traffic. A tally
    # is what makes that visible, so each rung has to be counted as itself.
    section = _section(
        [
            _track(1, "/plex/exact.flac"),
            _track(2, "/plex/meta.flac", artist="Adele", title="Hello"),
            _habibi_in_plex(3, 212_000),
        ]
    )
    res = resolve_ordered_tracks(
        section,
        [
            _spec("/plex/exact.flac", item_id=1),
            _spec("/music/b.flac", item_id=2, albumartist="Adele", title="Hello"),
            _habibi_spec(item_id=3, length_seconds=212.0),
            _spec("/music/nowhere.flac", item_id=4, albumartist="Nobody", title="Nothing"),
        ],
    )
    assert [(m.track.ratingKey, m.method) for m in res.matches] == [
        (1, "path"),
        (2, "artist_title"),
        (3, "album_length"),
    ]
    assert res.matched_by.model_dump() == {"path": 1, "artist_title": 1, "album_length": 1}
    assert res.tracks == [m.track for m in res.matches]  # the plain view stays in step
    assert [m.item_id for m in res.missing] == [4]


def test_every_match_method_has_a_field_to_be_counted_in() -> None:
    # The tally is built BY NAME, so a rung added to PlexMatchMethod without a
    # field on PlexMatchCounts would be dropped in silence: the sync would report
    # fewer matches than it made and no test would notice.
    assert set(get_args(PlexMatchMethod)) == set(PlexMatchCounts.model_fields)
