"""Host-guard policy tests: the DNS-rebinding allowlist predicate.

Wire tests on the real app live further down (Task 3); this section tests the
pure policy in isolation. Exactness tests are load-bearing: mutating the
allowlist compare from ``==``/``in``-tuple to ``endswith``/substring is THE
canonical origin/host-validation bug (see the #153 deep-review lesson) and
survives any suite whose negative fixtures share no substring with an allowed
name — every near-miss below shares one.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.auth.source import PASSWORD_HASH_FILENAME
from app.host_guard import HostGuardMiddleware, host_allowed, resolve_allowed_hosts
from app.main import app as real_app
from tests.conftest import low_cost_stored_hash


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
    # Ordering pin: HostGuard wraps outside the body limit, so a wrong-host
    # oversize body gets the host 400, not the body-limit 413. (It is no longer
    # outermost - CORS and the security-headers stamper wrap outside it since
    # 8eda506 (#181) - but neither of those rejects an ordinary request, so this
    # precedence is unchanged. The oversize-vs-ORIGIN-guard pin lives in
    # test_body_limit.py and is likewise unchanged.)
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
    # No conftest in the child, so the session secret is seeded and a cookie
    # minted here. The three GETs hit `/api/health`, which the session gate
    # exempts, but the reverse-proxy WRITE below is gated — un-authenticated it
    # would report 401 and the proxy assertion would silently stop testing the
    # host guard. Scaffolding only; every assertion still pins HOST posture.
    code = (
        "from starlette.testclient import TestClient\n"
        "from app.main import app\n"
        "from app.auth.session import SESSION_COOKIE_NAME, mint_session_token\n"
        # The gate signs with the EFFECTIVE hash (env var, else the stored
        # file), so the child mints under the same resolver rather than under
        # settings.password_hash alone — otherwise a password-hash file left in
        # whatever beets dir the child resolves would make every gated request
        # 401 and this test would pass or fail for the wrong reason.
        "from app.auth.source import effective_password\n"
        "secret = b'0123456789abcdef0123456789abcdef'\n"
        "app.state.session_secret = secret\n"
        "c = TestClient(app, cookies={SESSION_COOKIE_NAME:"
        " mint_session_token(secret, effective_password()[0])})\n"
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
        # The child runs without conftest, so nothing pins the stored-hash
        # file: aim MUSICDROP_BEETS_DIR at a throwaway dir so it cannot read
        # (or be decided by) the one in the dev library `.env` points at.
        "MUSICDROP_BEETS_DIR": str(tmp_path / "beets"),
        # Hermetic binding: the session token is signed with a key derived
        # from MUSICDROP_PASSWORD_HASH, and an env var beats backend/.env —
        # so an owner who sets a real hash locally cannot change what this
        # child mints under.
        "MUSICDROP_PASSWORD_HASH": "",
    }
    backend = Path(__file__).resolve().parents[1]
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**env, "PYTHONPATH": str(backend)},
        # Not ``backend``: ``Settings()`` reads ``.env`` from the cwd.
        cwd=tmp_path,
        timeout=180,
        check=False,
    )
    assert out.returncode == 0, f"child failed: stderr={out.stderr!r}"
    lines = out.stdout.strip().splitlines()
    assert lines[-2] == "400 200 200 200"
    assert lines[-1] == "('music.example.test',)"


class _UvicornChild:
    """A live ``uvicorn app.main:app`` process, with its output pumped to a queue.

    Pumping on a thread rather than reading inline so a MISSING line fails on a
    deadline instead of blocking forever in ``readline()`` once uvicorn goes
    quiet. ``stderr`` is merged into ``stdout``: uvicorn's default config sends
    its own records to stderr and the access log to stdout, and a test that
    asks "what would the operator see in ``docker logs``?" wants both.
    """

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self._proc = proc
        self._queue: queue.Queue[str | None] = queue.Queue()
        self.seen: list[str] = []
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            self._queue.put(line)
        self._queue.put(None)  # the child's output ended

    def wait_for(self, needle: str, *, timeout: float = 60.0) -> str:
        """The next output line containing ``needle``, or fail saying what came instead."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if line is None:  # child exited
                break
            self.seen.append(line)
            if needle in line:
                return line
        raise AssertionError(f"no {needle!r} line in uvicorn output: {self.seen!r}")

    def port(self) -> int:
        """The port uvicorn actually bound, read back from its own startup line."""
        line = self.wait_for("Uvicorn running on")
        _, _, tail = line.partition("http://127.0.0.1:")
        digits = tail.split()[0].strip().rstrip("/")
        assert digits.isdigit(), line
        return int(digits)

    def close(self) -> None:
        self._proc.terminate()
        self._proc.wait(timeout=30)
        self._reader.join(timeout=30)


@contextmanager
def _real_uvicorn(
    beets_dir: Path,
    *,
    static_dir: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> Iterator[_UvicornChild]:
    """Boot ``app.main:app`` under real uvicorn, exactly as the Dockerfile CMD does.

    No ``--log-level`` and no ``--log-config``, because ``Dockerfile:61`` passes
    neither: what these tests are for is the output an operator gets from the
    SHIPPED command, and a flag here would be a configuration the container does
    not have.

    ``extra_env`` is merged LAST, so a caller can set a ``MUSICDROP_*`` the
    fixed block above does not (``tests/test_store_layout_boot.py`` boots a
    refused Trash layout that way).
    """
    env = {
        **os.environ,
        "MUSICDROP_STATIC_DIR": str(static_dir) if static_dir else "",
        "MUSICDROP_ALLOWED_HOSTS": "music.example.test",
        # Real uvicorn runs the lifespan, which OPENS a beets library; aim it at
        # a throwaway dir so the boot cannot touch the dev library `.env` points at.
        "MUSICDROP_BEETS_DIR": str(beets_dir),
        # Pin the auth clause's input rather than inheriting it: an env var
        # beats `backend/.env`, so an owner who sets a real hash locally does
        # not turn these assertions red.
        "MUSICDROP_PASSWORD_HASH": "",
        **(extra_env or {}),
    }
    backend = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "0"],
        # Not ``backend``: ``Settings()`` reads ``.env`` from the cwd.
        cwd=beets_dir.parent,
        env={**env, "PYTHONPATH": str(backend)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    child = _UvicornChild(proc)
    try:
        yield child
    finally:
        child.close()


def _posture_line_under_real_uvicorn(tmp_path: Path, beets_dir: Path) -> str:
    """Boot real uvicorn in PROD posture and return its ``security posture:`` line.

    Shared by the two tests below, which differ only in what they seed into
    ``beets_dir`` — the whole point of the second one is that the SAME emission
    site has to report a configured server as well as an empty one.
    """
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<!doctype html><div id="root"></div>')
    with _real_uvicorn(beets_dir, static_dir=dist) as child:
        return child.wait_for("security posture:")


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
    posture = _posture_line_under_real_uvicorn(tmp_path, tmp_path / "beets")

    assert "prod (static_dir set)" in posture
    assert "music.example.test" in posture
    assert "IP literals, localhost" in posture
    # The auth clause, on the same line and through the same logger. The child's
    # MUSICDROP_PASSWORD_HASH is empty and its beets dir is fresh, so this is the
    # state a fresh deploy is in — an app that answers nothing until the operator
    # sets one, which is exactly the case that must not boot silently.
    assert "NO password configured" in posture
    assert "MUSICDROP_PASSWORD_HASH" in posture
    # The cookie clause is a RULE, not a state: Secure is decided per request
    # from that request's scheme, so the line must not claim a boot-time value
    # an operator behind a TLS proxy would read as false.
    assert "session cookie: Secure on HTTPS requests, plain otherwise" in posture


def test_the_posture_log_reports_a_CONFIGURED_server_too(tmp_path: Path) -> None:
    """The return journey, at the emission site rather than in the helper.

    ``auth_posture`` is pinned per state by unit tests, but until this existed
    the ARGUMENT the boot line passes was not: replacing it with "nothing is
    configured" left the whole suite green, so a server with a perfectly good
    stored password could announce that every request would be rejected. This is
    the only test that boots the module with a password in place.
    """
    beets_dir = tmp_path / "beets"
    beets_dir.mkdir()
    (beets_dir / PASSWORD_HASH_FILENAME).write_text(
        low_cost_stored_hash("the operator's password") + "\n", encoding="utf-8"
    )

    posture = _posture_line_under_real_uvicorn(tmp_path, beets_dir)

    assert "password configured from" in posture
    assert str(beets_dir / PASSWORD_HASH_FILENAME) in posture
    assert "NO password configured" not in posture


#: ``socket.getaddrinfo`` as the stdlib defines it, captured at IMPORT time —
#: which is before ``tests/conftest.py::_resolve_hosts_public`` (autouse) swaps
#: in one that answers 93.184.216.34 for every host so the SSRF guard is inert.
#: The test below is the suite's only one that opens a real socket, and under
#: that stub its request to the child on 127.0.0.1 dialled a public address and
#: timed out instead of failing on anything to do with logging.
_REAL_GETADDRINFO = socket.getaddrinfo


def _post_to_the_child(port: int, path: str, payload: dict[str, str], *, cookie: str = "") -> str:
    """POST JSON over a real socket and return the session cookie it set.

    ``urllib`` rather than ``TestClient``: the point of these two tests is what
    a separate PROCESS writes to its own stderr, so the request has to leave
    this one. No ``Origin`` header, which is what curl and the container
    healthcheck send too, so the CSRF guard passes it through.
    """
    headers = {"content-type": "application/json"}
    if cookie:
        headers["cookie"] = cookie
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            assert response.status == 200, response.status
            set_cookie: str = response.headers.get("set-cookie", "")
            return set_cookie.split(";")[0]
    except urllib.error.HTTPError as exc:  # pragma: no cover - a failing route
        raise AssertionError(f"{path} answered {exc.code}: {exc.read()!r}") from exc


def test_both_password_lines_reach_real_uvicorns_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setup and change each write a levelled line to the process's own output.

    README points an operator at these two lines, and no in-process test can
    tell whether they arrive: ``caplog`` attaches a handler to the ROOT logger,
    so a record from any logger name passes it. Under the shipped CMD
    (``Dockerfile:61``, no log config) uvicorn's LOGGING_CONFIG configures only
    its own loggers and leaves root at WARNING with no handler. Measured through
    ``logging.getLogger(__name__)``: the setup WARNING reached stderr only via
    ``logging.lastResort``, printed bare with no level to grep for, and the
    change INFO did not appear at all.

    So both go through ``uvicorn.error``, the way the boot posture line already
    does, and this asserts the LEVEL as well as the sentence — the level marker
    is what the bare last-resort spelling loses.
    """
    # Runs after the autouse stub and therefore wins, the way test_artwork_ssrf
    # re-stubs it: without this, 127.0.0.1 resolves to a public address.
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    first, second = "the-first-password", "the-second-password"
    beets_dir = tmp_path / "beets"
    quoted_path = repr(str(beets_dir / PASSWORD_HASH_FILENAME))

    with _real_uvicorn(beets_dir) as child:
        port = child.port()
        cookie = _post_to_the_child(port, "/api/auth/setup", {"password": first})
        # Short deadlines: each POST has already answered 200 by now, so the
        # record was written before the response left the child. A generous one
        # would only make a regression take a minute to report.
        setup_line = child.wait_for("first-run setup stored a password at", timeout=15.0)
        _post_to_the_child(
            port,
            "/api/auth/password",
            {"current_password": first, "new_password": second},
            cookie=cookie,
        )
        change_line = child.wait_for("the stored password was changed at", timeout=15.0)

    assert "WARNING" in setup_line, setup_line
    assert quoted_path in setup_line, setup_line
    # INFO, not WARNING: a change is an expected administrative action where
    # setup is a one-way change of the instance's posture.
    assert "INFO" in change_line, change_line
    assert quoted_path in change_line, change_line
    for line in (setup_line, change_line):
        for secret in (first, second):
            assert secret not in line, line
