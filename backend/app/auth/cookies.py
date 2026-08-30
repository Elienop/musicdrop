"""Whether the session cookie a request earns may be marked ``Secure``.

The flag is CONDITIONAL, decided per request, not a constant. MusicDrop's
primary deployment is plain HTTP by LAN IP (``http://192.168.1.10:3030``),
where a ``Secure`` cookie is never sent back and the app could not be signed
into at all — so a constant ``True`` is not available. But a constant ``False``
throws the protection away on the deployment that HAS TLS, which is the one
where it matters. Reading the scheme gives both: the by-IP path behaves exactly
as it did before, and a browser reaching MusicDrop through a TLS reverse proxy
gets a cookie its own browser will refuse to put on a plain-HTTP hop.

**Why the header is read here rather than configured into uvicorn.** uvicorn's
proxy-header middleware is on by default, but it only trusts ``127.0.0.1``
(``forwarded_allow_ips``), and a reverse proxy in a sibling container connects
from a bridge address — so ``request.url.scheme`` is ``"http"`` behind Caddy
today. Widening that trust is per-deployment configuration this image cannot
ship, and it would widen more than the scheme: the same middleware rewrites
``X-Forwarded-For``, which is the client identity every rate limit and log line
is keyed on. Reading one header for one decision is the smaller change.

**Trusting X-Forwarded-Proto is safe HERE, and that is a claim about the
failure mode rather than about the header.** A direct client that sends
``X-Forwarded-Proto: https`` over plain HTTP mints itself a ``Secure`` cookie
that its own browser will then refuse to send back over that same plain
connection: a self-inflicted lockout, costing nobody else anything. No session
but the forger's is weakened, and absence of the header is exactly the old
behaviour. There is no reading of this header that downgrades a third party.

Contrast ``app/host_guard.py``, which VALIDATES ``X-Forwarded-Host`` against an
allowlist instead of trusting it. The difference is not the header, it is the
question being asked of it: that value feeds the authority comparison the
origin guard makes a security DECISION on, so a forged one buys a real bypass
and the guard has to fail closed. This value feeds a hardening flag whose
forged form harms only the forger. Different rules for different questions.
"""

from __future__ import annotations

from typing import Final

from starlette.requests import Request

#: Set by a reverse proxy to record the scheme of the ORIGINAL client hop.
_FORWARDED_PROTO_HEADER: Final = "x-forwarded-proto"
_HTTPS_SCHEME: Final = "https"


def request_is_https(request: Request) -> bool:
    """Whether the browser's own hop to MusicDrop was encrypted.

    True for direct TLS (or a proxy uvicorn already trusted, which rewrites the
    scheme before any of this runs), and for an ``X-Forwarded-Proto`` naming
    https. Anything else — including the header being absent — is false, so a
    deployment that adds no proxy sees no change at all.
    """
    if request.url.scheme == _HTTPS_SCHEME:
        return True
    forwarded = request.headers.get(_FORWARDED_PROTO_HEADER)
    if forwarded is None:
        return False
    # The FIRST element, never a substring search. Proxies APPEND, so a chain
    # writes ``https, http`` — browser over TLS, plain hop onward — and the
    # leftmost value is the one describing the browser's own connection. A
    # substring test would read the reverse chain, ``http, https`` (a plain
    # client whose proxy talks TLS onward), as an encrypted browser hop and
    # mint a cookie that browser can never send.
    return forwarded.split(",")[0].strip().lower() == _HTTPS_SCHEME
