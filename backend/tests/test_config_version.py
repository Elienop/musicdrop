"""Settings.version is env-driven (MUSICDROP_VERSION) with a dev fallback.

`_env_file=None` keeps a developer's local backend/.env from leaking into
the assertions (the suite must pass with or without one present).
"""

import pytest

from app.config import Settings


def test_version_defaults_to_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MUSICDROP_VERSION", raising=False)
    # type ignore: _env_file is a real pydantic-settings init kwarg the mypy
    # plugin's synthesized signature doesn't know about.
    assert Settings(_env_file=None).version == "dev"  # type: ignore[call-arg]


def test_version_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSICDROP_VERSION", "v1.2.3")
    assert Settings(_env_file=None).version == "v1.2.3"  # type: ignore[call-arg]
