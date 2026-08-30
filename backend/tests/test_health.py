"""``/api/health`` (anonymous, liveness only) and ``/api/version`` (gated).

The split is the point of this file. ``/api/health`` is reachable without a
cookie so the container HEALTHCHECK works, which makes its body public; the
running build moved out of it into ``/api/version``, behind the gate.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _anonymous() -> TestClient:
    """A client with no session cookie — the conftest seam installs one."""
    anon = TestClient(app)
    anon.cookies.clear()
    return anon


def test_health() -> None:
    resp = _anonymous().get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_leaks_no_version_to_an_anonymous_caller() -> None:
    """The absence is the fix, so the absence is what gets pinned.

    An exempt endpoint's body is readable by anyone who can reach the port, and
    a build number is what an attacker looks up known issues against. Asserted
    on the KEY rather than the whole body so it stays a statement about the
    leak even if the endpoint grows another public field.
    """
    assert "version" not in _anonymous().get("/api/health").json()


def test_the_version_endpoint_needs_a_session() -> None:
    """Gated by default: it is under ``/api/`` and not on the exempt list.

    Nothing in ``app/api/health.py`` opts this route in — the gate covers it
    because it covers everything. Pinned rather than assumed, because "it is
    automatic" is exactly the belief under which the version was public in the
    first place.

    This 401 on its own would NOT prove the route exists: the gate rejects
    before the router runs, so a misspelt or unregistered ``/api/`` path answers
    the same 401. The test below is the control — it needs a real route to
    return a body.
    """
    resp = _anonymous().get("/api/version")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "authentication required"}


def test_the_version_endpoint_reports_the_running_build(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "version", "v9.9.9")
    resp = client.get("/api/version")
    assert resp.status_code == 200
    assert resp.json() == {"version": "v9.9.9"}
