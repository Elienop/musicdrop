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


@pytest.mark.anyio
async def test_assert_public_url_rejects_every_normal_integration_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The anti-hardening pin: this guard must NEVER be reused on ``base_url``.

    ``PlexConfig.base_url`` and ``SlskdConfig.base_url`` are the mirror image of
    the pasted-image URL this module guards. A pasted image URL is expected to
    be PUBLIC, so refusing private space costs a legitimate user nothing; a
    ``base_url`` is expected to be PRIVATE — a LAN address, a compose service
    name, or the app's own host — so the same guard would reject EVERY CORRECT
    CONFIGURATION. That alone disqualifies it, whatever else it might block: it
    would in fact also block the link-local metadata target that
    ``SlskdConfig.base_url``'s comment describes, so the argument here is
    "wrong tool", not "no benefit". Both field comments say this in prose and
    cite these exact values; this test is the executable half, so the claim
    cannot quietly become false if the guard's address rules are ever loosened.

    Overlaps ``test_assert_public_url_rejects_each_disallowed_class`` on intent,
    but not entirely on coverage: ``172.16/12`` is exercised nowhere else in the
    suite, and this is the only ``assert_public_url`` case with an explicit port,
    so it is the only one taking the ``parts.port`` branch rather than the
    scheme default.

    The ``resolved`` assertion is load-bearing, not decoration.
    ``assert_public_url`` deliberately raises the SAME generic message for "DNS
    failed" and "address disallowed" (no oracle), so
    ``pytest.raises(match=_GENERIC)`` alone cannot tell them apart — a review
    probe mis-keyed every mapping entry, leaving all four hosts unresolvable,
    and the test still passed. Note it must record a SUCCESSFUL resolution, not
    merely that the stub was called: a first attempt at this fix appended the
    host on entry, which the same mutation sailed straight through. Recording
    the address the stub handed back is what proves each refusal came from the
    guard's address rules.
    """
    from app.artwork.download import assert_public_url

    normal_base_urls = {
        "192.168.1.50": "192.168.1.50",  # Plex on the LAN, the owner's own case
        "plex": "172.18.0.4",  # a compose service name on the bridge network
        "slskd": "172.18.0.5",  # ditto — this repo's own test value
        "localhost": "127.0.0.1",  # same host as the app
    }
    ports = {"slskd": 5030}  # the real shapes: slskd listens on 5030, Plex on 32400
    resolved: list[tuple[str, str]] = []

    def _recording(mapping: dict[str, str]) -> Callable[..., list[Any]]:
        inner = _resolver(mapping)

        def fake(host: str, port: int, *args: object, **kwargs: object) -> list[Any]:
            infos = inner(host, port, *args, **kwargs)  # raises gaierror if unresolvable
            resolved.append((host, str(infos[0][4][0])))
            return infos

        return fake

    for host, ip in normal_base_urls.items():
        monkeypatch.setattr(socket, "getaddrinfo", _recording({host: ip}))
        with pytest.raises(ValueError, match=_GENERIC):
            assert_public_url(f"http://{host}:{ports.get(host, 32400)}")

    # Each host resolved to its private address BEFORE the refusal, so every
    # refusal came from the address rules — not from an unresolvable stub key.
    assert resolved == list(normal_base_urls.items())
