"""Tests for ``build_config_snapshot`` and the redaction/apply-pending logic.

The shared autouse ``_clear_beets_globals`` fixture in ``tests/conftest.py``
resets ``beets.config`` and the plugin registry between every test, so each
case here starts from a clean confuse singleton.
"""

import os
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import beets
import pytest

from app.beets.config_snapshot import (
    build_config_snapshot,
    find_redacted_paths,
)
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


def test_apply_pending_when_mtime_advances(loaded_handle: LibraryHandle) -> None:
    cfg = loaded_handle.config_path
    newer = cfg.stat().st_mtime + 10
    os.utime(cfg, (newer, newer))
    snap = build_config_snapshot(loaded_handle)
    assert snap.apply_pending is True
    assert snap.file_modified_at is not None
    # CAS fields are populated whenever the file is readable.
    assert snap.mtime_ns > 0
    assert len(snap.sha256) == 64  # hex sha256


def test_file_modified_at_none_when_file_missing(
    loaded_handle: LibraryHandle,
) -> None:
    loaded_handle.config_path.unlink()
    snap = build_config_snapshot(loaded_handle)
    assert snap.file_modified_at is None
    assert snap.apply_pending is True
    # Missing-file path leaves CAS fields at their zero values.
    assert snap.mtime_ns == 0
    assert snap.sha256 == ""


def test_fresh_snapshot_has_no_apply_pending(
    loaded_handle: LibraryHandle,
) -> None:
    snap = build_config_snapshot(loaded_handle)
    assert snap.apply_pending is False
    assert isinstance(snap.loaded_at, datetime)
    # Pin UTC specifically — any other tz would still pass `is not None` but
    # break the BeetsConfigSnapshot contract (`loaded_at` is documented UTC).
    assert snap.loaded_at.utcoffset() == timedelta(0)
    # CAS fields are real (non-default) for the live file.
    assert snap.mtime_ns > 0
    assert len(snap.sha256) == 64


def test_safety_net_recurses_into_lists_of_dicts(loaded_handle: LibraryHandle) -> None:
    """Plugin configs like ``accounts: [{api_token: "..."}, ...]`` must be redacted.

    Without list-recursion the leaf hides under a list and slips through both
    the confuse per-view pass (the plugin didn't mark it) and the regex
    safety-net (which only walked dict values).
    """
    beets.config["mything"]["accounts"].set([{"token": "leakme"}])
    snap = build_config_snapshot(loaded_handle)
    assert "leakme" not in snap.yaml_text
    assert "REDACTED" in snap.yaml_text


def test_safety_net_masks_pwd_and_apisecret_variants(loaded_handle: LibraryHandle) -> None:
    """Real bundled plugins use ``pwd`` (kodiupdate) and ``apisecret`` (beatport).

    The previous anchored pattern missed both; this test pins the chosen
    permissive substring pattern so a future "tighten the regex" change can't
    silently regress coverage on these real keys.
    """
    beets.config["beatport"]["apisecret"].set("bp-leak")
    beets.config["kodiupdate"]["pwd"].set("kodi-leak")
    snap = build_config_snapshot(loaded_handle)
    assert "bp-leak" not in snap.yaml_text
    assert "kodi-leak" not in snap.yaml_text


def test_find_redacted_paths_recurses_into_nested_lists() -> None:
    """Lockstep with ``_mask_secrets_in_place``: both must descend into
    ``list[list[dict]]``. Display redacts these via the mask helper; save's
    secret-preserve merge depends on ``find_redacted_paths`` finding them too.
    Asymmetry = display redacts but save can't preserve (or vice versa).

    No real beets/plugin config nests lists today; this test pins the
    invariant before someone adds one and silently leaks a fresh secret on
    save.
    """
    data: dict[str, object] = {"a": [[{"client_secret": "x"}]]}
    assert find_redacted_paths(data) == [("a", "client_secret")]


def test_find_redacted_paths_dedups_within_list_of_dicts() -> None:
    """``accounts: [{api_token: ...}, {api_token: ...}]`` must yield each
    distinct path once, not once per list item. Downstream
    ``merge_preserve_secrets`` walks the result and would do redundant work on
    duplicates."""
    data: dict[str, object] = {
        "accounts": [
            {"api_token": "a"},
            {"api_token": "b"},
            {"api_token": "c"},
        ]
    }
    assert find_redacted_paths(data) == [("accounts", "api_token")]


def test_find_redacted_paths_walks_top_level_list() -> None:
    """A bare list at the top level must be walked, matching
    ``_mask_secrets_in_place``'s ``if isinstance(d, list)`` entry branch.
    Beets configs never start with a list, but the helper is reused on
    arbitrary sub-trees and the symmetry with the mask helper is the contract.
    """
    assert find_redacted_paths([{"client_secret": "x"}]) == [("client_secret",)]
