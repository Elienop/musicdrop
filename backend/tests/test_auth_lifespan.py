"""How the signing secret gets onto ``app.state`` — and stays there.

The middleware stack is declared at import time; the secret lives under
``settings.beets_dir``, which is not settled until the lifespan runs. The split
is: lifespan SEEDS (only if absent), middleware READS (per request, fail
closed). Both halves of "only if absent" are pinned here, because the failure
mode of getting it wrong is not subtle — an unconditional overwrite 401s every
request made inside a ``with TestClient(app)`` block, which is a dozen files.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth.session import session_secret_path
from app.main import app as real_app
from tests.conftest import TEST_SESSION_SECRET


@pytest.fixture
def beets_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway BEETSDIR with a minimal config, so the lifespan can boot.

    Aimed at ``tmp_path`` rather than the default, so a real lifespan here can
    never touch the dev library ``backend/.env`` points at.
    """
    music = tmp_path / "music"
    music.mkdir()
    (tmp_path / "config.yaml").write_text(
        f"directory: {music}\nlibrary: library.db\nplugins:\n  - musicbrainz\n"
    )
    monkeypatch.setattr("app.config.settings.beets_dir", str(tmp_path))
    return tmp_path


def test_a_preseeded_secret_survives_the_lifespan(beets_dir: Path) -> None:
    """The seam the whole suite rests on.

    ``tests/conftest.py`` seeds a fixed secret at import; a lifespan that
    overwrote it would invalidate every cookie the TestClient patch minted, and
    the ~30 tests that run inside a ``with TestClient(app)`` block would all
    401. Asserted from INSIDE the block, where a request would actually be made.
    """
    with TestClient(real_app):
        assert real_app.state.session_secret == TEST_SESSION_SECRET
    # ...and it is still there afterwards: teardown deletes only what THIS
    # lifespan created, so the next lifespan-less test still has a key.
    assert real_app.state.session_secret == TEST_SESSION_SECRET


def test_requests_inside_a_lifespan_block_are_authenticated(beets_dir: Path) -> None:
    """The behavioural half of the test above.

    An overwrite would leave the state consistent-looking and simply refuse
    every request, so this asserts the thing the other files actually depend on.
    """
    with TestClient(real_app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/openapi.json").status_code == 200


def test_no_secret_file_is_written_when_one_is_already_seeded(beets_dir: Path) -> None:
    """ "Only if absent" means no disk write either.

    A lifespan that created the file anyway would leave a stray key in every
    test's tmp BEETSDIR — harmless, but it would also mean the guard is on the
    wrong side of the load-or-create call.
    """
    with TestClient(real_app):
        pass
    assert not session_secret_path(str(beets_dir)).exists()


def test_a_first_boot_creates_the_secret_and_a_restart_reuses_it(
    beets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production path: no pre-seed, so the lifespan loads or creates.

    Two boots against one BEETSDIR must produce ONE key, or every container
    restart would sign the owner out.
    """
    monkeypatch.delattr(real_app.state, "session_secret")
    path = session_secret_path(str(beets_dir))

    with TestClient(real_app):
        first = real_app.state.session_secret
    assert isinstance(first, bytes)
    assert len(first) == 32
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # Teardown removed what it created, so the gate is not left holding a key
    # from a torn-down app.
    assert not hasattr(real_app.state, "session_secret")

    with TestClient(real_app):
        second = real_app.state.session_secret
    assert second == first
