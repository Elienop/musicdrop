"""The suite reads no ``.env`` and none of the dev box's own data.

The floor is in the root ``conftest.py``. CI has no ``backend/.env``, so the
first test here writes one of its own and the last runs a child pytest from a
cwd that holds one.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import plexapi
import pytest

from app.config import (
    Settings,
    resolve_artist_image_cache_dir,
    resolve_cover_thumb_cache_dir,
    settings,
)
from conftest import SUITE_DATA_DIR


def test_a_settings_built_in_a_test_ignores_a_dotenv_in_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("MUSICDROP_APP_NAME=CANARY-FROM-DOTENV\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MUSICDROP_APP_NAME", raising=False)

    assert Settings().app_name == "MusicDrop"


def test_the_app_singleton_holds_no_dotenv_value() -> None:
    """Built at the first ``app.config`` import, which the floor runs from a sandbox."""
    assert settings.model_dump() == Settings(_env_file=None).model_dump()  # type: ignore[call-arg]  # pydantic-settings' init kwarg


def test_the_image_caches_and_the_plexapi_config_sit_in_the_suite_sandbox() -> None:
    """Their defaults are the repo root's ``data/cache`` and ``~/.config/plexapi``."""
    assert resolve_artist_image_cache_dir() == SUITE_DATA_DIR / "artist-images"
    assert resolve_cover_thumb_cache_dir() == SUITE_DATA_DIR / "cover-thumbs"
    assert Path(plexapi.CONFIG_PATH) == SUITE_DATA_DIR / "plexapi-config.ini"


def test_a_dotenv_in_the_cwd_does_not_reach_the_app_singleton(tmp_path: Path) -> None:
    """A child pytest started from a cwd holding a canary ``.env``.

    The singleton is built when ``app.config`` is first imported; a floor that
    only fixed later ``Settings()`` would leave the canary in it.
    """
    (tmp_path / ".env").write_text("MUSICDROP_APP_NAME=CANARY-FROM-DOTENV\n", encoding="utf-8")
    test = f"{Path(__file__).resolve()}::test_the_app_singleton_holds_no_dotenv_value"
    env = {k: v for k, v in os.environ.items() if k != "MUSICDROP_APP_NAME"}

    child = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly", test],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert child.returncode == 0, f"child pytest failed:\n{child.stdout}\n{child.stderr}"
    assert "1 passed" in child.stdout, child.stdout
