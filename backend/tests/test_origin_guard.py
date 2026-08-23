"""The app-wide origin guard: policy, middleware, and real-app wiring.

Spec: docs/superpowers/specs/2026-08-23-origin-guard-design.md. The policy is
a browser-CSRF guard, not auth: a missing Origin (curl, the container
healthcheck, the slskd webhook) is allowed by design.
"""

from __future__ import annotations

from app.api.csrf import origin_allowed

_EXTRA = ("http://localhost:5173",)


def test_missing_origin_is_allowed() -> None:
    # Non-browser clients (curl, LAN tooling, webhooks) send no Origin.
    assert origin_allowed(None, host="x", forwarded_host=None, extra_origins=())


def test_origin_matching_host_is_allowed() -> None:
    assert origin_allowed("http://nas:3030", host="nas:3030", forwarded_host=None, extra_origins=())


def test_scheme_is_stripped_before_the_host_compare() -> None:
    # Today's semantics (csrf.py splits the scheme off): an https Origin
    # matches an http-hosted authority.
    assert origin_allowed(
        "https://nas:3030", host="nas:3030", forwarded_host=None, extra_origins=()
    )


def test_origin_matching_forwarded_host_is_allowed() -> None:
    # A Host-rewriting reverse proxy: public host arrives in X-Forwarded-Host.
    assert origin_allowed(
        "https://music.example",
        host="127.0.0.1:3030",
        forwarded_host="music.example",
        extra_origins=(),
    )


def test_extra_origin_is_allowed() -> None:
    assert origin_allowed(
        "http://localhost:5173", host="nas:3030", forwarded_host=None, extra_origins=_EXTRA
    )


def test_foreign_origin_is_rejected() -> None:
    assert not origin_allowed(
        "http://evil.test", host="nas:3030", forwarded_host=None, extra_origins=_EXTRA
    )


def test_null_origin_is_rejected() -> None:
    # Sandboxed iframes send the literal string "null".
    assert not origin_allowed("null", host="nas:3030", forwarded_host=None, extra_origins=_EXTRA)


def test_extra_origin_not_in_tuple_is_rejected() -> None:
    # The prod posture: empty tuple, dev origin rejected.
    assert not origin_allowed(
        "http://localhost:5173", host="nas:3030", forwarded_host=None, extra_origins=()
    )
