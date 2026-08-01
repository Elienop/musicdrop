"""Tests for ``build_config_snapshot`` and the redaction/apply-pending logic.

The shared autouse ``_clear_beets_globals`` fixture in ``tests/conftest.py``
resets ``beets.config`` and the plugin registry between every test, so each
case here starts from a clean confuse singleton.
"""

import copy
import os
from collections.abc import Iterator
from datetime import datetime, timedelta
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


def test_yaml_text_is_raw_on_disk(loaded_handle: LibraryHandle) -> None:
    """The editable ``yaml_text`` is the user's file byte-for-byte — comments
    and all — NOT the flattened dump (fixes the save that rewrote config.yaml as
    a comment-free, every-default-pinned flatten)."""
    on_disk = loaded_handle.config_path.read_text(encoding="utf-8")
    snap = build_config_snapshot(loaded_handle)
    assert snap.yaml_text == on_disk
    # The starter config is comment-rich; a comment line proves it is not the
    # (comment-free) flatten dump.
    assert "#" in snap.yaml_text


def test_effective_yaml_has_merged_defaults(loaded_handle: LibraryHandle) -> None:
    """The read-only ``effective_yaml`` is the fully-merged view: it carries
    beets defaults the sparse user file never lists."""
    snap = build_config_snapshot(loaded_handle)
    # ``import:`` with its defaults is materialized in the merged view.
    assert "import:" in snap.effective_yaml
    # The merged view is distinct from the raw file (it pins defaults).
    assert snap.effective_yaml != snap.yaml_text


def test_effective_yaml_redacts_per_view_flag(loaded_handle: LibraryHandle) -> None:
    beets.config["spotify"]["client_secret"].set("supersecret")
    beets.config["spotify"]["client_secret"].redact = True
    snap = build_config_snapshot(loaded_handle)
    assert "supersecret" not in snap.effective_yaml
    assert "REDACTED" in snap.effective_yaml


def test_effective_yaml_safety_net_masks_unmarked_secrets(
    loaded_handle: LibraryHandle,
) -> None:
    beets.config["mything"]["api_key"].set("leakme")
    snap = build_config_snapshot(loaded_handle)
    assert "leakme" not in snap.effective_yaml
    assert "REDACTED" in snap.effective_yaml


def test_apply_pending_when_mtime_advances(loaded_handle: LibraryHandle) -> None:
    cfg = loaded_handle.config_path
    newer = cfg.stat().st_mtime + 10
    os.utime(cfg, (newer, newer))
    snap = build_config_snapshot(loaded_handle)
    assert snap.apply_pending is True
    assert snap.file_modified_at is not None
    # The CAS token is populated whenever the file is readable.
    assert len(snap.sha256) == 64  # hex sha256


def test_file_modified_at_none_when_file_missing(
    loaded_handle: LibraryHandle,
) -> None:
    loaded_handle.config_path.unlink()
    snap = build_config_snapshot(loaded_handle)
    assert snap.file_modified_at is None
    assert snap.apply_pending is True
    # Missing-file path leaves the CAS token at its zero value and the editable
    # doc empty (the effective view still renders from in-memory beets.config).
    assert snap.sha256 == ""
    assert snap.yaml_text == ""


def test_yaml_text_empty_on_non_utf8_file(loaded_handle: LibraryHandle) -> None:
    """A config.yaml corrupted to non-UTF-8 must degrade to an empty editable doc
    (like a missing file), NOT 500 the settings page. ``decode`` raises
    ``UnicodeDecodeError`` (a ``ValueError``, not ``OSError``), so the read guard
    has to catch it too — otherwise it escapes ``build_config_snapshot``."""
    loaded_handle.config_path.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    snap = build_config_snapshot(loaded_handle)  # must not raise
    assert snap.yaml_text == ""
    # The merged view still renders from the in-memory beets.config.
    assert snap.effective_yaml != ""


def test_fresh_snapshot_has_no_apply_pending(
    loaded_handle: LibraryHandle,
) -> None:
    snap = build_config_snapshot(loaded_handle)
    assert snap.apply_pending is False
    assert isinstance(snap.loaded_at, datetime)
    # Pin UTC specifically — any other tz would still pass `is not None` but
    # break the BeetsConfigSnapshot contract (`loaded_at` is documented UTC).
    assert snap.loaded_at.utcoffset() == timedelta(0)
    # The CAS token is real (non-default) for the live file.
    assert len(snap.sha256) == 64


def test_safety_net_recurses_into_lists_of_dicts(loaded_handle: LibraryHandle) -> None:
    """Plugin configs like ``accounts: [{api_token: "..."}, ...]`` must be redacted
    in the effective view.

    Without list-recursion the leaf hides under a list and slips through both
    the confuse per-view pass (the plugin didn't mark it) and the regex
    safety-net (which only walked dict values).
    """
    beets.config["mything"]["accounts"].set([{"token": "leakme"}])
    snap = build_config_snapshot(loaded_handle)
    assert "leakme" not in snap.effective_yaml
    assert "REDACTED" in snap.effective_yaml


def test_snapshot_does_not_mutate_live_config(loaded_handle: LibraryHandle) -> None:
    """Building the snapshot must leave ``beets.config`` byte-identical.

    ``flatten()`` copies the mapping levels but hands back the LIVE list objects
    for non-mapping views, so masking the flattened result in place used to
    overwrite a list-nested credential (e.g. ``kodiupdate.kodi[].pwd``) with the
    redaction tombstone in the running process — merely opening Settings
    destroyed the credential until the next Apply/restart.

    A top-level secret would NOT catch this (flatten rebuilds each mapping
    level), so the shape under test is deliberately list-nested. Asserting only
    on the rendered output — as the other redaction tests do — is exactly how
    this slipped through, so this one asserts on the LIVE config as well.
    """
    original = [{"host": "kodi.local", "user": "kodi", "pwd": "kodi-leak"}]
    beets.config["kodiupdate"]["kodi"].set(copy.deepcopy(original))

    snap = build_config_snapshot(loaded_handle)

    # The live config still holds the real credential, untouched.
    assert beets.config["kodiupdate"]["kodi"].get() == original
    # ...and the read-only view still redacts it.
    assert "kodi-leak" not in snap.effective_yaml
    assert "REDACTED" in snap.effective_yaml


def test_safety_net_masks_pwd_and_apisecret_variants(loaded_handle: LibraryHandle) -> None:
    """Real bundled plugins use ``pwd`` (kodiupdate) and ``apisecret`` (beatport).

    The previous anchored pattern missed both; this test pins the chosen
    permissive substring pattern so a future "tighten the regex" change can't
    silently regress coverage on these real keys.
    """
    beets.config["beatport"]["apisecret"].set("bp-leak")
    beets.config["kodiupdate"]["pwd"].set("kodi-leak")
    snap = build_config_snapshot(loaded_handle)
    assert "bp-leak" not in snap.effective_yaml
    assert "kodi-leak" not in snap.effective_yaml
