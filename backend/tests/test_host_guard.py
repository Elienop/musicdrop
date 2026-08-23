"""Host-guard policy tests: the DNS-rebinding allowlist predicate.

Wire tests on the real app live further down (Task 3); this section tests the
pure policy in isolation. Exactness tests are load-bearing: mutating the
allowlist compare from ``==``/``in``-tuple to ``endswith``/substring is THE
canonical origin/host-validation bug (see the #153 deep-review lesson) and
survives any suite whose negative fixtures share no substring with an allowed
name — every near-miss below shares one.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.host_guard import HostGuardMiddleware, host_allowed, resolve_allowed_hosts
from app.main import app as real_app


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


# ---------------------------------------------------------------------------
# Wire tests on the REAL app (dev posture: `testserver` is allowlisted).
# ---------------------------------------------------------------------------

_REJECT_BODY = {"detail": "invalid host"}


def _client() -> TestClient:
    # No lifespan: none of these routes need the library, and the guard runs
    # before the router anyway.
    return TestClient(real_app)


def _middleware_kwargs(cls: object) -> dict[str, object]:
    for m in real_app.user_middleware:
        if m.cls is cls:
            return dict(m.kwargs)
    raise AssertionError(f"middleware not registered: {cls!r}")


def test_dev_posture_resolved_tuple_reaches_the_guard() -> None:
    allowed = _middleware_kwargs(HostGuardMiddleware)["allowed_hosts"]
    assert allowed == ("testserver",)


def test_allowed_host_passes() -> None:
    r = _client().get("/api/health")  # Host: testserver
    assert r.status_code == 200


def test_disallowed_host_rejected_on_get_and_post() -> None:
    c = _client()
    get = c.get("/api/health", headers={"Host": "evil.test:3030"})
    post = c.post(
        "/api/config/validate",
        json={"yaml_text": "a: 1"},
        headers={"Host": "evil.test:3030"},
    )
    assert get.status_code == 400
    assert get.json() == _REJECT_BODY
    assert get.headers["content-type"] == "application/json"
    assert post.status_code == 400
    assert post.json() == _REJECT_BODY


def test_disallowed_forwarded_host_rejected_even_with_an_allowed_host() -> None:
    # X-Forwarded-Host feeds the ORIGIN guard's authority comparison, so the
    # host guard holds it to the same policy whenever it is present.
    r = _client().get("/api/health", headers={"X-Forwarded-Host": "evil.test"})
    assert r.status_code == 400
    assert r.json() == _REJECT_BODY


def test_empty_forwarded_host_rejected() -> None:
    # Pins `forwarded is not None` against a truthiness rewrite: a PRESENT but
    # empty X-Forwarded-Host must still be policed (it fails the predicate and
    # 400s), not skipped as falsy. Fail-closed either way today, but the
    # decision is the fail-open-able one — a `if forwarded:` refactor makes an
    # empty header bypass the check for free.
    r = _client().get("/api/health", headers={"X-Forwarded-Host": ""})
    assert r.status_code == 400
    assert r.json() == _REJECT_BODY


def test_allowed_forwarded_host_passes() -> None:
    r = _client().get("/api/health", headers={"X-Forwarded-Host": "127.0.0.1:3030"})
    assert r.status_code == 200


def test_options_from_a_disallowed_host_rejected() -> None:
    # ALL methods — a CORS preflight from a rebound page dies here, before
    # CORSMiddleware would answer it.
    r = _client().options(
        "/api/config/validate",
        headers={
            "Host": "evil.test:3030",
            "Origin": "http://evil.test:3030",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status_code == 400


def test_dns_rebinding_simulation_is_closed() -> None:
    # THE attack this slice exists for, exactly as it beat #153's origin
    # guard: attacker-chosen Host with a MATCHING Origin (the browser's
    # same-origin fetch after rebinding evil.test to the box's LAN IP). The
    # origin guard passes this pair by construction; the host guard must not.
    r = _client().post(
        "/api/config/validate",
        json={"yaml_text": "a: 1"},
        headers={"Host": "evil.test:3030", "Origin": "http://evil.test:3030"},
    )
    assert r.status_code == 400
    assert r.json() == _REJECT_BODY


def test_healthcheck_shape_passes() -> None:
    # The Docker HEALTHCHECK: urllib GET with Host: 127.0.0.1:3030, no Origin.
    r = _client().get("/api/health", headers={"Host": "127.0.0.1:3030"})
    assert r.status_code == 200


def test_disallowed_host_beats_the_body_limit() -> None:
    # Ordering pin: HostGuard wraps OUTERMOST, so a wrong-host oversize body
    # gets the host 400, not the body-limit 413. (The oversize-vs-ORIGIN-guard
    # precedence pin lives in test_body_limit.py and is unchanged.)
    big = b"x" * (25 * 1024 * 1024 + 1)
    r = _client().post(
        "/api/config/validate",
        content=big,
        headers={"Host": "evil.test:3030", "Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json() == _REJECT_BODY


def test_prod_posture_rejects_testserver_and_honors_the_setting(tmp_path: Path) -> None:
    """The prod half of the dev/prod seam, on the REAL app wiring.

    The suite runs in dev posture, where a correctly-resolved allowlist and a
    hardcoded ``("testserver",)`` are identical — no in-process test can tell
    them apart (the #153 lesson). This builds ``app.main`` in a subprocess
    with ``MUSICDROP_STATIC_DIR`` and ``MUSICDROP_ALLOWED_HOSTS`` set, so
    import-time settings resolve to PRODUCTION, and pins:
    - ``testserver`` is REJECTED (the dev extra does not leak into prod);
    - the configured name is allowed (the setting actually reaches the guard);
    - an IP literal is allowed (the carve-out is not posture-keyed);
    - the name-based reverse-proxy WRITE flow works end-to-end: internal IP
      Host + allowlisted ``X-Forwarded-Host`` + matching Origin is accepted
      (the Caddy deployment shape — host guard passes all three, origin guard
      matches the forwarded host);
    - the guard's resolved kwargs are exactly the configured tuple.
    """
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<!doctype html><div id="root"></div>')
    code = (
        "from starlette.testclient import TestClient\n"
        "from app.main import app\n"
        "c = TestClient(app)\n"
        "default = c.get('/api/health')\n"
        "named = c.get('/api/health', headers={'Host': 'music.example.test'})\n"
        "ip = c.get('/api/health', headers={'Host': '127.0.0.1:3030'})\n"
        "proxy = c.post('/api/config/validate', json={'yaml_text': 'a: 1'},"
        " headers={'Host': '127.0.0.1:3030',"
        " 'X-Forwarded-Host': 'music.example.test',"
        " 'Origin': 'http://music.example.test'})\n"
        "g = next(m.kwargs['allowed_hosts'] for m in app.user_middleware"
        " if m.cls.__name__ == 'HostGuardMiddleware')\n"
        "print(default.status_code, named.status_code, ip.status_code, proxy.status_code)\n"
        "print(tuple(g))\n"
    )
    env = {
        **os.environ,
        "MUSICDROP_STATIC_DIR": str(dist),
        "MUSICDROP_ALLOWED_HOSTS": "Music.Example.Test",
    }
    backend = Path(__file__).resolve().parents[1]
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=backend,
        timeout=180,
        check=False,
    )
    assert out.returncode == 0, f"child failed: stderr={out.stderr!r}"
    lines = out.stdout.strip().splitlines()
    assert lines[-2] == "400 200 200 200"
    assert lines[-1] == "('music.example.test',)"


def test_posture_log_emits_under_real_uvicorn(tmp_path: Path) -> None:
    """The startup posture line must actually reach the operator's console.

    No in-process caplog test can prove this: caplog attaches a handler to the
    root logger, so ANY logger name passes. Under the real Dockerfile entrypoint
    uvicorn configures only ``uvicorn``/``uvicorn.error``/``uvicorn.access`` and
    leaves root at WARNING with no handlers — an INFO record from ``app.main``
    is dropped before it reaches stdout, which is how the line shipped invisible.
    So boot real uvicorn in PROD posture and read its output.

    Guards the whole diagnostic: with a wrong allowlist every request 400s, and
    this line is the only thing that says which posture and which names are live.
    """
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<!doctype html><div id="root"></div>')
    env = {
        **os.environ,
        "MUSICDROP_STATIC_DIR": str(dist),
        "MUSICDROP_ALLOWED_HOSTS": "music.example.test",
        # Real uvicorn runs the lifespan, which OPENS a beets library; aim it at
        # a throwaway dir so the boot cannot touch the dev library `.env` points at.
        "MUSICDROP_BEETS_DIR": str(tmp_path / "beets"),
    }
    backend = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", "0", "--log-level", "info"],
        cwd=backend,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    seen: list[str] = []
    posture: str | None = None
    # Pump on a thread so a MISSING line fails on the deadline instead of
    # blocking forever in readline() once uvicorn goes quiet after startup.
    lines: queue.Queue[str | None] = queue.Queue()

    def _pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    reader = threading.Thread(target=_pump, daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=1.0)
            except queue.Empty:
                continue
            if line is None:  # child exited
                break
            seen.append(line)
            if "security posture:" in line:
                posture = line
                break
    finally:
        proc.terminate()
        proc.wait(timeout=30)
        reader.join(timeout=30)
    assert posture is not None, f"no posture line in uvicorn output: {seen!r}"
    assert "prod (static_dir set)" in posture
    assert "music.example.test" in posture
    assert "IP literals, localhost" in posture
