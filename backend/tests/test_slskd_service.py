import httpx
import pytest

from app.models.slskd import SlskdConnection
from app.slskd import service
from app.slskd.config import SlskdConfig


def _configured() -> SlskdConfig:
    return SlskdConfig(base_url="http://slskd:5030", token="key")


def test_test_connection_unconfigured() -> None:
    result = service.test_connection(SlskdConfig())
    assert result.ok is False
    assert result.version is None
    assert result.error == "slskd is not configured."


def test_test_connection_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.client, "check", lambda base_url, token: "0.22.3")
    result = service.test_connection(_configured())
    assert result == SlskdConnection(ok=True, version="0.22.3", error=None)


def test_test_connection_rejects_bad_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(base_url: str, token: str) -> str:
        request = httpx.Request("GET", "http://slskd:5030/api/v0/application/version")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    monkeypatch.setattr(service.client, "check", boom)
    result = service.test_connection(_configured())
    assert result.ok is False
    assert result.error == "slskd rejected the API key."


def test_test_connection_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(base_url: str, token: str) -> str:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(service.client, "check", boom)
    result = service.test_connection(_configured())
    assert result.ok is False
    assert result.error == "Couldn't reach the slskd server."


def test_check_calls_version_endpoint_with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # The SOLE network seam: GET {base_url}/api/v0/application/version with the
    # X-API-Key header. Stub httpx.get so no socket is opened.
    from app.slskd import client

    seen: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> str:
            return "0.22.3"

    def fake_get(url: str, *, headers: dict[str, str], timeout: float) -> _Resp:
        seen["url"] = url
        seen["headers"] = headers
        return _Resp()

    monkeypatch.setattr(httpx, "get", fake_get)
    version = client.check("http://slskd:5030", "secret-key")
    assert version == "0.22.3"
    assert seen["url"] == "http://slskd:5030/api/v0/application/version"
    assert seen["headers"] == {"X-API-Key": "secret-key"}
