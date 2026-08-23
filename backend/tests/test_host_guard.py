"""Host-guard policy tests: the DNS-rebinding allowlist predicate.

Wire tests on the real app live further down (Task 3); this section tests the
pure policy in isolation. Exactness tests are load-bearing: mutating the
allowlist compare from ``==``/``in``-tuple to ``endswith``/substring is THE
canonical origin/host-validation bug (see the #153 deep-review lesson) and
survives any suite whose negative fixtures share no substring with an allowed
name — every near-miss below shares one.
"""

from __future__ import annotations

import pytest

from app.host_guard import host_allowed, resolve_allowed_hosts


@pytest.mark.parametrize(
    "value",
    [
        "127.0.0.1",
        "127.0.0.1:3030",
        "192.168.1.5:3030",
        "10.0.0.7",
        "[::1]",
        "[::1]:3030",
        "[2001:db8::1]:8080",
        "::1",  # unbracketed IPv6 is invalid in a Host header but IS an IP literal
    ],
)
def test_ip_literals_always_pass(value: str) -> None:
    assert host_allowed(value, allowed=())


@pytest.mark.parametrize("value", ["localhost", "localhost:3030", "LOCALHOST:3030", "Localhost"])
def test_localhost_always_passes(value: str) -> None:
    assert host_allowed(value, allowed=())


@pytest.mark.parametrize("value", ["nas", "nas:3030", "NAS:3030", "music.example.test:443"])
def test_allowlisted_names_pass_port_and_case_insensitively(value: str) -> None:
    assert host_allowed(value, allowed=("nas", "music.example.test"))


@pytest.mark.parametrize(
    "value",
    [
        "evil-nas",
        "evil-nas:3030",
        "nas.evil.test",
        "nas.evil.test:3030",
        "nas0",
        "sub.nas",
        "nas.",  # trailing-dot FQDN spelling does NOT alias its dotless twin
        "evilnas",
        "anas",
    ],
)
def test_allowlist_compare_is_exact_not_a_suffix_or_substring(value: str) -> None:
    assert not host_allowed(value, allowed=("nas",))


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "evil.test",
        "evil.test:3030",
        "nas:abc",  # non-numeric port
        "nas:",  # empty port
        "[::1",  # unterminated bracket
        "[::1]x",  # junk after the bracket
        "[::1]:abc",  # non-numeric port on the bracket form
    ],
)
def test_everything_else_is_rejected(value: str | None) -> None:
    assert not host_allowed(value, allowed=("nas",))


def test_resolver_dev_posture_appends_testserver() -> None:
    # static_dir empty = dev: Starlette's TestClient default Host is allowed so
    # the suite can run at all. Order/content pinned exactly.
    assert resolve_allowed_hosts("", "") == ("testserver",)
    assert resolve_allowed_hosts("", "nas") == ("nas", "testserver")


def test_resolver_prod_posture_does_not_append_testserver() -> None:
    assert resolve_allowed_hosts("/app/static", "") == ()
    assert resolve_allowed_hosts("/app/static", "nas") == ("nas",)


def test_resolver_parses_the_comma_separated_setting() -> None:
    # Whitespace stripped, empties dropped, lowercased.
    assert resolve_allowed_hosts("/app/static", " NAS , music.Example.test ,, ") == (
        "nas",
        "music.example.test",
    )
