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


# --- download_poster (Task 3) -------------------------------------------------

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
_JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 8


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        pass


class _FakeSession:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self.requested: str | None = None

    def get(self, url: str) -> _FakeResponse:
        self.requested = url
        return _FakeResponse(self._content)


class _PosterPlaylist:
    def __init__(self, title: str, playlist_type: str, thumb: str | None) -> None:
        self.title = title
        self.playlistType = playlist_type
        self.thumb = thumb


class _PosterServer:
    def __init__(self, playlists: list[_PosterPlaylist], content: bytes = _PNG) -> None:
        self._pls = playlists
        self._session = _FakeSession(content)

    def playlists(self) -> list[_PosterPlaylist]:
        return list(self._pls)

    def url(self, path: str, includeToken: bool = False) -> str:
        return f"http://plex:32400{path}?token=t" if includeToken else f"http://plex:32400{path}"


def _patch_poster(monkeypatch: pytest.MonkeyPatch, server: _PosterServer) -> None:
    monkeypatch.setattr(playlists_pull.client, "connect", lambda url, token: server)


def test_download_poster_returns_bytes_and_sniffed_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _PosterServer(
        [_PosterPlaylist("Road", "audio", "/library/metadata/1/composite/2")], content=_PNG
    )
    _patch_poster(monkeypatch, server)
    result = playlists_pull.download_poster(CONFIG, "Road")
    assert result is not None
    data, fmt = result
    assert data == _PNG and fmt == "png"
    # The thumb was fetched through the server's authed session (token-carrying url).
    assert server._session.requested is not None and "token=t" in server._session.requested


def test_download_poster_sniffs_jpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _PosterServer([_PosterPlaylist("Road", "audio", "/thumb/1")], content=_JPG)
    _patch_poster(monkeypatch, server)
    result = playlists_pull.download_poster(CONFIG, "Road")
    assert result is not None and result[1] == "jpg"


def test_download_poster_missing_playlist_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_poster(monkeypatch, _PosterServer([]))
    assert playlists_pull.download_poster(CONFIG, "Nope") is None


def test_download_poster_no_thumb_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _PosterServer([_PosterPlaylist("Road", "audio", None)])
    _patch_poster(monkeypatch, server)
    assert playlists_pull.download_poster(CONFIG, "Road") is None


def test_download_poster_unsupported_bytes_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _PosterServer([_PosterPlaylist("Road", "audio", "/thumb/1")], content=b"GIF89a....")
    _patch_poster(monkeypatch, server)
    assert playlists_pull.download_poster(CONFIG, "Road") is None


def test_download_poster_requires_config() -> None:
    with pytest.raises(PlexNotConfigured):
        playlists_pull.download_poster(PlexConfig(), "Road")
