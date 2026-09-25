"""The Keep downloads switch: ``GET``/``POST /api/config/import-operation`` (design S5).

The switch writes beets' own ``import:`` keys into config.yaml and reloads beets
the way Apply does, in one request. Every test drives the real route over a
real config.yaml, then asks beets itself what it would run: the parity test in
``test_import_operation.py`` pins our reading of the flags to beets'
``ImportSession.set_config``, and ``_beets_operation`` here is that same
reading, so a beets bump that changes either fails.
"""

from __future__ import annotations

import os
from typing import Any, cast

import pytest
from beets import config
from beets.importer.session import ImportSession
from beets.library import Library
from fastapi.testclient import TestClient

from app.beets.config_editor import (
    APPLY_FIRST,
    IMPORT_NOT_EDITABLE,
    INCLUDE_DECIDES,
    JOB_RUNNING,
    parse_yaml,
)
from app.beets.import_operation import configured_file_operation
from app.beets.library import LibraryHandle
from tests.test_import_operation import _beets_operation

_ROUTE = "/api/config/import-operation"


def _live() -> LibraryHandle:
    """The handle serving now: every Apply and flip swaps in a new one."""
    from app.main import app

    handle: LibraryHandle = app.state.beets_library
    return handle


def _music(handle: LibraryHandle) -> str:
    return os.fsdecode(handle.lib.directory)


def _text(handle: LibraryHandle, import_block: str | None, extra: str = "") -> str:
    """A config.yaml with comments around ``import:``; ``None`` leaves ``import:`` out."""
    head = (
        f"directory: {_music(handle)}   # the library\n"
        "library: library.db\n"
        "plugins:\n  - musicbrainz\n"
        f"{extra}"
    )
    if import_block is None:
        return head
    return f"{head}# how imports file\nimport:{import_block}"


def _load(client: TestClient, handle: LibraryHandle, text: str) -> str:
    """Write ``text`` as config.yaml and Apply it; the sha a flip then sends."""
    handle.config_path.write_text(text, encoding="utf-8")
    assert client.post("/api/config/apply").status_code == 200
    snapshot = client.get("/api/config").json()
    assert snapshot["apply_pending"] is False
    return str(snapshot["sha256"])


def _flip(client: TestClient, sha: str, *, on: bool) -> Any:
    return client.post(_ROUTE, json={"keep_downloads": on, "base_sha256": sha})


def _operation(client: TestClient) -> str:
    r = client.get(_ROUTE)
    assert r.status_code == 200
    return str(r.json()["operation"])


def _beets_would_run() -> str:
    """What beets' own ``set_config`` resolves the live config to."""
    session = ImportSession(cast(Library, object()), None, [], None)
    session.set_config(config["import"])
    return _beets_operation(config["import"])


#: Every starting shape of the one file-operation line, between two other keys.
_SHAPES: dict[str, str | None] = {
    "no key": None,
    "move": "move: yes",
    "copy": "copy: yes",
    "hardlink": "hardlink: yes",
    "link": "link: yes",
    "reflink": "reflink: yes",
    "reflink auto": "reflink: auto",
}

#: ``import:``'s keys after the flip, in order. A new key goes before the first
#: file-operation key, or first; ``copy`` is never touched.
_ON: dict[str, list[tuple[str, object]]] = {
    "no key": [("hardlink", True), ("autotag", True), ("write", True)],
    "move": [("autotag", True), ("hardlink", True), ("move", False), ("write", True)],
    "copy": [("autotag", True), ("hardlink", True), ("copy", True), ("write", True)],
    "hardlink": [("autotag", True), ("hardlink", True), ("write", True)],
    "link": [("autotag", True), ("hardlink", True), ("link", False), ("write", True)],
    "reflink": [("autotag", True), ("hardlink", True), ("reflink", False), ("write", True)],
    "reflink auto": [("autotag", True), ("hardlink", True), ("reflink", False), ("write", True)],
}
_OFF: dict[str, list[tuple[str, object]]] = {
    "no key": [("move", True), ("autotag", True), ("write", True)],
    "move": [("autotag", True), ("move", True), ("write", True)],
    "copy": [("autotag", True), ("move", True), ("copy", True), ("write", True)],
    "hardlink": [("autotag", True), ("move", True), ("hardlink", False), ("write", True)],
    "link": [("autotag", True), ("move", True), ("link", False), ("write", True)],
    "reflink": [("autotag", True), ("move", True), ("reflink", False), ("write", True)],
    "reflink auto": [("autotag", True), ("move", True), ("reflink", False), ("write", True)],
}


@pytest.mark.parametrize("on", [True, False], ids=["on", "off"])
@pytest.mark.parametrize("shape", list(_SHAPES))
def test_the_switch_writes_only_its_keys_and_beets_agrees(
    client: TestClient, beets_library: LibraryHandle, shape: str, on: bool
) -> None:

    line = _SHAPES[shape]
    op_line = "" if line is None else f"\n  {line}   # the user's line"
    block = f"\n  autotag: yes   # review{op_line}\n  write: yes\n"
    sha = _load(client, beets_library, _text(beets_library, block))
    before = parse_yaml(beets_library.config_path.read_text(encoding="utf-8"))

    r = _flip(client, sha, on=on)
    assert r.status_code == 200, r.text
    assert r.json()["apply_pending"] is False

    text = beets_library.config_path.read_text(encoding="utf-8")
    after = parse_yaml(text)
    assert list(after["import"].items()) == (_ON if on else _OFF)[shape]
    for key in ("directory", "library", "plugins"):
        assert after[key] == before[key]
    for comment in ("# the library", "# how imports file", "# review"):
        assert comment in text
    if line is not None:
        assert "# the user's line" in text

    target = "hardlink" if on else "move"
    assert _operation(client) == target
    assert _live().file_operation == target
    assert _beets_would_run() == target


def test_what_imports_use_is_read_with_every_runs_overlay(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """``copy`` + ``delete``: beets alone would move; every in-app run pins
    ``delete`` off, so imports copy, and the switch says so."""
    _load(client, beets_library, _text(beets_library, "\n  copy: yes\n  delete: yes\n"))
    assert _operation(client) == "copy"
    # The control: the live config without the overlay reads as a move.
    assert configured_file_operation() == "move"


def test_a_config_with_no_import_section_gets_one(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """The ACCEPT side of the ``import:`` refusal: absent is not refused."""
    from app.main import app

    sha = _load(client, beets_library, _text(beets_library, None))
    old = app.state.beets_library
    r = _flip(client, sha, on=True)
    assert r.status_code == 200, r.text
    text = beets_library.config_path.read_text(encoding="utf-8")
    assert text.endswith("import:\n  hardlink: true\n")
    assert app.state.beets_library is not old  # reloaded, as Apply does
    assert _operation(client) == "hardlink"


def _refused(client: TestClient, handle: LibraryHandle, sha: str, *, on: bool, status: int) -> Any:
    before = handle.config_path.read_bytes()
    r = _flip(client, sha, on=on)
    assert r.status_code == status, r.text
    assert handle.config_path.read_bytes() == before
    return r.json()["detail"]


def test_a_running_job_refuses(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.import_jobs.registry import get_registry

    sha = _load(client, beets_library, _text(beets_library, "\n  move: yes\n"))
    monkeypatch.setattr(get_registry(), "has_active_job", lambda: True)
    assert _refused(client, beets_library, sha, on=True, status=409) == JOB_RUNNING
    assert _operation(client) == "move"


def test_saved_edits_apply_has_not_loaded_refuse(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """The flip's reload would load them unseen; the sha is current, so only
    the pending check can refuse this."""
    _load(client, beets_library, _text(beets_library, "\n  move: yes\n"))
    later = _live().file_mtime_at_load + 5
    os.utime(beets_library.config_path, (later, later))
    sha = client.get("/api/config").json()["sha256"]
    assert _refused(client, beets_library, sha, on=True, status=409) == APPLY_FIRST


def test_a_changed_file_refuses_with_the_saves_conflict_body(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    sha = _load(client, beets_library, _text(beets_library, "\n  move: yes\n"))
    detail = _refused(client, beets_library, "0" * 64, on=True, status=409)
    assert detail == {
        "detail": "File changed on disk",
        "current_yaml_text": beets_library.config_path.read_text(encoding="utf-8"),
        "current_sha256": sha,
    }


@pytest.mark.parametrize(
    "block",
    [" &shared\n  move: yes\nother: *shared\n", "\n  <<: {move: yes}\n"],
    ids=["anchored", "merge"],
)
def test_an_import_section_shared_with_another_key_refuses(
    client: TestClient, beets_library: LibraryHandle, block: str
) -> None:
    sha = _load(client, beets_library, _text(beets_library, block))
    assert _refused(client, beets_library, sha, on=True, status=422) == IMPORT_NOT_EDITABLE


def test_an_import_section_merged_in_from_another_key_refuses(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """A top-level ``<<`` supplies ``import:``: the write would land in ``base:``."""
    extra = "base: &base\n  import:\n    move: yes\n<<: *base\n"
    sha = _load(client, beets_library, _text(beets_library, None, extra))
    assert _operation(client) == "move"  # beets reads the merged key
    assert _refused(client, beets_library, sha, on=True, status=422) == IMPORT_NOT_EDITABLE


@pytest.mark.parametrize("block", [" 3\n", "\n"], ids=["scalar", "null"])
def test_an_import_section_that_is_not_a_mapping_refuses(
    client: TestClient, beets_library: LibraryHandle, block: str
) -> None:
    """beets loads either and fails only when an import reads ``import:``, so
    Apply loads it too, and both routes name the key instead."""
    sha = _load(client, beets_library, _text(beets_library, block))
    r = client.get(_ROUTE)
    assert r.status_code == 422
    assert r.json()["detail"] == IMPORT_NOT_EDITABLE
    assert _refused(client, beets_library, sha, on=True, status=422) == IMPORT_NOT_EDITABLE


def test_an_alias_refuses(client: TestClient, beets_library: LibraryHandle) -> None:
    """``import: *x``: writing through it would change ``x`` too."""
    extra = "base: &x\n  move: yes\n"
    sha = _load(client, beets_library, _text(beets_library, " *x\n", extra))
    assert _refused(client, beets_library, sha, on=True, status=422) == IMPORT_NOT_EDITABLE


def test_an_include_that_decides_the_operation_refuses(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """Includes outrank config.yaml, so ``hardlink: yes`` here would not run."""
    (beets_library.beets_dir / "extra.yaml").write_text("import:\n  move: yes\n")
    extra = "include:\n  - extra.yaml\n"
    sha = _load(client, beets_library, _text(beets_library, None, extra))
    assert _refused(client, beets_library, sha, on=True, status=422) == INCLUDE_DECIDES
    # The control: the same include agrees with off, which is accepted.
    r = _flip(client, sha, on=False)
    assert r.status_code == 200, r.text
    assert _operation(client) == "move"


def test_an_include_whose_import_is_not_a_mapping_refuses(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """The Beets Save's check reads the merged ``import:`` first and names the
    include, so the operation read after it never meets the bad value."""
    (beets_library.beets_dir / "extra.yaml").write_text("import: 3\n")
    extra = "include:\n  - extra.yaml\n"
    sha = _load(client, beets_library, _text(beets_library, "\n  move: yes\n", extra))
    detail = _refused(client, beets_library, sha, on=True, status=422)
    assert detail == (
        "import must be a collection, not int (in extra.yaml). Check it in Settings → Beets."
    )


def test_a_text_the_beets_save_would_refuse_is_not_written(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """The Beets Save's check runs on the text about to be written. beets loads
    ``write: 'no'`` (a bare ``if`` reads the string as true) and the check
    refuses it, so the flip names that key rather than writing around it."""
    sha = _load(client, beets_library, _text(beets_library, "\n  write: 'no'\n"))
    detail = _refused(client, beets_library, sha, on=True, status=422)
    assert isinstance(detail, str)
    assert detail.startswith("import.write: ")
    assert detail.endswith(". Check it in Settings → Beets.")
