"""ASGI middleware that rejects requests whose Host is not allowlisted (the DNS-rebinding guard).

The #153 origin guard anchors on ``Origin`` authority == ``Host`` — a check
that holds trivially when the ATTACKER chose the hostname: rebind a DNS name
to this server's LAN IP and the victim browser's same-origin fetch carries
matching Origin+Host. Rebinding needs a DNS *name* — an IP literal in the
address bar cannot be rebound — so the policy here is: bare IP literals and
``localhost`` always pass, configured DNS names (``MUSICDROP_ALLOWED_HOSTS``)
pass, everything else is 400. Applies to ALL HTTP methods: rebinding's payoff
is reading the library as much as writing to it.

``X-Forwarded-Host``, when present, is held to the same policy — it feeds the
origin guard's authority comparison, so it cannot stay untrusted. (Browsers
cannot send it CORS-simply; validating it closes the coupling rather than
arguing about reachability.)

Added in ``app.main`` so it wraps outside every guard (Starlette applies
middleware in reverse add order; only the security-headers stamper wraps it):
a wrong-host request is refused before the body limit, CORS, or the origin
guard spend anything on it. Dev/prod posture keys on
``settings.static_dir`` exactly like ``resolve_extra_origins``; dev
additionally allows Starlette's TestClient default host (``testserver``),
production rejects it (pinned by the prod-posture subprocess test).

Spec: docs/superpowers/specs/2026-08-23-host-guard-design.md.
"""

from __future__ import annotations

import ipaddress

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

# Starlette TestClient's default Host header. Dev posture only.
_TESTCLIENT_HOST = "testserver"


def _authority_host(value: str) -> str | None:
    """The lowercased hostname of a Host-shaped value, or None if unparseable.

    Strips an optional numeric port; handles the bracketed IPv6 form. A
    non-numeric or dangling port fails the parse (fail closed) — no browser
    produces one, so that arm is forged traffic anyway.
    """
    value = value.strip().lower()
    if not value:
        return None
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None
        rest = value[end + 1 :]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return None
        return value[1:end]
    host, sep, port = value.rpartition(":")
    if not sep:
        return value
    if ":" in host:
        # Two-plus colons without brackets: an unbracketed IPv6 literal (not
        # valid in a Host header, but ``ipaddress`` should still get to judge
        # the whole value — it is an IP either way, and an IP cannot rebind).
        return value
    if not port.isdigit():
        return None
    return host


def host_allowed(value: str | None, *, allowed: tuple[str, ...]) -> bool:
    """Whether one Host/X-Forwarded-Host header value may reach the app.

    Bare IP literals always pass (DNS rebinding needs a DNS name — this also
    keeps the Docker healthcheck's ``Host: 127.0.0.1:3030`` and by-IP browsing
    working with zero config), as does ``localhost``; any other name must be
    an exact, case-insensitive, port-less match against ``allowed``. Missing
    or unparseable values are rejected.
    """
    if value is None:
        return False
    host = _authority_host(value)
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return True
    if host == "localhost":
        return True
    return host in allowed


def resolve_allowed_hosts(static_dir: str, allowed_hosts: str) -> tuple[str, ...]:
    """The DNS names allowed in Host, resolved once at app build.

    ``allowed_hosts`` is the comma-separated ``MUSICDROP_ALLOWED_HOSTS``
    setting (whitespace stripped, empties dropped, lowercased). Dev posture
    (``static_dir`` empty) appends the TestClient host so the suite can run;
    production single-image mode does not — secure by default, an owner
    decision, 2026-08-23 (see the spec).
    """
    names = tuple(part.strip().lower() for part in allowed_hosts.split(",") if part.strip())
    return names if static_dir else (*names, _TESTCLIENT_HOST)


class HostGuardMiddleware:
    def __init__(self, app: ASGIApp, *, allowed_hosts: tuple[str, ...]) -> None:
        self._app = app
        self._allowed = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            if not host_allowed(headers.get("host"), allowed=self._allowed):
                await _reject(send)
                return
            forwarded = headers.get("x-forwarded-host")
            if forwarded is not None and not host_allowed(forwarded, allowed=self._allowed):
                await _reject(send)
                return
        await self._app(scope, receive, send)


async def _reject(send: Send) -> None:
    # 400 is TrustedHostMiddleware's convention for an invalid Host; the body
    # shape matches every other JSON error this app emits.
    await send(
        {
            "type": "http.response.start",
            "status": 400,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": b'{"detail":"invalid host"}',
        }
    )
