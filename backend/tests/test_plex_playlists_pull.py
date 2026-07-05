import pytest

from app.models.plex import PlexPlaylistInfo
from app.plex import playlists_pull
from app.plex.config import PlexConfig
from app.plex.errors import PlexConnectionError, PlexNotConfigured

CONFIG = PlexConfig(base_url="http://plex:32400", token="t")


class _FakeItem:
    def __init__(self, title: str, artist: str, album: str, ms: int, path: str) -> None:
        self.title = title
        self.grandparentTitle = artist
        self.parentTitle = album
        self.duration = ms
        self.locations = [path]


class _FakePlaylist:
    def __init__(self, title: str, playlist_type: str, items: list[_FakeItem]) -> None:
        self.title = title
        self.playlistType = playlist_type
        self._items = items

    def items(self) -> list[_FakeItem]:
        return list(self._items)


class _FakeServer:
    def __init__(self, playlists: list[_FakePlaylist]) -> None:
        self._pls = playlists

    def playlists(self) -> list[_FakePlaylist]:
        return list(self._pls)


def _patch(monkeypatch: pytest.MonkeyPatch, server: _FakeServer) -> None:
    monkeypatch.setattr(playlists_pull.client, "connect", lambda url, token: server)


def test_list_audio_playlists_filters_video(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakeServer(
        [
            _FakePlaylist("Road", "audio", [_FakeItem("t", "a", "b", 1000, "/p")]),
            _FakePlaylist("Clips", "video", []),
        ]
    )
    _patch(monkeypatch, server)
    assert playlists_pull.list_audio_playlists(CONFIG) == [
        PlexPlaylistInfo(name="Road", track_count=1)
    ]


def test_pull_playlist_entries_maps_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    item = _FakeItem(
        "Around the World", "Daft Punk", "Homework", 213000, "/old/M/01 Around the World.mp3"
    )
    server = _FakeServer([_FakePlaylist("Road", "audio", [item])])
    _patch(monkeypatch, server)
    (parsed,) = playlists_pull.pull_playlist_entries(CONFIG, ["Road"])
    assert parsed.name == "Road"
    (entry,) = parsed.entries
    assert entry.artist == "Daft Punk" and entry.title == "Around the World"
    assert entry.album == "Homework"
    assert entry.duration_seconds == 213.0
    assert entry.path == "/old/M/01 Around the World.mp3"
    assert entry.source == "plex:Road"


def test_pull_unknown_playlist_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _FakeServer([]))
    with pytest.raises(PlexConnectionError):
        playlists_pull.pull_playlist_entries(CONFIG, ["Nope"])


def test_pull_requires_config() -> None:
    with pytest.raises(PlexNotConfigured):
        playlists_pull.list_audio_playlists(PlexConfig())
