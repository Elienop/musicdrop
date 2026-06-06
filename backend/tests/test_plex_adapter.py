import pytest

from app.plex import service
from app.plex.config import PlexConfig
from app.plex.errors import PlexConnectionError, PlexNotConfigured


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
