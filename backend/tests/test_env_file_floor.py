"""The suite reads no ``.env``: a local run sees the settings CI sees.

The floor is in the root ``conftest.py``. CI has no ``backend/.env``, so both
tests here build their own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, settings


def test_a_settings_built_in_a_test_ignores_a_dotenv_in_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("MUSICDROP_APP_NAME=CANARY-FROM-DOTENV\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MUSICDROP_APP_NAME", raising=False)

    assert Settings().app_name == "MusicDrop"


def test_the_app_singleton_holds_no_dotenv_value() -> None:
    """Built at the first ``app.config`` import, which the floor re-reads in place."""
    assert settings.model_dump() == Settings(_env_file=None).model_dump()  # type: ignore[call-arg]  # pydantic-settings' init kwarg
