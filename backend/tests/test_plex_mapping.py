from app.plex.client import music_section
from app.plex.mapping import PlexTrackSpec, index_tracks_by_path, resolve_ordered_tracks


class _FakeTrack:
    def __init__(
        self,
        rating_key: int,
        locations: list[str],
        *,
        grandparentTitle: str = "",
        parentTitle: str = "",
        title: str = "",
        index: int | None = None,
    ) -> None:
        self.ratingKey = rating_key
        self.locations = locations
        self.grandparentTitle = grandparentTitle
        self.parentTitle = parentTitle
        self.title = title
        self.index = index


class _FakeSection:
    TYPE = "artist"

    def __init__(self, tracks: list[_FakeTrack]) -> None:
        self._tracks = tracks

    def searchTracks(self) -> list[_FakeTrack]:
        return self._tracks


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


def test_index_maps_each_location_to_rating_key() -> None:
    section = _FakeSection(
        [
            _FakeTrack(10, ["/data/music/A/1.flac"]),
            _FakeTrack(20, ["/data/music/B/2.flac", "/data/music/B/2.alt.flac"]),
        ]
    )
    index = index_tracks_by_path(section)
    assert index["/data/music/A/1.flac"] == 10
    assert index["/data/music/B/2.flac"] == 20
    assert index["/data/music/B/2.alt.flac"] == 20


def test_music_section_picks_artist_type() -> None:
    artist = _FakeSection([])
    server = _FakeServer([_FakeOtherSection(), artist])
    assert music_section(server) is artist


def test_music_section_none_when_absent() -> None:
    server = _FakeServer([_FakeOtherSection()])
    assert music_section(server) is None


def _spec(path: str, **meta: object) -> PlexTrackSpec:
    return PlexTrackSpec(
        path=path,
        albumartist=str(meta.get("albumartist", "")),
        album=str(meta.get("album", "")),
        title=str(meta.get("title", "")),
        track=meta.get("track"),  # type: ignore[arg-type]  # int | None
    )


def test_resolve_ordered_tracks_preserves_order_and_counts_missing() -> None:
    t1 = _FakeTrack(10, ["/data/music/A/1.flac"])
    t2 = _FakeTrack(20, ["/data/music/B/2.flac"])
    section = _FakeSection([t1, t2])
    tracks, missing = resolve_ordered_tracks(
        section,
        [
            _spec("/data/music/B/2.flac"),
            _spec("/data/music/GONE.flac"),
            _spec("/data/music/A/1.flac"),
        ],
    )
    assert [t.ratingKey for t in tracks] == [20, 10]
    assert missing == 1


def test_path_miss_falls_back_to_unique_artist_title() -> None:
    # Plex file is named differently; only metadata can bridge it.
    t = _FakeTrack(
        10,
        ["/plex/Adele_19_01_Daydreamer.flac"],
        grandparentTitle="Adele",
        parentTitle="19",
        title="Daydreamer",
        index=1,
    )
    section = _FakeSection([t])
    spec = _spec(
        "/beets/Adele/19/01 Daydreamer.flac",
        albumartist="Adele",
        album="19",
        title="Daydreamer",
        track=1,
    )
    tracks, missing = resolve_ordered_tracks(section, [spec])
    assert [x.ratingKey for x in tracks] == [10]
    assert missing == 0


def test_fallback_is_case_and_whitespace_insensitive() -> None:
    t = _FakeTrack(11, ["/plex/x.flac"], grandparentTitle="Adele", title="Daydreamer")
    section = _FakeSection([t])
    spec = _spec("/beets/none.flac", albumartist="  ADELE ", title="DAYDREAMER")
    tracks, missing = resolve_ordered_tracks(section, [spec])
    assert [x.ratingKey for x in tracks] == [11]
    assert missing == 0


def test_fallback_disambiguates_same_title_by_artist() -> None:
    # "What's Up" exists under two album-artists; the albumartist key disambiguates.
    blondes = _FakeTrack(1, ["/plex/a.flac"], grandparentTitle="4 Non Blondes", title="What's Up")
    dalmatians = _FakeTrack(
        2, ["/plex/b.flac"], grandparentTitle="The Cast of 101 Dalmatians", title="What's Up"
    )
    section = _FakeSection([blondes, dalmatians])
    spec = _spec("/beets/none.flac", albumartist="4 Non Blondes", title="What's Up")
    tracks, missing = resolve_ordered_tracks(section, [spec])
    assert [x.ratingKey for x in tracks] == [1]
    assert missing == 0


def test_fallback_album_then_track_tiebreak() -> None:
    # Same artist+title twice -> album breaks the tie.
    studio = _FakeTrack(
        1, ["/plex/s.flac"], grandparentTitle="X", parentTitle="Album", title="Song", index=3
    )
    live = _FakeTrack(
        2, ["/plex/l.flac"], grandparentTitle="X", parentTitle="Live", title="Song", index=9
    )
    section = _FakeSection([studio, live])
    spec = _spec("/beets/none.flac", albumartist="X", album="Album", title="Song", track=3)
    tracks, missing = resolve_ordered_tracks(section, [spec])
    assert [x.ratingKey for x in tracks] == [1]
    assert missing == 0


def test_fallback_ambiguous_is_missing_not_guessed() -> None:
    # Same artist+title+album, no track to break the tie -> never guess.
    a = _FakeTrack(1, ["/plex/a.flac"], grandparentTitle="X", parentTitle="Al", title="Song")
    b = _FakeTrack(2, ["/plex/b.flac"], grandparentTitle="X", parentTitle="Al", title="Song")
    section = _FakeSection([a, b])
    spec = _spec("/beets/none.flac", albumartist="X", album="Al", title="Song")
    tracks, missing = resolve_ordered_tracks(section, [spec])
    assert tracks == []
    assert missing == 1


def test_no_metadata_hit_is_missing() -> None:
    section = _FakeSection([_FakeTrack(1, ["/plex/a.flac"], grandparentTitle="X", title="Other")])
    spec = _spec("/beets/none.flac", albumartist="X", title="Nope")
    tracks, missing = resolve_ordered_tracks(section, [spec])
    assert tracks == []
    assert missing == 1


def test_empty_metadata_spec_never_metadata_matches() -> None:
    # A path-only spec (no album-artist/title) must not collide with a track that
    # also happens to have empty metadata — only an exact path can resolve it.
    section = _FakeSection([_FakeTrack(1, ["/plex/a.flac"])])
    tracks, missing = resolve_ordered_tracks(section, [_spec("/beets/gone.flac")])
    assert tracks == []
    assert missing == 1


def test_path_match_wins_over_metadata() -> None:
    # An exact path present -> used directly, metadata never consulted.
    t = _FakeTrack(10, ["/plex/exact.flac"], grandparentTitle="X", title="Song")
    section = _FakeSection([t])
    spec = _spec("/plex/exact.flac", albumartist="DIFFERENT", title="DIFFERENT")
    tracks, missing = resolve_ordered_tracks(section, [spec])
    assert [x.ratingKey for x in tracks] == [10]
    assert missing == 0
