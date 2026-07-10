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
    """Serves per-URL bytes (substring match), falling back to a default.

    ``requests`` records every fetched URL in order so tests can assert which
    resource was pulled (the custom poster key vs the composite thumb).
    """

    def __init__(self, content: bytes = _PNG, by_url: dict[str, bytes] | None = None) -> None:
        self._content = content
        self._by_url = by_url or {}
        self.requested: str | None = None
        self.requests: list[str] = []

    def get(self, url: str) -> _FakeResponse:
        self.requested = url
        self.requests.append(url)
        for fragment, data in self._by_url.items():
            if fragment in url:
                return _FakeResponse(data)
        return _FakeResponse(self._content)


class _FakePoster:
    """Mirrors plexapi.media.Poster (BaseResource): ``key`` + ``selected``."""

    def __init__(self, key: str | None, selected: bool) -> None:
        self.key = key
        self.selected = selected


class _PosterPlaylist:
    def __init__(
        self,
        title: str,
        playlist_type: str,
        thumb: str | None,
        posters: list[_FakePoster] | None = None,
        posters_error: Exception | None = None,
    ) -> None:
        self.title = title
        self.playlistType = playlist_type
        # plexapi's ``thumb`` is a property aliasing ``composite`` (the mosaic).
        self.thumb = thumb
        self._posters = posters or []
        self._posters_error = posters_error

    def posters(self) -> list[_FakePoster]:
        if self._posters_error is not None:
            raise self._posters_error
        return list(self._posters)


class _PosterServer:
    def __init__(
        self,
        playlists: list[_PosterPlaylist],
        content: bytes = _PNG,
        by_url: dict[str, bytes] | None = None,
    ) -> None:
        self._pls = playlists
        self._session = _FakeSession(content, by_url)

    def playlists(self) -> list[_PosterPlaylist]:
        return list(self._pls)

    def url(self, path: str, includeToken: bool = False) -> str:
        return f"http://plex:32400{path}?token=t" if includeToken else f"http://plex:32400{path}"


def _patch_poster(monkeypatch: pytest.MonkeyPatch, server: _PosterServer) -> None:
    monkeypatch.setattr(playlists_pull.client, "connect", lambda url, token: server)


def test_download_poster_prefers_selected_custom_poster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Playlist carries BOTH a composite mosaic (thumb) and a selected custom poster.
    poster_key = "/library/metadata/1/posters/upload-abc"
    playlist = _PosterPlaylist(
        "Road",
        "audio",
        "/library/metadata/1/composite/2",
        posters=[
            _FakePoster("/library/metadata/1/posters/plex-default", selected=False),
            _FakePoster(poster_key, selected=True),
        ],
    )
    server = _PosterServer(
        [playlist],
        by_url={"/composite/": _PNG, "/posters/": _JPG},
    )
    _patch_poster(monkeypatch, server)
    result = playlists_pull.download_poster(CONFIG, "Road")
    assert result is not None
    data, fmt = result
    # The CUSTOM poster bytes (jpg) win over the composite mosaic (png).
    assert data == _JPG and fmt == "jpg"
    # The fetched URL was the selected poster key, token included, not the composite.
    assert server._session.requested is not None
    assert poster_key in server._session.requested
    assert "token=t" in server._session.requested
    assert "/composite/" not in server._session.requested


def test_download_poster_no_selected_poster_falls_back_to_composite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No selected poster (present but none selected) -> composite mosaic is used.
    playlist = _PosterPlaylist(
        "Road",
        "audio",
        "/library/metadata/1/composite/2",
        posters=[_FakePoster("/library/metadata/1/posters/plex-default", selected=False)],
    )
    server = _PosterServer([playlist], content=_PNG)
    _patch_poster(monkeypatch, server)
    result = playlists_pull.download_poster(CONFIG, "Road")
    assert result is not None
    data, fmt = result
    assert data == _PNG and fmt == "png"
    assert server._session.requested is not None and "/composite/" in server._session.requested


def test_download_poster_empty_posters_falls_back_to_composite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    playlist = _PosterPlaylist("Road", "audio", "/library/metadata/1/composite/2", posters=[])
    server = _PosterServer([playlist], content=_JPG)
    _patch_poster(monkeypatch, server)
    result = playlists_pull.download_poster(CONFIG, "Road")
    assert result is not None and result[1] == "jpg"


def test_download_poster_posters_raise_falls_back_to_composite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A posters() failure must NOT abort the pull -- degrade to the composite.
    playlist = _PosterPlaylist(
        "Road",
        "audio",
        "/library/metadata/1/composite/2",
        posters_error=RuntimeError("boom"),
    )
    server = _PosterServer([playlist], content=_PNG)
    _patch_poster(monkeypatch, server)
    result = playlists_pull.download_poster(CONFIG, "Road")
    assert result is not None
    data, fmt = result
    assert data == _PNG and fmt == "png"


def test_download_poster_custom_bytes_fail_sniff_falls_back_to_composite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Selected poster is e.g. webp (sniff rejects) -> fall through to the composite.
    poster_key = "/library/metadata/1/posters/webp"
    playlist = _PosterPlaylist(
        "Road",
        "audio",
        "/library/metadata/1/composite/2",
        posters=[_FakePoster(poster_key, selected=True)],
    )
    server = _PosterServer(
        [playlist],
        by_url={"/posters/": b"RIFF....WEBP", "/composite/": _PNG},
    )
    _patch_poster(monkeypatch, server)
    result = playlists_pull.download_poster(CONFIG, "Road")
    assert result is not None
    data, fmt = result
    # The webp custom poster was fetched first, then rejected; composite won.
    assert data == _PNG and fmt == "png"
    assert poster_key in server._session.requests[0]
    assert any("/composite/" in u for u in server._session.requests)


def test_download_poster_selected_poster_without_key_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    playlist = _PosterPlaylist(
        "Road",
        "audio",
        "/library/metadata/1/composite/2",
        posters=[_FakePoster(None, selected=True)],
    )
    server = _PosterServer([playlist], content=_PNG)
    _patch_poster(monkeypatch, server)
    result = playlists_pull.download_poster(CONFIG, "Road")
    assert result is not None and result[1] == "png"
    assert server._session.requested is not None and "/composite/" in server._session.requested


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
