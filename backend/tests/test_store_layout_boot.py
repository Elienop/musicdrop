"""The containment rule at STARTUP: a refused layout must not come up.

Three inputs (``MUSICDROP_TRASH_DIR``, ``MUSICDROP_TRASH_ORIGINS_DIR``,
``MUSICDROP_BEETS_DIR``) are env-derived and settled once per process, so the
boot check is where a bad layout is caught before anything can delete on it.
Two levels of pin, because they answer different questions:

* in-process, through ``TestClient``'s lifespan — that the refusal HAPPENS and
  carries the reason;
* in a real uvicorn child — that the line reaches the operator's console at all.
  ``caplog`` cannot answer the second: it attaches a handler to the ROOT logger,
  so a record from any logger name passes it, while under the Dockerfile CMD
  uvicorn configures only its own loggers and leaves root at WARNING with no
  handler (the same trap ``test_host_guard`` documents for the posture line).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.beets.store_layout import StoreLayoutError
from app.main import app as real_app
from tests.test_host_guard import _real_uvicorn


@pytest.fixture
def beets_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway BEETSDIR whose ``config.yaml`` points at a sibling music dir.

    Same shape as ``tests/test_auth_lifespan.py``'s fixture and for the same
    reason: a real lifespan runs here, so it must never reach the dev library
    ``backend/.env`` points at.
    """
    beets = tmp_path / "beets"
    beets.mkdir()
    music = tmp_path / "music"
    music.mkdir()
    (beets / "config.yaml").write_text(
        f"directory: {music}\nlibrary: library.db\nplugins:\n  - musicbrainz\n"
    )
    monkeypatch.setattr("app.config.settings.beets_dir", str(beets))
    return beets


def test_a_trash_dir_at_the_music_root_refuses_to_start(
    beets_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``MUSICDROP_TRASH_DIR=/music`` — one typo from the default — does not boot.

    ERROR, not WARNING: the process is refusing to come up, and the operator
    grepping for the reason after uvicorn's "Application startup failed" needs a
    level that says so. Through ``uvicorn.error``, not ``app.main``, for the
    reason main.py's posture-line comment gives.
    """
    monkeypatch.setattr("app.config.settings.trash_dir", str(tmp_path / "music"))
    with caplog.at_level(logging.ERROR), pytest.raises(StoreLayoutError):
        with TestClient(real_app):
            pass  # pragma: no cover - the lifespan raises before the body runs

    refusals = [r for r in caplog.records if r.name == "uvicorn.error"]
    assert len(refusals) == 1, [(r.name, r.getMessage()) for r in caplog.records]
    assert refusals[0].levelno == logging.ERROR
    message = refusals[0].getMessage()
    assert "refusing to start" in message
    assert "The Trash directory is the music library" in message
    assert "MUSICDROP_TRASH_DIR" in message


def test_a_trash_dir_that_will_not_resolve_still_gets_the_one_error_line(
    beets_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A symlink loop used to escape the gate entirely.

    ``Path.resolve()`` raises ``RuntimeError`` on it, ``main.py`` catches
    ``StoreLayoutError``, so the operator got a raw lifespan traceback and NO
    "refusing to start" line — the single diagnostic this gate exists to print.
    Asserted on the log record, not just on the raise, for that reason.
    """
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    monkeypatch.setattr("app.config.settings.trash_dir", str(loop))

    with caplog.at_level(logging.ERROR), pytest.raises(StoreLayoutError):
        with TestClient(real_app):
            pass  # pragma: no cover - the lifespan raises before the body runs

    refusals = [r for r in caplog.records if r.name == "uvicorn.error"]
    assert len(refusals) == 1, [(r.name, r.getMessage()) for r in caplog.records]
    message = refusals[0].getMessage()
    assert "refusing to start" in message
    assert "MUSICDROP_TRASH_DIR could not be resolved" in message


def test_the_origin_store_inside_the_library_refuses_to_start(
    beets_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the ruling, at the same gate: the store stays out of /music.

    A second test rather than a parametrize case because it exercises a
    DIFFERENT setting, and the point of the boot gate is that both reach it.
    """
    monkeypatch.setattr(
        "app.config.settings.trash_origins_dir", str(tmp_path / "music" / "records")
    )
    with pytest.raises(StoreLayoutError):
        with TestClient(real_app):
            pass  # pragma: no cover - the lifespan raises before the body runs


def test_the_default_layout_still_boots(beets_dir: Path) -> None:
    """The control. Without it the two tests above pass on a gate that refuses
    everything, which is the failure mode a fail-closed check has."""
    with TestClient(real_app) as client:
        assert client.get("/api/health").status_code == 200


def test_a_beets_dir_inside_the_library_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``MUSICDROP_BEETS_DIR=/music/musicdrop`` — refused as of the review round.

    It used to boot with ``MUSICDROP_TRASH_ORIGINS_DIR`` pointed outside, which
    is why the store is moved out HERE too: without that the refusal could come
    from the old ``M contains O`` rule instead, and the test would pass without
    the beets-dir rule existing at all.
    """
    music = tmp_path / "music"
    beets = music / "musicdrop"
    beets.mkdir(parents=True)
    (beets / "config.yaml").write_text(
        f"directory: {music}\nlibrary: library.db\nplugins:\n  - musicbrainz\n"
    )
    monkeypatch.setattr("app.config.settings.beets_dir", str(beets))
    monkeypatch.setattr("app.config.settings.trash_origins_dir", str(tmp_path / "records"))
    with pytest.raises(StoreLayoutError) as exc:
        with TestClient(real_app):
            pass  # pragma: no cover - the lifespan raises before the body runs
    assert "The music library contains the beets data directory" in str(exc.value)


def test_a_library_inside_the_beets_dir_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other nesting direction, at the same gate.

    Trash and the store are left at their defaults under ``B`` — both allowed —
    so the only thing this layout breaks is the new ``B contains M`` rule.
    """
    beets = tmp_path / "beets"
    beets.mkdir()
    music = beets / "music"
    music.mkdir()
    (beets / "config.yaml").write_text(
        f"directory: {music}\nlibrary: library.db\nplugins:\n  - musicbrainz\n"
    )
    monkeypatch.setattr("app.config.settings.beets_dir", str(beets))
    with pytest.raises(StoreLayoutError) as exc:
        with TestClient(real_app):
            pass  # pragma: no cover - the lifespan raises before the body runs
    assert "The beets data directory contains the music library" in str(exc.value)


def test_a_database_inside_the_trash_refuses_to_start(
    beets_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``library:`` pointed into Trash — the DB file, not one of the four dirs.

    The boot check reads it off the handle that was actually opened
    (``handle.lib.path``), so a hand-edited ``library:`` is caught by the same
    gate as a hand-edited ``directory:``.
    """
    trash = tmp_path / "bin"
    trash.mkdir()  # beets opens the DB before the gate runs; sqlite needs the dir
    monkeypatch.setattr("app.config.settings.trash_dir", str(trash))
    (beets_dir / "config.yaml").write_text(
        f"directory: {tmp_path / 'music'}\n"
        f"library: {trash / 'library.db'}\n"
        "plugins:\n  - musicbrainz\n"
    )
    with pytest.raises(StoreLayoutError) as exc:
        with TestClient(real_app):
            pass  # pragma: no cover - the lifespan raises before the body runs
    assert "The Trash directory contains the beets database" in str(exc.value)


def test_the_refusal_reaches_a_real_uvicorns_output(tmp_path: Path) -> None:
    """What ``docker logs`` shows, from a separate process with no test handlers.

    The child's starter config resolves ``directory: ../music`` against BEETSDIR
    (confuse joins a relative filename to ``config_dir()``), so pointing
    ``MUSICDROP_TRASH_DIR`` at that sibling is the ``T == M`` case.
    """
    beets = tmp_path / "beets"
    with _real_uvicorn(beets, extra_env={"MUSICDROP_TRASH_DIR": str(tmp_path / "music")}) as child:
        line = child.wait_for("refusing to start", timeout=60.0)
        # uvicorn's own verdict, so a line that merely printed while the app came
        # up anyway would not pass this.
        failed = child.wait_for("Application startup failed", timeout=60.0)

    assert "ERROR" in line, line
    assert "The Trash directory is the music library" in line, line
    assert "MUSICDROP_TRASH_DIR" in line, line
    assert "Application startup failed" in failed, failed
