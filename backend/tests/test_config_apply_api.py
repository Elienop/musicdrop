"""End-to-end tests for ``POST /api/config/apply``.

Covers the one-click in-process reload: the handler holds an ``asyncio.Lock``
on ``app.state.beets_swap_lock`` and offloads the blocking read and rebuild
(``read_beets_config``, then ``reset_beets_globals`` + ``open_beets``) to
FastAPI's threadpool, then
atomically swaps ``app.state.beets_library`` with the new handle. The 409
import-gate check uses ``get_registry()`` (the live binding — the autouse
``reset_import_registry`` fixture in ``conftest.py`` swaps the module global
between tests, so the handler must NOT import the name eagerly).

A rebuild that fails after the teardown puts the old config back and answers
422 (owner ruling 2026-09-23); the 500 is left for a restore that fails too.
One 500 test monkeypatches ``app.beets.config_editor.open_beets`` (the name the
handler captured at import time), so the rebuild and the restore both raise.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest
from beets import plugins
from beets.library import Item
from fastapi.testclient import TestClient

from app.beets.library import LibraryHandle


def test_apply_returns_snapshot_with_apply_pending_false(
    client: TestClient, beets_library_config_path: Path
) -> None:
    # Touch the file so apply_pending becomes true; the post-Apply snapshot
    # must report apply_pending=False because read_beets_config() captured the
    # bumped mtime as the new baseline.
    time.sleep(0.01)
    new_ts = time.time() + 1
    os.utime(beets_library_config_path, (new_ts, new_ts))
    snap_before = client.get("/api/config").json()
    assert snap_before["apply_pending"] is True

    r = client.post("/api/config/apply")
    assert r.status_code == 200
    assert r.json()["apply_pending"] is False


def test_apply_reattaches_new_lib_to_import_registry(
    client: TestClient, beets_library_config_path: Path
) -> None:
    """Apply swaps ``app.state.beets_library`` for a freshly-rebuilt handle; the
    import registry must follow. The lifespan attaches the lib once (main.py) and
    the registry's runner captures it at construction, so without a re-attach every
    later import (manual, inbox webhook, bank-apply) silently runs against the
    pre-Apply Library — old ``directory`` / ``path_formats`` / DB path, i.e. files
    placed per the stale config and, if ``library:`` changed, a split DB."""
    from app.import_jobs.registry import get_registry
    from app.main import app

    old_lib = app.state.beets_library.lib
    get_registry().attach_library(old_lib)  # mirror the lifespan wiring
    assert get_registry()._lib is old_lib

    r = client.post("/api/config/apply")
    assert r.status_code == 200

    new_lib = app.state.beets_library.lib
    assert new_lib is not old_lib  # the rebuild produced a fresh Library
    assert get_registry()._lib is new_lib  # registry re-attached to it, not the stale old lib


def test_apply_409_when_import_active(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.import_jobs.registry import get_registry

    monkeypatch.setattr(get_registry(), "has_active_job", lambda: True)
    r = client.post("/api/config/apply")
    assert r.status_code == 409
    assert "import" in r.json()["detail"].lower()


def test_apply_500_when_open_beets_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both loads raise: the rebuild, then the restore of the old config."""
    from app.beets import config_editor

    calls: list[object] = []

    def _boom(read: object) -> object:
        calls.append(read)
        raise RuntimeError("boom")

    monkeypatch.setattr(config_editor, "open_beets", _boom)
    r = client.post("/api/config/apply")
    assert len(calls) == 2
    assert r.status_code == 500
    # Pin the response shape. Starlette already wraps our payload as
    # ``{"detail": ...}`` once — so the handler's payload must be a flat dict
    # with ``message`` + ``recovery`` keys, NOT a nested ``{"detail": ..., "recovery": ...}``
    # (which would render as the double-nested ``{"detail": {"detail": ...}}``
    # the FE then has to special-case). The 409 sibling uses a flat ``str``
    # for ``detail``; this 500 payload is the structured form of the same
    # convention.
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    # Inner key is ``message`` (NOT ``detail``) so the response body is not
    # ``{"detail": {"detail": "..."}}`` — the FE would have to special-case
    # that double-``detail`` shape, and the 409 sibling uses a flat
    # ``detail: str``. ``message`` lines up with the structured-error
    # convention every other 500 follow.
    assert detail == {
        "message": "Apply failed during rebuild: boom",
        "recovery": rebuild_failed("boom"),
    }


def config_rejected(cause: str) -> str:
    """Apply's 500 recovery for a config error, copied by hand so a wording change is made twice."""
    return (
        f"beets rejected a value in the config: {cause}. Fix it and Apply again;"
        " MusicDrop will not start until you do."
    )


def rebuild_failed(cause: str) -> str:
    """Apply's 500 recovery for any other rebuild failure."""
    return f"Apply stopped partway: {cause}. Fix that and Apply again."


def restored_config_rejected(cause: str) -> dict[str, str]:
    """Apply's 422 body when a config error was put back, copied by hand."""
    return {
        "message": f"Apply failed and put the old config back: {cause}",
        "recovery": (
            f"beets rejected a value in the config: {cause}, so nothing was changed."
            " Fix it and Apply again; MusicDrop will not start until you do."
        ),
    }


def restored_rebuild_failed(cause: str) -> dict[str, str]:
    """Apply's 422 body when any other rebuild failure was put back."""
    return {
        "message": f"Apply failed and put the old config back: {cause}",
        "recovery": f"Apply stopped: {cause}, so nothing was changed. Fix that and Apply again.",
    }


@pytest.mark.parametrize(
    ("exc", "cause"),
    [(RuntimeError(), "RuntimeError"), (OSError("disk full."), "disk full")],
    ids=["empty-message", "trailing-period"],
)
def test_the_rebuild_recovery_always_names_a_cause(exc: Exception, cause: str) -> None:
    """An empty message names the class; a trailing period is not doubled."""
    from app.beets import config_editor

    assert config_editor._rebuild_recovery(exc) == rebuild_failed(cause)


class _Before(NamedTuple):
    """What a refused Apply must leave exactly as it was."""

    handle: LibraryHandle
    item: Item
    plugins: list[str]
    destination: bytes


def _live_state(client: TestClient, beets_library: LibraryHandle) -> _Before:
    """Load a config whose ``the`` plugin shows in ``destination()``, and record it.

    Measured before the read moved ahead of the teardown: plugins were cleared,
    the read failed, and writes went on with plugin-less templates —
    ``%the{$artist}`` came out literally in the destination.
    """
    from app.main import app

    music = Path(beets_library.lib.directory.decode())
    beets_library.config_path.write_text(
        f"directory: {music}\n"
        "library: library.db\n"
        "plugins:\n  - musicbrainz\n  - the\n"
        "paths:\n  singleton: '%the{$artist}/$title'\n",
        encoding="utf-8",
    )
    assert client.post("/api/config/apply").status_code == 200
    handle = app.state.beets_library
    item = Item(artist="The Beatles", title="Come Together", path=b"/nowhere.mp3")
    handle.lib.add(item)
    before = _Before(
        handle, item, sorted(p.name for p in plugins.find_plugins()), item.destination()
    )
    assert b"Beatles, The" in before.destination  # the control: ``the`` is live
    return before


def _assert_unchanged(client: TestClient, before: _Before) -> None:
    from app.main import app

    assert app.state.beets_library is before.handle
    assert sorted(p.name for p in plugins.find_plugins()) == before.plugins
    assert before.item.destination() == before.destination
    assert client.get("/api/albums").status_code == 200


def _assert_restored(client: TestClient, before: _Before) -> None:
    """The old config loaded again, on a fresh handle everything now holds."""
    from app.import_jobs.registry import get_registry
    from app.main import app

    restored: LibraryHandle = app.state.beets_library
    assert restored is not before.handle
    assert get_registry()._lib is restored.lib
    assert sorted(p.name for p in plugins.find_plugins()) == before.plugins
    assert before.item.id is not None
    item = restored.lib.get_item(before.item.id)
    assert item is not None
    assert item.destination() == before.destination
    assert client.get("/api/albums").status_code == 200


def test_apply_of_an_unparseable_config_changes_nothing(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """A config.yaml beets cannot parse leaves the running process as it was.

    The recovery names the line PyYAML reports (its ``problem_mark``, 1-based):
    the unclosed ``[`` runs to the end of the file, line 2.
    """
    before = _live_state(client, beets_library)
    cfg = beets_library.config_path
    cfg.write_text("directory: [unclosed\n", encoding="utf-8")

    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {
        "message": (
            f"Apply refused: {cfg} could not be read: while parsing a flow sequence\n"
            f'  in "{cfg}", line 1, column 12\n'
            "expected ',' or ']', but got '<stream end>'\n"
            f'  in "{cfg}", line 2, column 1'
        ),
        "recovery": (
            "beets could not read config.yaml (line 2), so nothing was changed."
            " Fix the file and Apply again."
        ),
    }
    _assert_unchanged(client, before)


def test_an_unreadable_config_with_no_line_keeps_the_plain_recovery(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """An integer over CPython's 4300-digit limit: a ``ValueError``, no YAML mark."""
    before = _live_state(client, beets_library)
    cfg = beets_library.config_path
    cfg.write_text(f"library: library.db\ndirectory: {'9' * 5000}\n", encoding="utf-8")

    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {
        "message": (
            f"Apply refused: {cfg} could not be read: ValueError: Exceeds the limit"
            " (4300 digits) for integer string conversion: value has 5000 digits; use"
            " sys.set_int_max_str_digits() to increase the limit"
        ),
        "recovery": (
            "beets could not read config.yaml, so nothing was changed."
            " Fix the file and Apply again."
        ),
    }
    _assert_unchanged(client, before)


@pytest.mark.parametrize("shape", ["absent", "directory", "fifo", "dangling-link"])
def test_apply_of_a_missing_config_changes_nothing_and_writes_no_starter(
    client: TestClient, beets_library: LibraryHandle, shape: str
) -> None:
    """The starter is the boot's; Apply refuses instead.

    Apply wrote the starter without the image's ``/music`` override, so in the
    container it loaded ``directory: ../music``, which is inside the volume.
    Every shape ``os.path.isfile`` answers False for gets the same sentence.
    """
    before = _live_state(client, beets_library)
    cfg = beets_library.config_path
    cfg.unlink()
    if shape == "directory":
        cfg.mkdir()
    elif shape == "fifo":
        os.mkfifo(cfg)
    elif shape == "dangling-link":
        cfg.symlink_to(cfg.parent / "gone.yaml")

    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {
        "message": f"Apply refused: {cfg} is missing or is not a regular file",
        "recovery": (
            "MusicDrop found no regular file at config.yaml, so nothing was changed."
            " Restore the file and Apply again."
        ),
    }
    assert os.path.lexists(cfg) is (shape != "absent")
    _assert_unchanged(client, before)


def test_apply_of_a_config_whose_include_beets_would_skip_changes_nothing(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """beets writes one stderr line for an include it cannot read and loads without it.

    ``beets/__init__.py:37-38`` in the installed beets 2.14.0. Owner ruling
    2026-09-21: refuse, change nothing. The control is the same document with
    the include present, which Apply loads.
    """
    before = _live_state(client, beets_library)
    cfg = beets_library.config_path
    music = Path(beets_library.lib.directory.decode())
    overlay = beets_library.beets_dir / "overlay.yaml"
    cfg.write_text(
        f"directory: {music}\nlibrary: library.db\nplugins:\n  - musicbrainz\n  - the\n"
        "paths:\n  singleton: '%the{$artist}/$title'\ninclude:\n  - overlay.yaml\n",
        encoding="utf-8",
    )

    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {
        "message": (
            "Apply refused: beets would skip the include 'overlay.yaml': No such file or directory"
        ),
        "recovery": (
            "beets could not read the include 'overlay.yaml' (No such file or directory),"
            " so nothing was changed. Fix it and Apply again."
        ),
    }
    _assert_unchanged(client, before)

    overlay.write_text("ui:\n  color: no\n", encoding="utf-8")
    assert client.post("/api/config/apply").status_code == 200


@pytest.mark.parametrize(
    ("shape", "reason"),
    [
        ("yaml", "YAML error at line 3"),
        pytest.param(
            "permission",
            "Permission denied",
            marks=pytest.mark.skipif(
                os.geteuid() == 0, reason="root ignores the permission bits this test sets"
            ),
        ),
    ],
)
def test_a_skipped_include_names_why_at_apply(
    client: TestClient, beets_library: LibraryHandle, shape: str, reason: str
) -> None:
    """Owner ruling 2026-09-21: a YAML error in the refusal carries its line number."""
    before = _live_state(client, beets_library)
    music = Path(beets_library.lib.directory.decode())
    bad = beets_library.beets_dir / "bad.yaml"
    bad.write_text("a: 1\nfoo: [unclosed\n", encoding="utf-8")
    if shape == "permission":
        bad.chmod(0)
    beets_library.config_path.write_text(
        f"directory: {music}\nlibrary: library.db\ninclude:\n  - bad.yaml\n", encoding="utf-8"
    )

    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {
        "message": f"Apply refused: beets would skip the include 'bad.yaml': {reason}",
        "recovery": (
            f"beets could not read the include 'bad.yaml' ({reason}), so nothing was changed."
            " Fix it and Apply again."
        ),
    }
    _assert_unchanged(client, before)


def test_a_skipped_include_is_named_escaped_at_apply(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """Named raw before: a newline and an ESC reached the 422's message and recovery."""
    before = _live_state(client, beets_library)
    music = Path(beets_library.lib.directory.decode())
    beets_library.config_path.write_text(
        f'directory: {music}\nlibrary: library.db\ninclude:\n  - "m\\nFAKE \\e[31mx.yaml"\n',
        encoding="utf-8",
    )

    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    shown = "'m\\nFAKE \\x1b[31mx.yaml'"
    assert r.json()["detail"] == {
        "message": (
            f"Apply refused: beets would skip the include {shown}: No such file or directory"
        ),
        "recovery": (
            f"beets could not read the include {shown} (No such file or directory),"
            " so nothing was changed. Fix it and Apply again."
        ),
    }
    _assert_unchanged(client, before)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("a: 1\nkey: *Hunter2Alias\n", "YAML error at line 2"),
        ("a: 1\nkey: !Hunter2Tag x\n", "YAML error at line 2"),
        ("a: 1\nkey: !Hunter2Handle!x y\n", "YAML error at line 2"),
    ],
    ids=["undefined-alias", "unknown-tag", "undefined-tag-handle"],
)
def test_a_skipped_include_quotes_nothing_from_inside_it(
    client: TestClient, beets_library: LibraryHandle, text: str, reason: str
) -> None:
    """PyYAML's problem text quoted the token, which can be an unquoted secret."""
    before = _live_state(client, beets_library)
    music = Path(beets_library.lib.directory.decode())
    (beets_library.beets_dir / "secret.yaml").write_text(text, encoding="utf-8")
    beets_library.config_path.write_text(
        f"directory: {music}\nlibrary: library.db\ninclude:\n  - secret.yaml\n",
        encoding="utf-8",
    )

    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {
        "message": f"Apply refused: beets would skip the include 'secret.yaml': {reason}",
        "recovery": (
            f"beets could not read the include 'secret.yaml' ({reason}), so nothing was changed."
            " Fix it and Apply again."
        ),
    }
    _assert_unchanged(client, before)


def _nul_path_error() -> str:
    """CPython's own words for a NUL in a path, from the ``os.open`` the gate calls.

    They changed within 3.12: 3.12.13 says "open: embedded null character in
    path", and CI's 3.12.3 said "embedded null byte" (run 35891156419).
    """
    try:
        os.open("nul\0.yaml", os.O_RDONLY)
    except ValueError as exc:
        return str(exc)
    raise AssertionError("os.open accepted a NUL in a path")


@pytest.mark.parametrize(
    ("include", "detail"),
    [
        (
            "\n  - good.yaml\n  - list.yaml\n",
            "'list.yaml': YAML config must be a mapping, got <class 'list'>",
        ),
        ('\n  - "nul\\0.yaml"\n', f"'nul\\x00.yaml': {_nul_path_error()}"),
        (" good.yaml\n", "include must be a list, not str"),
        # Entry 1 resolves after nested.yaml is merged, so it is nested.yaml's own.
        ("\n  - nested.yaml\n  - good.yaml\n", "include#1: must be a filename, not OrderedDict"),
    ],
    ids=["not-a-mapping", "nul", "not-a-list", "nested-mapping"],
)
def test_an_include_refusal_names_the_entry_as_written(
    client: TestClient, beets_library: LibraryHandle, include: str, detail: str
) -> None:
    """Measured before: the first two named no entry. ``include:`` itself has none.

    A mapping is never quoted: round 8's text carried its repr, password and all.
    """
    before = _live_state(client, beets_library)
    music = Path(beets_library.lib.directory.decode())
    (beets_library.beets_dir / "good.yaml").write_text("a: 1\n", encoding="utf-8")
    (beets_library.beets_dir / "list.yaml").write_text("- a\n", encoding="utf-8")
    (beets_library.beets_dir / "nested.yaml").write_text(
        "include:\n  - x.yaml\n  - {password: Hunter2Nested}\n", encoding="utf-8"
    )
    beets_library.config_path.write_text(
        f"directory: {music}\nlibrary: library.db\ninclude:{include}", encoding="utf-8"
    )

    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {
        "message": "Apply refused: `include:` in config.yaml could not be read",
        "recovery": (
            f"`include:` in config.yaml could not be read: {detail}. Fix the include: list."
        ),
    }
    _assert_unchanged(client, before)


def test_apply_refuses_the_layout_beets_parses_not_the_one_ruamel_refuses_to(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """Duplicate ``directory:`` keys: ruamel raises, PyYAML keeps the LAST value.

    The pre-check parsed with ruamel and was silent on its error, so Apply tore
    down, loaded a music root that IS the beets dir, and only the post-load
    backstop refused — after the swap. Parsed the way beets parses, it is
    refused before anything changes.
    """
    before = _live_state(client, beets_library)
    beets_library.config_path.write_text(
        f"directory: {beets_library.beets_dir.parent}\n"
        "library: library.db\n"
        f"directory: {beets_library.beets_dir}\n",
        encoding="utf-8",
    )

    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message"] == (
        "Apply refused: The beets data directory is the music library"
    )
    _assert_unchanged(client, before)


def test_a_file_that_breaks_after_the_gate_is_refused_by_the_read(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate and beets' read are two reads of one file; the second one refuses too.

    The edit is made just before the real read runs, so the gate saw a good file.
    The refusal names the line of the file beets actually read.
    """
    from app.beets import config_editor
    from app.beets.setup import read_beets_config as real_read

    before = _live_state(client, beets_library)
    cfg = beets_library.config_path

    def _broken_after_the_gate(beets_dir: str) -> object:
        cfg.write_text("a: 1\nb: [unclosed\n", encoding="utf-8")
        return real_read(beets_dir)

    monkeypatch.setattr(config_editor, "read_beets_config", _broken_after_the_gate)

    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert r.json()["detail"]["recovery"] == (
        "beets could not read config.yaml (line 3), so nothing was changed."
        " Fix the file and Apply again."
    )
    _assert_unchanged(client, before)


@pytest.mark.parametrize(
    ("bad", "cause"),
    [
        ("directory: 5\nlibrary: library.db\n", "directory: must be a filename, not int"),
        ("directory: [a]\nlibrary: library.db\n", "directory: must be a filename, not list"),
        (
            "directory: {a: 1}\nlibrary: library.db\n",
            "directory: must be a filename, not OrderedDict",
        ),
        (
            "DIR\nlibrary: library.db\nplugins: 5\n",
            "plugins: must be a whitespace-separated string or a list",
        ),
        (
            "DIR\nlibrary: library.db\nplugins:\n  - musicbrainz\nmusicbrainz: no\n",
            "musicbrainz must be a dict, not bool",
        ),
    ],
    ids=["dir-int", "dir-list", "dir-map", "plugins-int", "musicbrainz-no"],
)
def test_a_value_beets_rejects_after_the_teardown_is_fixed_by_a_second_apply(
    client: TestClient, beets_library: LibraryHandle, bad: str, cause: str
) -> None:
    """What the 422's recovery tells the operator to do, done.

    The file gate passes these and the rebuild's typed read raises, so the old
    config is put back. Boot refuses the same files
    (``tests/test_config_boot.py``), so the recovery does not offer a restart.
    The page prints only the recovery, so it quotes beets.
    """
    from app.main import app

    music = Path(beets_library.lib.directory.decode())
    cfg = beets_library.config_path
    cfg.write_text(bad.replace("DIR", f"directory: {music}"), encoding="utf-8")

    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == restored_config_rejected(cause)

    cfg.write_text(
        f"directory: {music}\nlibrary: library.db\nplugins:\n  - the\n", encoding="utf-8"
    )
    assert client.post("/api/config/apply").status_code == 200
    assert [p.name for p in plugins.find_plugins()] == ["the"]
    assert app.state.beets_library.lib.directory == os.fsencode(music)
    assert client.get("/api/albums").status_code == 200


def test_a_library_beets_cannot_open_puts_the_old_config_back(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """The other arm: not a config error, and boot refuses this file too."""
    before = _live_state(client, beets_library)
    music = Path(beets_library.lib.directory.decode())
    cfg = beets_library.config_path
    cfg.write_text(f"directory: {music}\nlibrary: /nonexistent-md/x/lib.db\n", encoding="utf-8")

    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == restored_rebuild_failed("unable to open database file")
    _assert_restored(client, before)

    cfg.write_text(f"directory: {music}\nlibrary: library.db\n", encoding="utf-8")
    assert client.post("/api/config/apply").status_code == 200
    assert client.get("/api/albums").status_code == 200


def test_a_value_beets_rejects_puts_the_old_config_back(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """Owner ruling 2026-09-23: a failed Apply puts the old config back.

    Measured before the restore: the plugin list came back empty and the same
    item's destination became a literal ``%the{$artist}/Come Together.mp3``.
    """
    before = _live_state(client, beets_library)
    music = Path(beets_library.lib.directory.decode())
    beets_library.config_path.write_text(
        f"directory: {music}\nlibrary: library.db\n"
        "plugins:\n  - musicbrainz\n  - the\nmusicbrainz: no\n",
        encoding="utf-8",
    )

    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == restored_config_rejected("musicbrainz must be a dict, not bool")
    _assert_restored(client, before)


def test_the_apply_contract_names_the_restore() -> None:
    """The 422 covers a load put back; the 500 is left for a restore that failed."""
    from app.main import app

    responses = app.openapi()["paths"]["/api/config/apply"]["post"]["responses"]
    assert responses["422"]["description"] == (
        "config.yaml on disk is not a regular file, is unreadable, skips an include,"
        " breaks the store layout, or failed to load and the old config was put back;"
        " the recovery line says what to fix."
    )
    assert responses["500"]["description"] == (
        "The rebuild failed and putting the old config back failed too; fix the"
        " error the recovery line quotes and Apply again."
    )


def test_a_restored_apply_leaves_the_saved_file_pending(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """The file on disk still differs from what is loaded."""
    _live_state(client, beets_library)
    music = Path(beets_library.lib.directory.decode())
    cfg = beets_library.config_path
    cfg.write_text(f"directory: {music}\nlibrary: library.db\nplugins: 5\n", encoding="utf-8")
    later = time.time() + 5
    os.utime(cfg, (later, later))

    assert client.post("/api/config/apply").status_code == 422
    assert client.get("/api/config").json()["apply_pending"] is True


def test_a_restore_that_fails_too_answers_the_500(
    client: TestClient, beets_library: LibraryHandle, caplog: pytest.LogCaptureFixture
) -> None:
    """The old library file is a directory by the time the restore opens it."""
    from app.main import app

    _live_state(client, beets_library)
    music = Path(beets_library.lib.directory.decode())
    cfg = beets_library.config_path
    cfg.write_text(f"directory: {music}\nlibrary: other.db\nplugins: 5\n", encoding="utf-8")
    old_db = beets_library.beets_dir / "library.db"
    old_db.rename(beets_library.beets_dir / "library.db.moved")
    old_db.mkdir()
    cause = "plugins: must be a whitespace-separated string or a list"

    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        r = client.post("/api/config/apply")

    assert r.status_code == 500, r.text
    assert r.json()["detail"] == {
        "message": f"Apply failed during rebuild: {cause}",
        "recovery": config_rejected(cause),
    }
    records = [rec for rec in caplog.records if rec.name == "uvicorn.error"]
    assert [rec.getMessage() for rec in records] == [
        f"Apply could not put the old config back after: {cause}"
    ]
    assert records[0].exc_info is not None
    assert str(records[0].exc_info[1]) == "unable to open database file"
    old_db.rmdir()
    cfg.write_text(
        f"directory: {music}\nlibrary: library.db\nplugins:\n  - the\n", encoding="utf-8"
    )
    assert client.post("/api/config/apply").status_code == 200
    assert [p.name for p in plugins.find_plugins()] == ["the"]
    assert app.state.beets_library.lib.path == old_db


def test_a_mistyped_tag_in_config_yaml_is_refused_with_nothing_changed(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """PyYAML raises ``KeyError('ture')``, which escaped every arm as a bare 500."""
    before = _live_state(client, beets_library)
    cfg = beets_library.config_path
    music = Path(beets_library.lib.directory.decode())
    cfg.write_text(f"directory: {music}\nlibrary: library.db\nx: !!bool ture\n", encoding="utf-8")

    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {
        "message": f"Apply refused: {cfg} could not be read: KeyError: 'ture'",
        "recovery": (
            "beets could not read config.yaml, so nothing was changed."
            " Fix the file and Apply again."
        ),
    }
    _assert_unchanged(client, before)


def test_a_mistyped_tag_in_an_include_is_refused_with_nothing_changed(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """The same tag in an include: the gate names the include."""
    before = _live_state(client, beets_library)
    music = Path(beets_library.lib.directory.decode())
    bad = beets_library.beets_dir / "bad.yaml"
    bad.write_text("x: !!bool ture\n", encoding="utf-8")
    beets_library.config_path.write_text(
        f"directory: {music}\nlibrary: library.db\ninclude:\n  - bad.yaml\n", encoding="utf-8"
    )

    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {
        "message": "Apply refused: `include:` in config.yaml could not be read",
        "recovery": (
            "`include:` in config.yaml could not be read: 'bad.yaml' raised KeyError."
            " Fix the include: list."
        ),
    }
    _assert_unchanged(client, before)


def test_a_file_that_breaks_after_the_gate_in_an_include_is_not_blamed_on_config_yaml(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """beets' own read follows ``include:``; its non-YAML errors may be an include's."""
    from app.beets import config_editor
    from app.beets.setup import read_beets_config as real_read

    before = _live_state(client, beets_library)
    cfg = beets_library.config_path
    music = Path(beets_library.lib.directory.decode())
    bad = beets_library.beets_dir / "late.yaml"
    bad.write_text("x: 1\n", encoding="utf-8")
    cfg.write_text(
        f"directory: {music}\nlibrary: library.db\ninclude:\n  - late.yaml\n", encoding="utf-8"
    )

    def _broken_after_the_gate(beets_dir: str) -> object:
        bad.write_text("x: !!bool ture\n", encoding="utf-8")
        return real_read(beets_dir)

    monkeypatch.setattr(config_editor, "read_beets_config", _broken_after_the_gate)

    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {
        "message": (f"Apply refused: {cfg} or one of its includes could not be read: KeyError"),
        "recovery": (
            "beets could not read config.yaml or one of its includes, so nothing was"
            " changed. Fix the file and Apply again."
        ),
    }
    _assert_unchanged(client, before)


@pytest.fixture
def repointed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Path]]:
    """Booted through ``beets -> beetsA``; the link now points at ``beetsB``.

    ``beetsB/config.yaml`` holds a value beets rejects, and each dir has its own
    library path. Yields ``(beetsA, beetsB)``.
    """
    from app.beets.library import close_library
    from app.beets.setup import setup_beets
    from app.main import app

    music = tmp_path / "music"
    music.mkdir()
    a, b = tmp_path / "beetsA", tmp_path / "beetsB"
    a.mkdir()
    b.mkdir()
    (a / "config.yaml").write_text(
        f"directory: {music}\nlibrary: library.db\nplugins:\n  - the\n", encoding="utf-8"
    )
    (b / "config.yaml").write_text(
        f"directory: {music}\nlibrary: library.db\nplugins:\n  - musicbrainz\nmusicbrainz: no\n",
        encoding="utf-8",
    )
    link = tmp_path / "beets"
    link.symlink_to(a)
    monkeypatch.setattr("app.config.settings.beets_dir", str(link))
    prior = getattr(app.state, "beets_library", None)
    app.state.beets_library = setup_beets(str(link))
    link.unlink()
    link.symlink_to(b)
    try:
        yield a, b
    finally:
        close_library(app.state.beets_library.lib)
        if prior is None:
            del app.state.beets_library
        else:
            app.state.beets_library = prior


def test_a_repointed_beets_dir_is_not_read_until_a_restart(repointed: tuple[Path, Path]) -> None:
    """The gate read the booted dir; beets read the setting again, and loaded the other."""
    from app.main import app

    a, b = repointed

    r = TestClient(app).post("/api/config/apply")

    assert r.status_code == 200, r.text
    assert app.state.beets_library.lib.path == a / "library.db"
    assert os.environ["BEETSDIR"] == str(a)
    assert not (b / "library.db").exists()


def test_a_failed_apply_after_a_repoint_puts_back_the_booted_library(
    repointed: tuple[Path, Path],
) -> None:
    """Measured before: the 422 said nothing was changed and the restore opened ``beetsB``."""
    from app.main import app

    a, b = repointed
    (a / "config.yaml").write_text(
        (b / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    r = TestClient(app).post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == restored_config_rejected("musicbrainz must be a dict, not bool")
    assert app.state.beets_library.lib.path == a / "library.db"
    assert os.environ["BEETSDIR"] == str(a)
    assert not (b / "library.db").exists()
