from app.plex.client import music_section
from app.plex.mapping import index_tracks_by_path, resolve_ordered_tracks


class _FakeTrack:
    def __init__(self, rating_key: int, locations: list[str]) -> None:
        self.ratingKey = rating_key
        self.locations = locations


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


def test_resolve_ordered_tracks_preserves_order_and_counts_missing() -> None:
    t1 = _FakeTrack(10, ["/data/music/A/1.flac"])
    t2 = _FakeTrack(20, ["/data/music/B/2.flac"])
    section = _FakeSection([t1, t2])
    tracks, missing = resolve_ordered_tracks(
        section, ["/data/music/B/2.flac", "/data/music/GONE.flac", "/data/music/A/1.flac"]
    )
    assert [t.ratingKey for t in tracks] == [20, 10]
    assert missing == 1
