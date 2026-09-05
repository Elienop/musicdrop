"""Which beets library the lifespan closes at shutdown.

``handle`` is bound once at boot and never rebound; a successful Apply rebinds
``app.state.beets_library``. Closing the first one leaves the SECOND open —
measured after an Apply: the old handle had 0 connections, the new one still had
1, ``select 1`` still answered on it, and ``/proc/self/fd`` still held an fd on
``library.db``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from beets.library import Library
from fastapi.testclient import TestClient

from app.beets.library import LibraryHandle, close_library
from app.main import app as real_app


@pytest.fixture
def boot_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway BEETSDIR the real lifespan can boot against.

    A SIBLING of the music dir: the boot gate refuses a beets data directory
    that contains the music library.
    """
    music = tmp_path / "music"
    music.mkdir()
    beets = tmp_path / "beets"
    beets.mkdir()
    (beets / "config.yaml").write_text(
        f"directory: {music}\nlibrary: library.db\nplugins:\n  - musicbrainz\n"
    )
    monkeypatch.setattr("app.config.settings.beets_dir", str(beets))
    return beets


def test_shutdown_closes_the_library_the_app_is_serving(
    boot_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After an Apply, the live handle is the one teardown has to close.

    Spied rather than probed for the connection: the lifespan runs on the portal
    thread, and beets keeps one connection PER THREAD, so "is it closed" is a
    question about a thread the test does not own. Which library was handed to
    ``close_library`` answers the same question without that dependency.
    """
    closed: list[Library] = []

    def spy(lib: Library) -> None:
        closed.append(lib)
        close_library(lib)

    monkeypatch.setattr("app.main.close_library", spy)

    with TestClient(real_app) as client:
        boot: LibraryHandle = real_app.state.beets_library
        assert client.post("/api/config/apply").status_code == 200
        live: LibraryHandle = real_app.state.beets_library
        assert live is not boot  # the control: the Apply really swapped it

    assert closed == [live.lib]
