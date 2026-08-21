import pytest

from app.plex import service
from app.plex.config import PlexConfig
from app.plex.errors import PlexConnectionError, PlexNotConfigured
from tests.plex_fakes import FakeSection, FakeServer


class _FakeUser:
    def __init__(self, uid: int, title: str, home: bool) -> None:
        self.id = uid
        self.title = title
        self.username = title
        self.email = f"{title}@example.com"
        self.home = home


class _FakeAccount:
    def __init__(self, users: list[_FakeUser]) -> None:
        self._users = users

    def users(self) -> list[_FakeUser]:
        return self._users


class _FakeServer:
    friendlyName = "Living Room"

    def __init__(self, users: list[_FakeUser]) -> None:
        self._users = users

    def myPlexAccount(self) -> _FakeAccount:
        return _FakeAccount(self._users)


CONFIG = PlexConfig(base_url="http://plex:32400", token="tok")


def _patch_connect(monkeypatch: pytest.MonkeyPatch, server: object) -> None:
    monkeypatch.setattr(service.client, "connect", lambda base_url, token: server)


def test_test_connection_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_connect(monkeypatch, _FakeServer([]))
    result = service.test_connection(CONFIG)
    assert result.ok is True
    assert result.server_name == "Living Room"


def test_test_connection_not_configured() -> None:
    result = service.test_connection(PlexConfig())
    assert result.ok is False
    assert result.error


def test_test_connection_reports_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from plexapi.exceptions import Unauthorized

    def boom(base_url: str, token: str) -> object:
        raise Unauthorized("bad token")

    monkeypatch.setattr(service.client, "connect", boom)
    result = service.test_connection(CONFIG)
    assert result.ok is False
    assert result.error


def test_discover_users(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakeServer([_FakeUser(1, "alice", True), _FakeUser(2, "bob", False)])
    _patch_connect(monkeypatch, server)
    users = service.discover_users(CONFIG)
    assert [u.name for u in users] == ["alice", "bob"]
    assert users[0].id == "1"
    assert users[0].home is True


def test_discover_users_not_configured() -> None:
    with pytest.raises(PlexNotConfigured):
        service.discover_users(PlexConfig())


def test_discover_users_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from requests.exceptions import ConnectionError as ReqConnectionError

    def boom(base_url: str, token: str) -> object:
        raise ReqConnectionError("no route")

    monkeypatch.setattr(service.client, "connect", boom)
    with pytest.raises(PlexConnectionError):
        service.discover_users(CONFIG)


def test_list_music_sections_returns_artist_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Section:
        def __init__(self, type_: str, title: str) -> None:
            self.TYPE = type_
            self.title = title
            self.locations = [f"/data/{title.lower()}"]

    class _Server:
        library = type(
            "L",
            (),
            {
                "sections": lambda _self: [
                    _Section("artist", "Music"),
                    _Section("movie", "Films"),
                    _Section("artist", "MusicDrop"),
                ]
            },
        )()

    monkeypatch.setattr(service.client, "connect", lambda url, token: _Server())
    sections = service.list_music_sections(PlexConfig(base_url="http://p", token="t"))
    assert [s.title for s in sections] == ["Music", "MusicDrop"]


def test_list_music_sections_reports_the_folders_plex_holds_for_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The folders are the whole point: a section's `locations` is the ONLY place
    # the true Plex-side library path can be read from, and dropping it is what
    # left the user typing `/music` for a library that lives at `/musicdrop`.
    # A Plex library may hold several folders, so the wire carries a list.
    server = FakeServer(
        [],
        sections=[
            FakeSection([], title="Music", locations=["/data/music"]),
            FakeSection([], title="MusicDrop", locations=["/musicdrop", "/mnt/spill"]),
        ],
    )
    monkeypatch.setattr(service.client, "connect", lambda url, token: server)
    sections = service.list_music_sections(PlexConfig(base_url="http://p", token="t"))
    assert [(s.title, s.locations) for s in sections] == [
        ("Music", ["/data/music"]),
        ("MusicDrop", ["/musicdrop", "/mnt/spill"]),
    ]


def test_list_music_sections_survives_a_section_that_reports_no_folders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every real library has at least one folder, but the attribute is read off
    # whatever the server sent; a section listed without one must degrade to "no
    # folders known" rather than break the whole dropdown.
    class _Bare:
        TYPE = "artist"
        title = "Music"

    class _Server:
        library = type("L", (), {"sections": lambda _self: [_Bare()]})()

    monkeypatch.setattr(service.client, "connect", lambda url, token: _Server())
    sections = service.list_music_sections(PlexConfig(base_url="http://p", token="t"))
    assert [(s.title, s.locations) for s in sections] == [("Music", [])]


def test_list_music_sections_requires_config() -> None:
    with pytest.raises(PlexNotConfigured):
        service.list_music_sections(PlexConfig())
