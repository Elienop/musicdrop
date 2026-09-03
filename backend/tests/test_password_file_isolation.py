"""Pins: no test may write a password into a REAL beets library.

The mechanism under test is the autouse ``password_hash_file`` fixture in
``tests/conftest.py``; the shape follows ``tests/test_beetsdir_isolation.py``,
which closes the same class of hole for beets' own config dir.

The hole this closes. ``app/auth/source.py`` derives the stored-hash path from
``settings.beets_dir`` at CALL time, and ``backend/.env`` aims that at the
developer's real library on a dev box. Two production routes WRITE that file
(``POST /api/auth/setup`` and ``POST /api/auth/password``), so a test that
exercised either without isolating itself would plant a credential in the real
library — or overwrite the owner's own, whose recovery is deleting the file and
restarting.

Why an autouse floor rather than per-test isolation: the write is reachable from
any test that can reach the two routes, and "remember to pin it" has already
failed for this class twice (see ``backend/conftest.py``'s docstring on the
``BEETSDIR`` floor). The floor is O(1) and covers tests nobody has written yet.

The decoy below is the same discipline that file keeps: when the floor IS
removed (mutation-testing this file), the escaping write must land somewhere
harmless. So the test points ``settings.beets_dir`` at a decoy under
``tmp_path`` first, and asserts BOTH that the decoy is untouched and that the
bytes went to the pinned path. Removing a data-safety fix to prove its test is
honest must not itself cost data.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth.source import PASSWORD_HASH_FILENAME, password_hash_path
from app.config import settings
from app.main import app as real_app


def test_a_test_that_repoints_beets_dir_still_cannot_write_a_real_hash_file(
    password_hash_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real setup POST, with ``settings.beets_dir`` aimed at a decoy library.

    ``settings.beets_dir`` is what production derives the path from, and ~50
    tests repoint it for their own reasons — so the floor is pinned one level
    lower, at the resolver's own path seam. This test is the proof: the decoy
    stands in for the dev library, and nothing appears in it.
    """
    decoy = tmp_path / "decoy-library"
    decoy.mkdir()
    monkeypatch.setattr("app.config.settings.beets_dir", str(decoy))

    client = TestClient(real_app)
    client.cookies.clear()
    resp = client.post("/api/auth/setup", json={"password": "a first password"})

    assert resp.status_code == 200
    # Where it DID go: the per-test tmp path the autouse fixture pinned.
    assert password_hash_file.is_file()
    # ...and where it did not. Without the floor, this is the file that would
    # have appeared inside the developer's library.
    assert not (decoy / PASSWORD_HASH_FILENAME).exists()
    assert list(decoy.iterdir()) == []


def test_the_pinned_path_is_not_the_one_settings_would_derive(
    password_hash_file: Path,
) -> None:
    """The floor is doing something, and the derivation it replaces is real.

    Without this the fixture could pin the path at exactly what
    ``settings.beets_dir`` resolves to and every assertion above would pass
    trivially. It also pins that ``password_hash_path`` is the production
    derivation rather than a test-only helper: it is the function the resolver
    calls.
    """
    derived_from_settings = password_hash_path(settings.beets_dir)

    assert password_hash_file != derived_from_settings
    assert password_hash_file.name == derived_from_settings.name == PASSWORD_HASH_FILENAME


def test_no_test_starts_with_a_stored_password(password_hash_file: Path) -> None:
    """Per-test, not per-session: one test's password cannot leak into the next.

    ``tmp_path`` is per-test, so this is a property of the fixture's scope. A
    session-scoped pin would make the first test that ran setup close the setup
    form for every test after it, and the failures would look like flakiness.
    """
    assert not password_hash_file.exists()
