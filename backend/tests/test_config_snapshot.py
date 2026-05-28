"""Tests for ``build_config_snapshot`` and the redaction/restart-required logic.

The shared autouse ``_clear_beets_globals`` fixture in ``tests/conftest.py``
resets ``beets.config`` and the plugin registry between every test, so each
case here starts from a clean confuse singleton.
"""

import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import beets
import pytest

from app.beets.config_snapshot import build_config_snapshot
from app.beets.library import LibraryHandle, close_library
from app.beets.setup import setup_beets


@pytest.fixture
def loaded_handle(tmp_path: Path) -> Iterator[LibraryHandle]:
    """A real ``setup_beets()`` handle under a tmp BEETSDIR (starter copy)."""
    handle = setup_beets(str(tmp_path))
    try:
        yield handle
    finally:
        close_library(handle.lib)


def test_yaml_text_redacts_per_view_flag(loaded_handle: LibraryHandle) -> None:
    beets.config["spotify"]["client_secret"].set("supersecret")
    beets.config["spotify"]["client_secret"].redact = True
    snap = build_config_snapshot(loaded_handle)
    assert "supersecret" not in snap.yaml_text
    assert "REDACTED" in snap.yaml_text


def test_yaml_text_safety_net_masks_unmarked_secrets(
    loaded_handle: LibraryHandle,
) -> None:
    beets.config["mything"]["api_key"].set("leakme")
    snap = build_config_snapshot(loaded_handle)
    assert "leakme" not in snap.yaml_text
    assert "REDACTED" in snap.yaml_text


def test_restart_required_when_mtime_advances(loaded_handle: LibraryHandle) -> None:
    cfg = loaded_handle.config_path
    newer = cfg.stat().st_mtime + 10
    os.utime(cfg, (newer, newer))
    snap = build_config_snapshot(loaded_handle)
    assert snap.restart_required is True
    assert snap.file_modified_at is not None


def test_file_modified_at_none_when_file_missing(
    loaded_handle: LibraryHandle,
) -> None:
    loaded_handle.config_path.unlink()
    snap = build_config_snapshot(loaded_handle)
    assert snap.file_modified_at is None
    assert snap.restart_required is True


def test_fresh_snapshot_has_no_restart_required(
    loaded_handle: LibraryHandle,
) -> None:
    snap = build_config_snapshot(loaded_handle)
    assert snap.restart_required is False
    assert isinstance(snap.loaded_at, datetime)
    assert snap.loaded_at.tzinfo is not None
