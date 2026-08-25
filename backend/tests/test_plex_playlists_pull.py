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
    def __init__(
        self,
        title: str,
        playlist_type: str,
        items: list[_FakeItem],
        leaf_count: int | None = None,
        rating_key: int | str = 1,
    ) -> None:
        self.title = title
        self.playlistType = playlist_type
        # plexapi's ratingKey is the server-side identity (an int on the wire);
        # titles are NOT unique, ratingKeys are.
        self.ratingKey = rating_key
        self._items = items
        # Real plexapi Playlist objects carry leafCount from the initial
        # server.playlists() response — no extra request.
        self.leafCount = len(items) if leaf_count is None else leaf_count
        self.items_calls = 0

    def items(self) -> list[_FakeItem]:
        self.items_calls += 1  # a real .items() is a full per-playlist track fetch
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
            _FakePlaylist("Road", "audio", [_FakeItem("t", "a", "b", 1000, "/p")], rating_key=42),
            _FakePlaylist("Clips", "video", [], rating_key=43),
        ]
    )
    _patch(monkeypatch, server)
    result = playlists_pull.list_audio_playlists(CONFIG)
    assert result == [
        PlexPlaylistInfo(name="Road", track_count=1, rating_key="42")  # int ratingKey stringified
    ]
    assert result[0].rating_key == "42"


def test_list_audio_playlists_counts_via_leafcount_without_fetching_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The picker's track_count must come from the already-loaded leafCount, NOT a
    per-playlist items() fetch (a full track-metadata download, many MB over a NAS).
    leaf_count is set distinct from the (unfetched) items length to prove the source."""
    pl = _FakePlaylist("Big", "audio", [_FakeItem("t", "a", "b", 1000, "/p")], leaf_count=4200)
    _patch(monkeypatch, _FakeServer([pl]))
    result = playlists_pull.list_audio_playlists(CONFIG)
    # leafCount, not len(items)
    assert result == [PlexPlaylistInfo(name="Big", track_count=4200, rating_key="1")]
    assert pl.items_calls == 0  # never paid the full-contents fetch just to count


def test_list_audio_playlists_keeps_duplicate_titles_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plex allows duplicate playlist titles. Both must be listed, each with its
    OWN ratingKey — otherwise one of them can never be picked."""
    server = _FakeServer(
        [
            _FakePlaylist("Road", "audio", [_FakeItem("t1", "a", "b", 1000, "/p1")], rating_key=11),
            _FakePlaylist(
                "Road",
                "audio",
                [_FakeItem("t2", "a", "b", 1000, "/p2"), _FakeItem("t3", "a", "b", 1000, "/p3")],
                rating_key=22,
            ),
        ]
    )
    _patch(monkeypatch, server)
    result = playlists_pull.list_audio_playlists(CONFIG)
    assert result == [
        PlexPlaylistInfo(name="Road", track_count=1, rating_key="11"),
        PlexPlaylistInfo(name="Road", track_count=2, rating_key="22"),
    ]
    assert [pl.rating_key for pl in result] == ["11", "22"]  # identity, not the shared title


def test_pull_playlist_entries_maps_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    item = _FakeItem(
        "Around the World", "Daft Punk", "Homework", 213000, "/old/M/01 Around the World.mp3"
    )
    server = _FakeServer([_FakePlaylist("Road", "audio", [item], rating_key=7)])
    _patch(monkeypatch, server)
    (parsed,) = playlists_pull.pull_playlist_entries(CONFIG, ["7"])
    assert parsed.name == "Road"  # display stays the TITLE, not the key
    (entry,) = parsed.entries
    assert entry.artist == "Daft Punk"
    assert entry.title == "Around the World"
    assert entry.album == "Homework"
    assert entry.duration_seconds == 213.0
    assert entry.path == "/old/M/01 Around the World.mp3"
    assert entry.source == "plex:Road"


def test_pull_playlist_entries_resolves_duplicate_titles_by_rating_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core regression: two same-titled playlists must each pull their OWN
    entries. Keyed by title, the second shadowed the first and one was
    unreachable forever."""
    first = _FakePlaylist(
        "Road", "audio", [_FakeItem("First", "A", "AA", 1000, "/1.mp3")], rating_key=11
    )
    second = _FakePlaylist(
        "Road", "audio", [_FakeItem("Second", "B", "BB", 2000, "/2.mp3")], rating_key=22
    )
    _patch(monkeypatch, _FakeServer([first, second]))

    (one,) = playlists_pull.pull_playlist_entries(CONFIG, ["11"])
    assert one.name == "Road"
    assert [e.title for e in one.entries] == ["First"]

    (two,) = playlists_pull.pull_playlist_entries(CONFIG, ["22"])
    assert two.name == "Road"
    assert [e.title for e in two.entries] == ["Second"]


def test_pull_playlist_entries_preserves_requested_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The FE aligns the preview list with the keys it sent by INDEX, so the
    results must come back in the requested order, not the server's."""
    first = _FakePlaylist("A", "audio", [_FakeItem("t1", "x", "y", 1, "/1")], rating_key=11)
    second = _FakePlaylist("B", "audio", [_FakeItem("t2", "x", "y", 1, "/2")], rating_key=22)
    _patch(monkeypatch, _FakeServer([first, second]))
    parsed = playlists_pull.pull_playlist_entries(CONFIG, ["22", "11"])
    assert [p.name for p in parsed] == ["B", "A"]


def test_pull_unknown_playlist_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _FakeServer([]))
    with pytest.raises(PlexConnectionError):
        playlists_pull.pull_playlist_entries(CONFIG, ["999"])


def test_pull_not_found_message_is_actionable_and_hides_rating_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This detail is surfaced verbatim to the user, so it must say what to DO —
    a raw numeric ratingKey names nothing the user can recognise."""
    _patch(monkeypatch, _FakeServer([_FakePlaylist("Road", "audio", [], rating_key=11)]))
    with pytest.raises(PlexConnectionError) as ei:
        playlists_pull.pull_playlist_entries(CONFIG, ["999", "1000"])
    message = str(ei.value)
    assert "999" not in message  # no opaque ids leaked
    assert "1000" not in message  # no opaque ids leaked
    assert "2 selected Plex playlists" in message
    assert "refresh the list" in message


def test_pull_by_title_no_longer_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Identity is the ratingKey now — a stale title-keyed request fails loudly
    instead of silently pulling a same-titled stranger."""
    _patch(monkeypatch, _FakeServer([_FakePlaylist("Road", "audio", [], rating_key=11)]))
    with pytest.raises(PlexConnectionError):
        playlists_pull.pull_playlist_entries(CONFIG, ["Road"])


def test_pull_requires_config() -> None:
    config = PlexConfig()
    with pytest.raises(PlexNotConfigured):
        playlists_pull.list_audio_playlists(config)


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
        rating_key: int | str = 1,
    ) -> None:
        self.title = title
        self.playlistType = playlist_type
        self.ratingKey = rating_key
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
    result = playlists_pull.download_poster(CONFIG, "1")
    assert result is not None
    data, fmt = result
    # The CUSTOM poster bytes (jpg) win over the composite mosaic (png).
    assert data == _JPG
    assert fmt == "jpg"
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
    result = playlists_pull.download_poster(CONFIG, "1")
    assert result is not None
    data, fmt = result
    assert data == _PNG
    assert fmt == "png"
    assert server._session.requested is not None
    assert "/composite/" in server._session.requested


def test_download_poster_empty_posters_falls_back_to_composite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    playlist = _PosterPlaylist("Road", "audio", "/library/metadata/1/composite/2", posters=[])
    server = _PosterServer([playlist], content=_JPG)
    _patch_poster(monkeypatch, server)
    result = playlists_pull.download_poster(CONFIG, "1")
    assert result is not None
    assert result[1] == "jpg"


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
    result = playlists_pull.download_poster(CONFIG, "1")
    assert result is not None
    data, fmt = result
    assert data == _PNG
    assert fmt == "png"


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
    result = playlists_pull.download_poster(CONFIG, "1")
    assert result is not None
    data, fmt = result
    # The webp custom poster was fetched first, then rejected; composite won.
    assert data == _PNG
    assert fmt == "png"
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
    result = playlists_pull.download_poster(CONFIG, "1")
    assert result is not None
    assert result[1] == "png"
    assert server._session.requested is not None
    assert "/composite/" in server._session.requested


def test_download_poster_returns_bytes_and_sniffed_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _PosterServer(
        [_PosterPlaylist("Road", "audio", "/library/metadata/1/composite/2")], content=_PNG
    )
    _patch_poster(monkeypatch, server)
    result = playlists_pull.download_poster(CONFIG, "1")
    assert result is not None
    data, fmt = result
    assert data == _PNG
    assert fmt == "png"
    # The thumb was fetched through the server's authed session (token-carrying url).
    assert server._session.requested is not None
    assert "token=t" in server._session.requested


def test_download_poster_sniffs_jpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _PosterServer([_PosterPlaylist("Road", "audio", "/thumb/1")], content=_JPG)
    _patch_poster(monkeypatch, server)
    result = playlists_pull.download_poster(CONFIG, "1")
    assert result is not None
    assert result[1] == "jpg"


def test_download_poster_missing_playlist_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_poster(monkeypatch, _PosterServer([]))
    assert playlists_pull.download_poster(CONFIG, "999") is None


def test_download_poster_no_thumb_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _PosterServer([_PosterPlaylist("Road", "audio", None)])
    _patch_poster(monkeypatch, server)
    assert playlists_pull.download_poster(CONFIG, "1") is None


def test_download_poster_unsupported_bytes_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _PosterServer([_PosterPlaylist("Road", "audio", "/thumb/1")], content=b"GIF89a....")
    _patch_poster(monkeypatch, server)
    assert playlists_pull.download_poster(CONFIG, "1") is None


def test_download_poster_requires_config() -> None:
    config = PlexConfig()
    with pytest.raises(PlexNotConfigured):
        playlists_pull.download_poster(config, "1")


def test_download_poster_resolves_duplicate_titles_by_rating_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two same-titled playlists: the poster must come from the one the import
    actually pulled, not whichever the server happened to list first."""
    first = _PosterPlaylist("Road", "audio", "/library/metadata/11/composite/1", rating_key=11)
    second = _PosterPlaylist("Road", "audio", "/library/metadata/22/composite/1", rating_key=22)
    server = _PosterServer(
        [first, second],
        by_url={"/metadata/11/": _PNG, "/metadata/22/": _JPG},
    )
    _patch_poster(monkeypatch, server)
    result = playlists_pull.download_poster(CONFIG, "22")
    assert result is not None
    data, fmt = result
    assert data == _JPG
    assert fmt == "jpg"  # the SECOND playlist's composite
    assert server._session.requested is not None
    assert "/metadata/22/" in server._session.requested


def test_download_poster_falls_back_to_title_without_a_rating_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Back-compat: a request minted before rating keys existed carries only the
    title, so first-match-by-title still resolves (best-effort, as before)."""
    server = _PosterServer(
        [_PosterPlaylist("Road", "audio", "/library/metadata/11/composite/1", rating_key=11)],
        content=_PNG,
    )
    _patch_poster(monkeypatch, server)
    result = playlists_pull.download_poster(CONFIG, None, "Road")
    assert result is not None
    assert result[1] == "png"


def test_download_poster_prefers_the_key_over_the_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With both supplied the KEY decides — a stale/renamed title must never
    redirect the pull to a same-titled stranger."""
    server = _PosterServer(
        [
            _PosterPlaylist("Road", "audio", "/library/metadata/11/composite/1", rating_key=11),
            _PosterPlaylist("Road", "audio", "/library/metadata/22/composite/1", rating_key=22),
        ],
        by_url={"/metadata/11/": _PNG, "/metadata/22/": _JPG},
    )
    _patch_poster(monkeypatch, server)
    result = playlists_pull.download_poster(CONFIG, "22", "Road")
    assert result is not None
    assert result[0] == _JPG


def test_download_poster_unknown_key_does_not_fall_back_to_the_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key that no longer exists means the playlist is gone — silently pulling
    a same-titled other playlist's art would be the very bug this fixes."""
    server = _PosterServer(
        [_PosterPlaylist("Road", "audio", "/library/metadata/11/composite/1", rating_key=11)],
        content=_PNG,
    )
    _patch_poster(monkeypatch, server)
    assert playlists_pull.download_poster(CONFIG, "999", "Road") is None


def test_download_poster_without_key_or_title_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _PosterServer(
        [_PosterPlaylist("Road", "audio", "/library/metadata/11/composite/1", rating_key=11)]
    )
    _patch_poster(monkeypatch, server)
    assert playlists_pull.download_poster(CONFIG, None, None) is None
