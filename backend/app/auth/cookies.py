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
failure MODES rather than about the header.** A client that sends this header
decides only its own cookie, and it can get that wrong in two directions:

* it claims ``https`` over a plain connection and mints itself a ``Secure``
  cookie its own browser will then refuse to send back over that same plain
  connection — a self-inflicted lockout, immediate and loud;
* it claims ``http`` on a session that really is TLS-fronted and gets a cookie
  with no ``Secure`` flag — a self-DOWNGRADE, and unlike the lockout it is
  SILENT: everything keeps working, the cookie is simply eligible to travel
  over a plain hop. This is reachable whenever the proxy in front of MusicDrop
  PRESERVES or APPENDS a client-supplied value rather than overwriting it.
  Measured against this app: ``https, http`` -> Secure, ``http, https`` -> no
  Secure, and duplicate headers ``http`` then ``https`` -> no Secure, because
  Starlette's ``Headers.get`` is first-header-wins.

Both cost only the sender, which is what decides this: no THIRD party is
downgraded. An attacker cannot put this header on a VICTIM's cross-origin
request — ``X-Forwarded-Proto`` is not CORS-safelisted, so the browser
preflights it, and prod's CORS allowlist is empty (``app/main.py``) while the
origin guard refuses the cross-origin write regardless. The design holds; the
silent mode is named here because a future reader should not have to
rediscover it, and because it is the mode that decides how you want the proxy
in front of MusicDrop to set this header.

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

    True when the ASGI scope's scheme is already https, and otherwise when an
    ``X-Forwarded-Proto`` names it. Anything else — including the header being
    absent — is false, so a deployment that adds no proxy sees no change at all.

    **The scheme branch is not a synonym for "direct TLS".** uvicorn 0.52.1's
    ``ProxyHeadersMiddleware`` is on by default (``Config.proxy_headers=True``),
    trusts ``127.0.0.1``, and assigns ``scope["scheme"]`` from
    ``X-Forwarded-Proto`` UNCONDITIONALLY, including downward — so on a
    hypothetical ``--ssl-certfile`` deployment a localhost client sending
    ``X-Forwarded-Proto: http`` reaches this function with a scheme of
    ``"http"`` (measured). Unreachable in the shipped image, whose ``CMD``
    passes no ``--ssl-*`` flags, and self-inflicted where it is reachable — but
    it is why this reads as "the scope says https", not "TLS was terminated
    here".

    The two layers read the same header differently, and only for a client
    uvicorn trusts: uvicorn takes the LAST header and requires an exact token
    match (so a comma list, or ``HTTPS`` in the wrong case, is ignored
    entirely), while this function takes the FIRST header and its first comma
    element. Every shape where the two disagree resolves toward ``Secure=True``
    — measured across both base schemes; duplicate ``http`` then ``https`` from
    a trusted client is the clearest, minting Secure where this function alone
    would not. The only non-Secure outcome is the one where both layers AGREE
    on ``http``, which is the self-downgrade the module docstring names.
    """
    if request.url.scheme == _HTTPS_SCHEME:
        return True
    forwarded = request.headers.get(_FORWARDED_PROTO_HEADER)
    if forwarded is None:
        return False
    # The FIRST element, never a substring search. By CONVENTION the leftmost
    # value names the original client hop — the same reading X-Forwarded-For
    # has, where each hop appends — so ``https, http`` is a browser over TLS
    # with a plain hop onward, and the leftmost element is the one describing
    # the browser's own connection.
    #
    # That is the convention, not a guarantee: whether the proxy in front of
    # MusicDrop appends, overwrites, or passes a client-supplied value straight
    # through is ITS configuration, which this code neither controls nor can
    # detect. Confirmed against upstream documentation for the two proxies
    # README names (2026-08-30):
    #
    #   Caddy   — ``reverse_proxy`` sets X-Forwarded-Proto, and "by default,
    #             the proxy will ignore their values from incoming requests, to
    #             prevent spoofing". Safe as shipped. The exception is
    #             ``trusted_proxies``: configured ranges ARE trusted to have
    #             sent good X-Forwarded-* values, so a range wide enough to
    #             include ordinary clients re-opens the spoof.
    #   nginx   — adds no X-Forwarded-* at all by default (its only default
    #             ``proxy_set_header`` directives are Host and Connection), so
    #             it needs ``proxy_set_header X-Forwarded-Proto $scheme;``.
    #             Written with ``$scheme`` it is the connection's own value;
    #             written to pass the client's header through, it is not.
    #
    # So the header is trustworthy under both defaults and untrustworthy under
    # specific misconfigurations — which is why this stays a documented
    # residual rather than a guarantee. The module docstring's silent
    # self-downgrade is what a pass-through costs.
    #
    # A substring test would be worse under every reading of the chain: it
    # takes ``http, https`` (a plain client hop, TLS onward) for an encrypted
    # browser hop and mints a cookie that browser can never send.
    return forwarded.split(",")[0].strip().lower() == _HTTPS_SCHEME
