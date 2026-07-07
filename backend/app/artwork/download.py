"""Shared HTTP image download + validation for the artist-image sources.

GET a URL (following redirects), reject oversize/non-image responses, return the
bytes + content-type. Every source (Deezer, Spotify, fanart.tv) downloads its
chosen image the same way, so the validation lives here once. A source may pass
``reject_url_substrings`` to treat a known "no image" placeholder — e.g. Deezer
302-redirects a no-photo artist to its blank-avatar URL — as a confirmed
no-match (``None``) rather than a transient failure.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi.concurrency import run_in_threadpool

from app.artwork.images import MAX_IMAGE_BYTES
from app.artwork.source import ResolvedImage, TransientSourceError

# One message for EVERY blocked-host / connection failure. Collapsing them is
# deliberate: distinct "connection refused" vs "no route" vs "blocked host"
# text would let a LAN caller use this endpoint as a port-scanning / service
# fingerprinting oracle. The size-cap message stays distinct (it leaks nothing).
_GENERIC_FETCH_ERROR = "could not fetch an image from that link"

# Only the initial request + this many redirect hops are followed, each
# re-validated, before giving up.
_MAX_REDIRECT_HOPS = 3
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})


def assert_public_url(url: str) -> None:
    """Raise ``ValueError`` if ``url``'s host resolves to a non-public address.

    SSRF guard for the user-supplied set-image-from-URL path: an unrestricted
    server-side fetch of a pasted URL can be aimed at loopback, the cloud
    metadata endpoint (169.254.169.254), or LAN hosts, turning the server into a
    fetch-proxy into internal services. Every returned address must be public;
    if resolution fails or ANY address is disallowed we raise the single generic
    message (no oracle). This resolves-then-validates — it does not pin the IP
    for the subsequent connect, so it does not defend against a deliberate
    DNS-rebind race; that is out of scope for this single-user LAN deployment,
    and the fetch path re-validates every redirect hop.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError(_GENERIC_FETCH_ERROR)
    host = parts.hostname
    if not host:
        raise ValueError(_GENERIC_FETCH_ERROR)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(_GENERIC_FETCH_ERROR) from exc
    if port is None:
        port = 443 if parts.scheme.lower() == "https" else 80
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(_GENERIC_FETCH_ERROR) from exc
    if not infos:
        raise ValueError(_GENERIC_FETCH_ERROR)
    for info in infos:
        sockaddr = info[4]
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError as exc:
            # e.g. a scoped IPv6 literal (fe80::1%eth0) — a link-local address we
            # would reject anyway; refuse rather than trust an unparseable host.
            raise ValueError(_GENERIC_FETCH_ERROR) from exc
        if (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_private
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(_GENERIC_FETCH_ERROR)


async def download_image(
    client: httpx.AsyncClient,
    url: str,
    *,
    reject_url_substrings: tuple[str, ...] = (),
) -> ResolvedImage | None:
    try:
        # Defense-in-depth: these are trusted public CDNs, but validate the
        # initial URL and the final URL after the redirect chain so a compromised
        # or misconfigured source can't redirect us into an internal address.
        await run_in_threadpool(assert_public_url, url)
        response = await client.get(url, follow_redirects=True)
        response.raise_for_status()
        await run_in_threadpool(assert_public_url, str(response.url))
    except ValueError as exc:
        raise TransientSourceError(f"image download blocked: {exc}") from exc
    except httpx.HTTPError as exc:
        raise TransientSourceError(f"image download failed: {exc}") from exc

    # A known "no image" placeholder is a confirmed no-match, not a failure: e.g.
    # Deezer 302-redirects a no-photo artist to its blank-avatar URL (hash = MD5
    # of the empty string), so the final URL — not the original — carries the tell.
    final_url = str(response.url)
    if any(token in final_url for token in reject_url_substrings):
        return None

    declared = response.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
        raise TransientSourceError("image exceeds size limit (Content-Length)")

    content_type = response.headers.get("content-type", "")
    if not content_type.lower().startswith("image/"):
        raise TransientSourceError(f"download was not an image: {content_type!r}")

    data = response.content
    if len(data) > MAX_IMAGE_BYTES:
        raise TransientSourceError("image exceeds size limit")

    return ResolvedImage(data=data, content_type=content_type)


async def fetch_image_bytes(client: httpx.AsyncClient, url: str) -> bytes:
    """Fetch a USER-supplied image URL as raw bytes (follows redirects, size-capped).

    Distinct from ``download_image`` (trusted source CDNs, validated by Content-Type
    header): a pasted URL may carry a wrong/missing content-type, so this returns raw
    bytes and the caller validates them with ``sniff_image_mime`` (magic bytes), exactly
    how the upload endpoint validates an uploaded file. Raises ``ValueError`` on a
    non-fetchable or oversize response.

    SSRF-hardened: every URL — the initial one and each redirect target — is run
    through :func:`assert_public_url` before it is fetched, and redirects are
    followed manually (``follow_redirects=False``) so a public URL that 302s to an
    internal address is blocked at the hop, not after the request lands.
    """
    current = url
    for _ in range(_MAX_REDIRECT_HOPS + 1):
        await run_in_threadpool(assert_public_url, current)
        try:
            response = await client.get(current, follow_redirects=False, timeout=10.0)
        except httpx.HTTPError as exc:
            raise ValueError(_GENERIC_FETCH_ERROR) from exc
        if response.status_code in _REDIRECT_STATUS:
            location = response.headers.get("location")
            if not location:
                raise ValueError(_GENERIC_FETCH_ERROR)
            current = urljoin(current, location)
            continue
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ValueError(_GENERIC_FETCH_ERROR) from exc
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
            raise ValueError("image is too large (max 10 MB)")
        data = response.content
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("image is too large (max 10 MB)")
        return data
    # More than _MAX_REDIRECT_HOPS redirects: treat as unreachable, don't leak.
    raise ValueError(_GENERIC_FETCH_ERROR)
