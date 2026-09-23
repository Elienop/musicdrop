"""Tests for ``build_config_snapshot`` and the redaction/apply-pending logic.

The shared autouse ``_clear_beets_globals`` fixture in ``tests/conftest.py``
resets ``beets.config`` and the plugin registry between every test, so each
case here starts from a clean confuse singleton.
"""

import copy
import datetime as dt
import hashlib
import os
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import beets
import confuse
import pytest
import yaml

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


def test_effective_yaml_redacts_per_view_flag(
    loaded_handle: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    beets.config["spotify"]["client_secret"].set("supersecret")
    monkeypatch.setattr(beets.config["spotify"]["client_secret"], "redact", True)
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
    has to catch it too — otherwise it escapes ``build_config_snapshot``.

    The sha is the file's own: the Save refuses such a file on the server."""
    raw = b"\xff\xfe not valid utf-8 \x80\x81"
    loaded_handle.config_path.write_bytes(raw)
    snap = build_config_snapshot(loaded_handle)  # must not raise
    assert (snap.yaml_text, snap.sha256) == ("", hashlib.sha256(raw).hexdigest())
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
    overwrite a list-nested credential with the redaction tombstone in the
    running process — merely opening Settings destroyed the credential until the
    next Apply/restart.

    The shape is the real ``kodi`` section (``beetsplug/kodiupdate.py`` registers
    ``kodi``, not ``kodiupdate``, and its default is a list of instances). A
    scalar under a mapping would NOT catch this — flatten rebuilds each mapping
    level, so only the list is shared:
    ``beets.config.flatten(redact=True)["kodi"] is beets.config["kodi"].get()``
    is True. Asserting only on the rendered output — as the other redaction
    tests do — is exactly how this slipped through, so this one asserts on the
    LIVE config as well.
    """
    original = [{"host": "kodi.local", "port": 8080, "user": "kodi", "pwd": "kodi-leak"}]
    beets.config["kodi"].set(copy.deepcopy(original))

    snap = build_config_snapshot(loaded_handle)

    # The live config still holds the real credential, untouched.
    assert beets.config["kodi"].get() == original
    # ...and the read-only view still redacts it.
    assert "kodi-leak" not in snap.effective_yaml
    assert "REDACTED" in snap.effective_yaml


def test_safety_net_masks_pwd_and_apisecret_variants(loaded_handle: LibraryHandle) -> None:
    """Both key names come from real bundled plugins, at their real paths.

    ``beetsplug/beatport.py`` marks ``apisecret`` (and ``apikey``) under the
    section ``beatport``; ``beetsplug/kodiupdate.py`` marks ``pwd`` under the
    section ``kodi``, whose value is a LIST of instances.

    The previous anchored pattern missed both; this test pins the chosen
    permissive substring pattern so a future "tighten the regex" change can't
    silently regress coverage on these real keys.
    """
    beets.config["beatport"]["apisecret"].set("bp-leak")
    beets.config["kodi"].set([{"host": "kodi.local", "pwd": "kodi-leak"}])
    snap = build_config_snapshot(loaded_handle)
    assert "bp-leak" not in snap.effective_yaml
    assert "kodi-leak" not in snap.effective_yaml


def test_safety_net_masks_a_key_named_key_or_ending_in_key(tmp_path: Path) -> None:
    """fetchart declares ``fanarttv_key`` secret only while it is LOADED; with
    fetchart off, the value sat in cleartext in the "secrets redacted" pane."""
    (tmp_path / "config.yaml").write_text(
        "library: library.db\n"
        "directory: music\n"
        "plugins: []\n"
        "fetchart:\n  fanarttv_key: fan-leak\n  google_key: goo-leak\n"
        "custom:\n  key: bare-leak\n  monkey: not-a-secret\n  keys_dir: /keys\n",
        encoding="utf-8",
    )
    handle = setup_beets(str(tmp_path))
    try:
        effective = yaml.safe_load(build_config_snapshot(handle).effective_yaml)
    finally:
        close_library(handle.lib)
    assert effective["fetchart"] == {"fanarttv_key": "REDACTED", "google_key": "REDACTED"}
    # The control: a whole-name suffix, not a substring.
    assert effective["custom"] == {"key": "REDACTED", "monkey": "not-a-secret", "keys_dir": "/keys"}


@pytest.mark.parametrize(
    ("label", "leaked"),
    [
        ("int", 4815162342),
        ("float", 1.5),
        ("bool", True),
        ("bytes", b"kodi-leak"),
        ("set", {"kodi-leak"}),
        ("str", "kodi-leak"),
    ],
)
def test_safety_net_masks_secret_of_any_scalar_type(
    loaded_handle: LibraryHandle, label: str, leaked: Any
) -> None:
    """A value under a secret-matching key is masked whatever type YAML parsed it as.

    The safety-net used to fire only on ``str`` leaves, so an UNQUOTED numeric
    password (``pwd: 4815162342`` — YAML gives an ``int``) rendered verbatim in
    the "Effective config" pane under a caption promising redacted secrets.
    ``float``/``bool``/``!!binary``/``!!set`` leaked the same way.

    Asserting on the PARSED document, not on a substring of the text: a
    ``bytes`` leak dumps as base64 (``!!binary a29kaS1sZWFr``), so a naive
    ``"kodi-leak" not in effective_yaml`` passes while the credential is right
    there in the pane.
    """
    beets.config["mything"]["pwd"].set(leaked)
    snap = build_config_snapshot(loaded_handle)
    parsed = yaml.safe_load(snap.effective_yaml)
    assert parsed["mything"]["pwd"] == "REDACTED", f"{label} secret survived redaction"


def test_safety_net_leaves_null_secret_as_null(loaded_handle: LibraryHandle) -> None:
    """A secret-matching key set to null must stay null, NOT become "REDACTED".

    It is a cost/benefit call: a null holds no credential, so masking it cannot
    prevent a leak, while the safety-net matches on KEY NAME alone and
    over-matches by design (``spotify.tokenfile`` is a filename) — so masking
    would make a pane captioned "with secrets redacted" claim a credential is
    configured on a field we only guessed was secret.

    Pass 1 disagrees: it renders a ``.redact``-marked null as "REDACTED", so a
    bundled plugin's unset credential never reaches this pass. See the
    ``_plain_redacted`` docstring for why that asymmetry is the safe one.

    Note this is the ONE input on which the two passes disagree: confuse's own
    pass 1 renders a null ``.redact`` field as "REDACTED"
    (``config["x"]["api_key"].set(None)`` + ``.redact = True`` flattens to
    ``"REDACTED"``). That is upstream behaviour, and the disagreement is in the
    safe direction — pass 2 reveals a null, never a value.
    """
    beets.config["mything"]["password"].set(None)
    snap = build_config_snapshot(loaded_handle)
    parsed = yaml.safe_load(snap.effective_yaml)
    assert parsed["mything"]["password"] is None


def test_confuse_pass1_ignores_redact_on_a_list_shaped_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the upstream fact the safety-net exists for (``beetsplug/kodiupdate.py``).

    The plugin registers the section ``kodi`` and adds a LIST as its default,
    then marks ``pwd.redact = True``. Because the view is not a mapping,
    ``View.flatten`` falls back to ``view.get()`` and the flag is ignored — so
    pass 1 hands the credential through in cleartext, AND hands back the live
    list. If a future confuse/beets makes pass 1 honour this, that is a real
    change to why ``_plain_redacted`` is load-bearing, and this test says so.
    """
    beets.config["kodi"].set([{"host": "kodi.local", "pwd": 4815162342}])
    monkeypatch.setattr(beets.config["kodi"]["pwd"], "redact", True)

    with pytest.raises(confuse.ConfigTypeError):
        beets.config["kodi"].flatten(redact=True)

    flat = beets.config.flatten(redact=True)
    assert flat["kodi"] == [{"host": "kodi.local", "pwd": 4815162342}]  # unredacted
    assert flat["kodi"] is beets.config["kodi"].get()  # ...and the LIVE list


def test_confuse_pass1_masks_a_null_redact_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the asymmetry documented on the null carve-out.

    ``_plain_redacted`` leaves a null under a secret-matching key alone, and its
    docstring justifies that while acknowledging pass 1 does the opposite. This
    asserts the "opposite" half so the justification can't quietly go stale.
    """
    beets.config["mything"]["api_key"].set(None)
    monkeypatch.setattr(beets.config["mything"]["api_key"], "redact", True)

    assert beets.config.flatten(redact=True)["mything"]["api_key"] == "REDACTED"


def test_non_string_mapping_key_does_not_raise(loaded_handle: LibraryHandle) -> None:
    """A non-``str`` YAML key with a ``str`` value under it must not 500 Settings.

    The key guard was ``key is not None``, so ``pattern.search(key)`` got handed
    an ``int``/``bool`` and raised ``TypeError: expected string or bytes-like
    object``. Real configs hit this: ``substitute: {112: One Twelve}`` (112, 311
    and 702 are band names) or ``types: {no: int}``, where YAML 1.1 resolves the
    bare key ``no`` to boolean ``False``. The result was a permanent HTTP 500 on
    the page that is both the default landing page and the only in-app way to
    edit the config that causes it.

    The value under each key MUST be a ``str`` — with an ``int`` value the old
    code short-circuited on ``isinstance(value, str)`` and never reached the
    regex, so an int-valued case would pass against the bug and prove nothing.
    """
    beets.config["substitute"].set(
        {
            112: "One Twelve",  # int key
            False: "int",  # bool key (YAML 1.1 `no:`)
            1.5: "one and a half",  # float key
            dt.date(1996, 1, 1): "a date",  # date key
        }
    )

    snap = build_config_snapshot(loaded_handle)  # must not raise

    # The dump is still valid YAML and round-trips every exotic key type.
    parsed = yaml.safe_load(snap.effective_yaml)
    assert parsed["substitute"] == {
        112: "One Twelve",
        False: "int",
        1.5: "one and a half",
        dt.date(1996, 1, 1): "a date",
    }


def test_safety_net_leaves_bare_list_of_strings_alone(
    loaded_handle: LibraryHandle,
) -> None:
    """A list does NOT propagate its own key to its items — deliberate, pinned.

    ``passwords: ["a", "b"]`` stays intact because list items are recursed with
    ``key=None``. Broadening the leaf rule to non-``str`` values must not quietly
    drag this along with it.
    """
    beets.config["mything"]["passwords"].set(["alpha", "beta"])
    snap = build_config_snapshot(loaded_handle)
    parsed = yaml.safe_load(snap.effective_yaml)
    assert parsed["mything"]["passwords"] == ["alpha", "beta"]
