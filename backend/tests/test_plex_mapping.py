import pytest

from app.plex.client import music_section
from app.plex.errors import PlexConnectionError
from app.plex.mapping import PlexResolution, PlexTrackSpec, resolve_ordered_tracks
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
) -> FakeTrack:
    """One Plex track at ``path``, its plexapi fields named the way a playlist
    spec talks about them (grandparentTitle = album-artist, parentTitle = album)."""
    return FakeTrack(
        rating_key,
        [path],
        grandparentTitle=artist,
        parentTitle=album,
        title=title,
        index=index,
    )


def _section(tracks: list[FakeTrack]) -> FakeSection:
    return FakeSection(tracks)


def _spec(path: str, item_id: int = 1, **meta: object) -> PlexTrackSpec:
    return PlexTrackSpec(
        item_id=item_id,
        path=path,
        albumartist=str(meta.get("albumartist", "")),
        album=str(meta.get("album", "")),
        title=str(meta.get("title", "")),
        track=meta.get("track"),  # type: ignore[arg-type]  # int | None
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
                item_id=11, path="/plex/a.flac", albumartist="A", album="X", title="one", track=1
            ),
            PlexTrackSpec(
                item_id=12, path="/plex/gone.flac", albumartist="B", album="Y", title="two", track=2
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
                item_id=7, path="/beets/z.flac", albumartist="", album="", title="one", track=None
            )
        ],
    )
    assert [(m.item_id, m.reason) for m in res.missing] == [(7, "not_found")]
