"""SSRF guard for the user-supplied set-image-from-URL path.

``fetch_image_bytes`` fetches a pasted URL, so an attacker (or a maintainer
tricked into pasting a crafted link) could aim it at loopback, the cloud
metadata endpoint, or a LAN host — turning the server into a fetch-proxy into
internal services. These tests pin the guard: private/loopback/link-local
targets are refused, a public URL that redirects to an internal one is refused,
and every failure surfaces the SAME generic message so it can't be used as a
port-scanning oracle. DNS is patched (never real network) and respx mocks the
HTTP layer.
"""

import socket
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import respx

from app.artwork.download import fetch_image_bytes
from app.artwork.images import MAX_IMAGE_BYTES

_GENERIC = "could not fetch an image from that link"


def _addrinfo(ip: str, port: int) -> list[Any]:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr: Any = (ip, port) if family == socket.AF_INET else (ip, port, 0, 0)
    return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]


def _resolver(mapping: dict[str, str], default: str | None = None) -> Callable[..., list[Any]]:
    """Return a getaddrinfo stub resolving host->ip per ``mapping`` (else ``default``)."""

    def fake(host: str, port: int, *args: object, **kwargs: object) -> list[Any]:
        ip = mapping.get(host, default)
        if ip is None:
            raise socket.gaierror(f"cannot resolve {host}")
        return _addrinfo(ip, port)

    return fake


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


@pytest.mark.anyio
@pytest.mark.parametrize("blocked_ip", ["127.0.0.1", "169.254.169.254", "10.0.0.5"])
@respx.mock
async def test_blocked_host_raises_generic(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, blocked_ip: str
) -> None:
    # The URL, if fetched, would return valid image bytes — so a passing test
    # proves the guard blocked BEFORE the fetch, not that the fetch happened to
    # fail.
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({"evil.test": blocked_ip}))
    respx.get("https://evil.test/x.jpg").mock(
        return_value=httpx.Response(200, content=b"IMG", headers={"content-type": "image/jpeg"})
    )
    with pytest.raises(ValueError, match=_GENERIC):
        await fetch_image_bytes(client, "https://evil.test/x.jpg")


@pytest.mark.anyio
@respx.mock
async def test_redirect_to_internal_is_blocked(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Public host that 302s to the cloud-metadata IP: the per-hop re-validation
    # must catch the redirected-to internal address.
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _resolver({"public.test": "93.184.216.34", "metadata.test": "169.254.169.254"}),
    )
    respx.get("https://public.test/a.jpg").mock(
        return_value=httpx.Response(302, headers={"location": "http://metadata.test/latest/meta"})
    )
    respx.get("http://metadata.test/latest/meta").mock(
        return_value=httpx.Response(200, content=b"SECRET", headers={"content-type": "image/jpeg"})
    )
    with pytest.raises(ValueError, match=_GENERIC):
        await fetch_image_bytes(client, "https://public.test/a.jpg")


@pytest.mark.anyio
@respx.mock
async def test_public_host_returns_bytes(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({"cdn.test": "93.184.216.34"}))
    respx.get("https://cdn.test/a.jpg").mock(
        return_value=httpx.Response(
            200, content=b"IMGBYTES", headers={"content-type": "image/jpeg"}
        )
    )
    assert await fetch_image_bytes(client, "https://cdn.test/a.jpg") == b"IMGBYTES"


@pytest.mark.anyio
@respx.mock
async def test_fetch_streams_the_body(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The body is read via client.stream() (capped as it arrives), never the
    # buffer-everything client.get(), so a lying Content-Length can't OOM us.
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({"cdn.test": "93.184.216.34"}))
    respx.get("https://cdn.test/a.jpg").mock(
        return_value=httpx.Response(200, content=b"IMG", headers={"content-type": "image/jpeg"})
    )
    used = {"stream": 0, "get": 0}
    real_stream = client.stream
    real_get = client.get

    def stream_spy(*a: object, **k: object) -> object:
        used["stream"] += 1
        return real_stream(*a, **k)  # type: ignore[arg-type]

    async def get_spy(*a: object, **k: object) -> object:
        used["get"] += 1
        return await real_get(*a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(client, "stream", stream_spy)
    monkeypatch.setattr(client, "get", get_spy)
    assert await fetch_image_bytes(client, "https://cdn.test/a.jpg") == b"IMG"
    assert used["stream"] >= 1
    assert used["get"] == 0


@pytest.mark.anyio
@respx.mock
async def test_public_redirect_is_followed(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A benign public->public redirect must still be followed (not over-blocked).
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _resolver({"cdn.test": "93.184.216.34", "cdn2.test": "93.184.216.35"}),
    )
    respx.get("https://cdn.test/src").mock(
        return_value=httpx.Response(302, headers={"location": "https://cdn2.test/dst"})
    )
    respx.get("https://cdn2.test/dst").mock(
        return_value=httpx.Response(200, content=b"OK", headers={"content-type": "image/jpeg"})
    )
    assert await fetch_image_bytes(client, "https://cdn.test/src") == b"OK"


@pytest.mark.anyio
@respx.mock
async def test_public_host_size_cap_still_enforced(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({"cdn.test": "93.184.216.34"}))
    respx.get("https://cdn.test/big").mock(
        return_value=httpx.Response(
            200, content=b"x", headers={"content-length": str(MAX_IMAGE_BYTES + 1)}
        )
    )
    with pytest.raises(ValueError, match="too large"):
        await fetch_image_bytes(client, "https://cdn.test/big")


@pytest.mark.anyio
@respx.mock
async def test_generic_message_is_same_for_blocked_and_connect_refused(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A blocked host and a genuine connection failure must be indistinguishable
    # from the error message — otherwise it's an open/closed-port oracle.
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _resolver({"blocked.test": "127.0.0.1", "refused.test": "93.184.216.34"}),
    )
    respx.get("https://blocked.test/x").mock(
        return_value=httpx.Response(200, content=b"IMG", headers={"content-type": "image/jpeg"})
    )
    respx.get("https://refused.test/x").mock(side_effect=httpx.ConnectError("Connection refused"))

    with pytest.raises(ValueError) as blocked_exc:
        await fetch_image_bytes(client, "https://blocked.test/x")
    with pytest.raises(ValueError) as refused_exc:
        await fetch_image_bytes(client, "https://refused.test/x")

    assert str(blocked_exc.value) == str(refused_exc.value) == _GENERIC


@pytest.mark.anyio
@respx.mock
async def test_ssrf_check_runs_in_threadpool(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The blocking DNS resolve (assert_public_url -> socket.getaddrinfo) must be
    # offloaded to the threadpool so a slow resolver can't stall the event loop.
    # Spy on the module-level run_in_threadpool (a real passthrough) and confirm
    # the guard is dispatched THROUGH it, not called inline on the loop.
    import app.artwork.download as download

    calls: list[Callable[..., object]] = []

    async def _spy(func: Callable[..., object], *args: object, **kwargs: object) -> object:
        calls.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(download, "run_in_threadpool", _spy, raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({"cdn.test": "93.184.216.34"}))
    respx.get("https://cdn.test/a.jpg").mock(
        return_value=httpx.Response(200, content=b"IMG", headers={"content-type": "image/jpeg"})
    )
    assert await fetch_image_bytes(client, "https://cdn.test/a.jpg") == b"IMG"
    assert download.assert_public_url in calls  # the guard was dispatched via the threadpool


@pytest.mark.anyio
async def test_assert_public_url_rejects_each_disallowed_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.artwork.download import assert_public_url

    disallowed = {
        "loopback.test": "127.0.0.1",
        "linklocal.test": "169.254.169.254",
        "private.test": "10.0.0.5",
        "private2.test": "192.168.1.1",
        "ula6.test": "fc00::1",
        "loopback6.test": "::1",
        "unspecified.test": "0.0.0.0",
        "multicast.test": "224.0.0.1",
    }
    for host, ip in disallowed.items():
        monkeypatch.setattr(socket, "getaddrinfo", _resolver({host: ip}))
        with pytest.raises(ValueError, match=_GENERIC):
            assert_public_url(f"https://{host}/a.jpg")


@pytest.mark.anyio
async def test_assert_public_url_allows_public(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.artwork.download import assert_public_url

    monkeypatch.setattr(socket, "getaddrinfo", _resolver({"cdn.test": "93.184.216.34"}))
    assert_public_url("https://cdn.test/a.jpg")  # no raise


@pytest.mark.anyio
async def test_assert_public_url_rejects_non_http_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.artwork.download import assert_public_url

    monkeypatch.setattr(socket, "getaddrinfo", _resolver({}, default="93.184.216.34"))
    with pytest.raises(ValueError, match=_GENERIC):
        assert_public_url("file:///etc/passwd")


@pytest.mark.anyio
async def test_assert_public_url_rejects_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.artwork.download import assert_public_url

    monkeypatch.setattr(socket, "getaddrinfo", _resolver({}))  # everything fails to resolve
    with pytest.raises(ValueError, match=_GENERIC):
        assert_public_url("https://nope.test/a.jpg")
