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

The last test here covers the READ side, which the autouse fixture cannot reach:
two module-import-time readers resolve the stored hash before any fixture runs,
so ``backend/conftest.py`` carries a second, process-level pin. See its docstring
for the measured failure that floor closes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth.source import PASSWORD_HASH_FILENAME, password_hash_path
from app.config import settings
from app.main import app as real_app
from conftest import SUITE_PASSWORD_HASH_PATH
from tests.conftest import low_cost_stored_hash


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


def test_the_process_floor_points_away_from_the_configured_beets_dir() -> None:
    """The import-time resolver answers inside the suite sandbox, not the library.

    Cheap half of the pin: the value itself. The behavioural half is below —
    this one only says the floor is not aimed at the directory
    ``settings.beets_dir`` names, which on a dev box is the real library.
    """
    assert SUITE_PASSWORD_HASH_PATH != password_hash_path(settings.beets_dir)
    assert not SUITE_PASSWORD_HASH_PATH.exists(), (
        "something in the suite wrote to the process floor's path; every test is"
        " supposed to be repointed at its own tmp_path by the autouse fixture"
    )


def test_a_stored_hash_in_the_configured_beets_dir_keeps_the_suite_green(
    tmp_path: Path,
) -> None:
    """A real ``password-hash`` under the configured beets dir must not turn tests red.

    The state a dev box lands in the first time the owner uses the setup form
    against ``backend/.env``'s library. Two reads happen at module-import time —
    ``app/main.py``'s posture clause and ``tests/test_health.py``'s module-level
    ``TestClient``, whose seam cookie is minted from ``effective_password()`` —
    and the per-test autouse fixture runs after both. Without the process floor
    the minted cookie bound to the real stored hash, the fixture then repointed
    the resolver at an empty tmp dir, and the gate answered ``401``.

    Run in a CHILD pytest because that is the only way to exercise import
    ordering: this process has already imported everything. ``test_health.py`` is
    the probe because its client is built at module level; the file is small, so
    the child costs a fraction of a second.
    """
    canary = tmp_path / "canary-beets-dir"
    canary.mkdir()
    (canary / PASSWORD_HASH_FILENAME).write_text(
        low_cost_stored_hash("a password the owner really set") + "\n", encoding="utf-8"
    )
    backend = Path(__file__).resolve().parents[1]

    child = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_health.py"],
        cwd=backend,
        env={
            **os.environ,
            "MUSICDROP_BEETS_DIR": str(canary),
            "BEETSDIR": str(canary),
            # Pinned rather than inherited: a non-empty env hash would win over
            # the file and the child would never read it, so the probe would
            # pass without measuring anything.
            "MUSICDROP_PASSWORD_HASH": "",
        },
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert child.returncode == 0, f"child pytest failed:\n{child.stdout}\n{child.stderr}"
    # The credential is read-only to the suite: the child must not have rewritten
    # what it found, which is the other half of "no test touches a real hash file".
    assert (canary / PASSWORD_HASH_FILENAME).read_text(encoding="utf-8").startswith("scrypt$")
