import pytest

from app.plex import sync
from app.plex.config import PlexConfig
from app.plex.errors import PlexConnectionError, PlexNotConfigured
from app.plex.mapping import PlexTrackSpec


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


class _FakePlaylist:
    def __init__(self, title: str, items: list[_FakeTrack]) -> None:
        self.title = title
        self.ratingKey = 500
        self._items = list(items)
        self.deleted = False

    def items(self) -> list[_FakeTrack]:
        return list(self._items)

    def addItems(self, tracks: list[_FakeTrack]) -> None:
        self._items.extend(tracks)

    def removeItems(self, tracks: list[_FakeTrack]) -> None:
        self._items = [t for t in self._items if t not in tracks]

    def delete(self) -> None:
        self._items = []
        self.deleted = True


class _FakeServer:
    def __init__(self, tracks: list[_FakeTrack]) -> None:
        section = _FakeSection(tracks)
        self.library = type("L", (), {"sections": lambda _self: [section]})()
        self._section = section
        self.created: list[_FakePlaylist] = []
        self._playlists: list[_FakePlaylist] = []

    def playlists(self) -> list[_FakePlaylist]:
        return self._playlists

    def createPlaylist(self, title: str, items: list[_FakeTrack]) -> _FakePlaylist:
        pl = _FakePlaylist(title, items)
        self.created.append(pl)
        self._playlists.append(pl)
        return pl


CONFIG = PlexConfig(base_url="http://plex:32400", token="t")


def _patch(monkeypatch: pytest.MonkeyPatch, server: object) -> None:
    monkeypatch.setattr(sync.client, "connect", lambda base_url, token: server)


def _p(path: str) -> PlexTrackSpec:
    """A path-only spec (no metadata) — exercises the exact-path branch."""
    return PlexTrackSpec(path=path, albumartist="", album="", title="", track=None)


def test_creates_playlist_with_matched_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakeServer([_FakeTrack(10, ["/m/a.flac"]), _FakeTrack(20, ["/m/b.flac"])])
    _patch(monkeypatch, server)
    state = sync.sync_playlist(CONFIG, "Mix", [_p("/m/a.flac"), _p("/m/b.flac")])
    assert state.status == "ok"
    assert state.missing == 0
    assert state.rating_key == "500"
    assert [t.ratingKey for t in server.created[0].items()] == [10, 20]


def test_partial_when_some_paths_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakeServer([_FakeTrack(10, ["/m/a.flac"])])
    _patch(monkeypatch, server)
    state = sync.sync_playlist(CONFIG, "Mix", [_p("/m/a.flac"), _p("/m/gone.flac")])
    assert state.status == "partial"
    assert state.missing == 1


def test_reconciles_existing_playlist(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakeServer([_FakeTrack(10, ["/m/a.flac"]), _FakeTrack(20, ["/m/b.flac"])])
    existing = _FakePlaylist("Mix", [_FakeTrack(99, ["/m/old.flac"])])
    server._playlists.append(existing)
    _patch(monkeypatch, server)
    state = sync.sync_playlist(CONFIG, "Mix", [_p("/m/b.flac")])
    # An existing playlist of this title is DELETED then recreated fresh (so we
    # never rely on Plex's emptied-playlist behaviour).
    assert existing.items() == []  # deleted
    assert len(server.created) == 1
    assert [t.ratingKey for t in server.created[0].items()] == [20]
    assert state.rating_key == "500"


def test_empty_when_no_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakeServer([])
    _patch(monkeypatch, server)
    state = sync.sync_playlist(CONFIG, "Mix", [_p("/m/gone.flac")])
    assert state.status == "empty"
    assert state.rating_key is None


def test_not_configured() -> None:
    with pytest.raises(PlexNotConfigured):
        sync.sync_playlist(PlexConfig(), "Mix", [_p("/m/a.flac")])


def test_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from requests.exceptions import ConnectionError as ReqConnErr

    def boom(base_url: str, token: str) -> object:
        raise ReqConnErr("no route")

    monkeypatch.setattr(sync.client, "connect", boom)
    with pytest.raises(PlexConnectionError):
        sync.sync_playlist(CONFIG, "Mix", [_p("/m/a.flac")])


def test_unexpected_error_translated(monkeypatch: pytest.MonkeyPatch) -> None:
    # Any non-plexapi/non-requests failure is still surfaced as PlexConnectionError.
    def boom(base_url: str, token: str) -> object:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(sync.client, "connect", boom)
    with pytest.raises(PlexConnectionError):
        sync.sync_playlist(CONFIG, "Mix", [_p("/m/a.flac")])


def test_fan_out_to_admin_and_users(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakeServer([_FakeTrack(10, ["/m/a.flac"])])

    user_servers: dict[str, _FakeServer] = {
        "7": _FakeServer([_FakeTrack(10, ["/m/a.flac"])]),
        "8": _FakeServer([_FakeTrack(10, ["/m/a.flac"])]),
    }

    def _switch(uid: str) -> _FakeServer:
        return user_servers[uid]

    server.switchUser = _switch  # type: ignore[attr-defined]
    _patch(monkeypatch, server)

    states = sync.sync_playlist_to_targets(CONFIG, "Mix", [_p("/m/a.flac")], ["7", "8"])
    assert set(states) == {"admin", "7", "8"}
    assert states["admin"].status == "ok"
    assert states["7"].status == "ok"
    # each user server actually got a playlist created
    assert len(user_servers["7"].created) == 1


def test_fan_out_isolates_a_failing_user(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakeServer([_FakeTrack(10, ["/m/a.flac"])])

    def _switch(uid: str) -> _FakeServer:
        if uid == "bad":
            raise RuntimeError("no access")
        return _FakeServer([_FakeTrack(10, ["/m/a.flac"])])

    server.switchUser = _switch  # type: ignore[attr-defined]
    _patch(monkeypatch, server)

    states = sync.sync_playlist_to_targets(CONFIG, "Mix", [_p("/m/a.flac")], ["bad", "ok"])
    assert states["admin"].status == "ok"
    assert states["bad"].status == "failed"
    assert states["bad"].error
    assert states["ok"].status == "ok"


def test_fan_out_not_configured() -> None:
    with pytest.raises(PlexNotConfigured):
        sync.sync_playlist_to_targets(PlexConfig(), "Mix", [_p("/m/a.flac")], ["7"])


def test_sync_metadata_fallback_populates(monkeypatch: pytest.MonkeyPatch) -> None:
    # Plex has the song at a different filename; only metadata bridges it.
    track = _FakeTrack(
        42,
        ["/plex/Adele_19_01_Daydreamer.flac"],
        grandparentTitle="Adele",
        parentTitle="19",
        title="Daydreamer",
        index=1,
    )
    server = _FakeServer([track])
    _patch(monkeypatch, server)
    spec = PlexTrackSpec(
        path="/beets/Adele/19/01 Daydreamer.flac",
        albumartist="Adele",
        album="19",
        title="Daydreamer",
        track=1,
    )
    state = sync.sync_playlist(CONFIG, "Mix", [spec])
    assert state.status == "ok"
    assert state.missing == 0
    assert [t.ratingKey for t in server.created[0].items()] == [42]


def test_delete_on_targets_admin_and_user(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_pl = _FakePlaylist("Mix", [_FakeTrack(1, ["/m/a.flac"])])
    server = _FakeServer([])
    server._playlists.append(admin_pl)

    user7 = _FakeServer([])
    user7_pl = _FakePlaylist("Mix", [_FakeTrack(2, ["/m/b.flac"])])
    user7._playlists.append(user7_pl)
    server.switchUser = lambda uid: user7  # type: ignore[attr-defined]
    _patch(monkeypatch, server)

    results = sync.delete_playlist_on_targets(CONFIG, "Mix", ["admin", "7"])
    assert results == {"admin": "deleted", "7": "deleted"}
    assert admin_pl.deleted is True
    assert user7_pl.deleted is True


def test_delete_on_targets_absent_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakeServer([])  # no playlists of that title
    _patch(monkeypatch, server)
    results = sync.delete_playlist_on_targets(CONFIG, "Mix", ["admin"])
    assert results == {"admin": "absent"}


def test_delete_on_targets_isolates_a_failing_account(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_pl = _FakePlaylist("Mix", [_FakeTrack(1, ["/m/a.flac"])])
    server = _FakeServer([])
    server._playlists.append(admin_pl)

    def _switch(uid: str) -> _FakeServer:
        raise RuntimeError("no access to this user")

    server.switchUser = _switch  # type: ignore[attr-defined]
    _patch(monkeypatch, server)

    results = sync.delete_playlist_on_targets(CONFIG, "Mix", ["admin", "bad"])
    assert results["admin"] == "deleted"  # the failing user never aborts the others
    assert results["bad"] == "failed"
    assert admin_pl.deleted is True


def test_delete_on_targets_not_configured() -> None:
    with pytest.raises(PlexNotConfigured):
        sync.delete_playlist_on_targets(PlexConfig(), "Mix", ["admin"])
