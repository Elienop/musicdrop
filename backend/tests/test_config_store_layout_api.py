"""The containment rule on the three request paths that can move ``directory:``.

``M`` is the only one of the four paths that moves at runtime, and it moves
through this editor: Validate lints a draft, Save writes it, Apply re-reads what
is on disk. All three run the same predicate
(``app.beets.store_layout.check_store_layout``) so the gutter, the Save refusal
and the Apply refusal cannot disagree about which documents are acceptable.

Every refusal case is paired with a control that still passes — a fail-closed
check that refused everything would satisfy each "is it refused?" assertion on
its own.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.beets.library import LibraryHandle


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _yaml_pointing_at(directory: Path) -> str:
    return f"directory: {directory}\nlibrary: library.db\n"


# --------------------------------------------------------------------------
# POST /api/config/validate — the editor's lint gutter.
# --------------------------------------------------------------------------


def test_validate_flags_a_directory_that_would_swallow_the_origin_store(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``directory:`` that CONTAINS the origin store, and breaks nothing else.

    The store is moved to a tree of its own first, so the candidate music root
    can sit above it without also nesting with the beets data dir — that pair
    has its own rule and its own message, and this row has to come from the
    ``M contains O`` one.

    ``loc`` and ``line`` matter as much as the refusal: the frontend paints the
    gutter from those two, and disables Save while any error row is present
    (SettingsBeetsPage) — so a row with no position would refuse the save with
    nothing on screen to explain it.
    """
    outside = beets_library.beets_dir.parent / "records"
    monkeypatch.setattr("app.config.settings.trash_origins_dir", str(outside / "store"))

    r = client.post("/api/config/validate", json={"yaml_text": _yaml_pointing_at(outside)})
    assert r.status_code == 200
    rows = [e for e in r.json()["errors"] if e["type"] == "store_layout"]
    assert len(rows) == 1, r.json()
    assert rows[0]["loc"] == "directory"
    assert rows[0]["line"] == 1
    assert "The music library contains the Trash origin store" in rows[0]["msg"]


def test_validate_flags_a_directory_that_nests_with_the_beets_dir(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """``directory:`` pointed AT the beets data dir — the ``B is M`` row.

    This document used to be accepted (the rule table had no B-vs-M entry) and
    the review round measured what it costs: one library-scope Reorganize
    offered bank/, inbox/, playlists/, plex/ and slskd/ for trashing.
    """
    r = client.post(
        "/api/config/validate",
        json={"yaml_text": _yaml_pointing_at(beets_library.beets_dir)},
    )
    assert r.status_code == 200
    rows = [e for e in r.json()["errors"] if e["type"] == "store_layout"]
    assert len(rows) == 1, r.json()
    assert rows[0]["loc"] == "directory"
    assert "The beets data directory is the music library" in rows[0]["msg"]


def test_validate_flags_a_library_key_that_would_sit_under_trash(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``library:`` row, and the gutter marks THAT key rather than ``directory:``.

    ``library:`` is an independent beets key: the database file can be moved into
    Trash while all four directories stay disjoint. Measured in the review round —
    the document passed and Empty Trash removed ``library.db``.
    """
    trash = beets_library.beets_dir.parent / "bin"
    monkeypatch.setattr("app.config.settings.trash_dir", str(trash))
    music = Path(beets_library.lib.directory.decode())
    text = f"directory: {music}\nlibrary: {trash / 'library.db'}\n"

    r = client.post("/api/config/validate", json={"yaml_text": text})
    assert r.status_code == 200
    rows = [e for e in r.json()["errors"] if e["type"] == "store_layout"]
    assert len(rows) == 1, r.json()
    assert rows[0]["loc"] == "library"
    assert rows[0]["line"] == 2  # the ``library:`` line, not the ``directory:`` one
    assert "The Trash directory contains the beets database" in rows[0]["msg"]


def test_validate_accepts_the_directory_the_library_already_uses(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """The control. Same route, same shape of document, no containment row."""
    music = Path(beets_library.lib.directory.decode())
    r = client.post("/api/config/validate", json={"yaml_text": _yaml_pointing_at(music)})
    assert r.status_code == 200
    assert r.json()["errors"] == []


def test_validate_flags_a_directory_that_would_sit_under_trash(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction: Trash stays put and ``directory:`` moves under it.

    Pinned separately because the refusal is decided by a pair, so a check that
    only ever looked at ``directory:`` against the beets dir would pass the case
    above and miss this one.
    """
    trash = beets_library.beets_dir.parent / "bin"
    monkeypatch.setattr("app.config.settings.trash_dir", str(trash))
    r = client.post("/api/config/validate", json={"yaml_text": _yaml_pointing_at(trash / "music")})
    assert r.status_code == 200
    msgs = [e["msg"] for e in r.json()["errors"]]
    assert any("The Trash directory contains the music library" in m for m in msgs), msgs


def test_validate_answers_a_lint_row_for_a_directory_that_will_not_resolve(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """``directory: "/music/\\0evil"`` — a plain double-quoted YAML scalar.

    ``lstat`` raises ``ValueError`` on an embedded NUL, and this route had no
    handler for it: measured in the review round as an UNHANDLED ValueError, a
    500 with a traceback, where the pre-slice route answered a clean
    ``value_error`` row. Both rows are asserted, because the layout check runs
    first and used to take the schema's row down with it.
    """
    r = client.post(
        "/api/config/validate",
        # The YAML source carries a backslash-zero escape; ruamel decodes it to a
        # NUL byte, which is what reaches ``resolve()``.
        json={"yaml_text": 'directory: "/music/\\0evil"\nlibrary: library.db\n'},
    )
    assert r.status_code == 200
    rows = r.json()["errors"]
    assert any(e["type"] == "store_layout" and "could not be resolved" in e["msg"] for e in rows), (
        rows
    )
    assert any(e["type"] == "value_error" and e["loc"] == "directory" for e in rows), rows


# --------------------------------------------------------------------------
# POST /api/config/save — the write, refused BEFORE it happens.
# --------------------------------------------------------------------------


def test_save_refuses_the_same_document_the_gutter_flags(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """422 with the same sentence, and — the part that matters — nothing written.

    A config saved in this shape would also refuse to BOOT, so a Save that wrote
    it first and complained afterwards would leave the operator with a process
    that will not come back up after the next restart.

    Same layout as the validate test above, deliberately: the two routes share
    one helper, and pinning them on the same document is what shows they agree.
    """
    outside = beets_library.beets_dir.parent / "records"
    monkeypatch.setattr("app.config.settings.trash_origins_dir", str(outside / "store"))
    config_path = beets_library.config_path
    before = config_path.read_bytes()

    r = client.post(
        "/api/config/save",
        json={
            "yaml_text": _yaml_pointing_at(outside),
            "base_sha256": _sha(config_path),
        },
    )

    assert r.status_code == 422
    rows = [d for d in r.json()["detail"] if d["type"] == "store_layout"]
    assert len(rows) == 1, r.json()
    assert rows[0]["loc"] == "directory"
    assert "Trash origin store" in rows[0]["msg"]
    assert config_path.read_bytes() == before


def test_save_still_writes_an_acceptable_document(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """The control: the check is a filter, not a wall."""
    config_path = beets_library.config_path
    music = Path(beets_library.lib.directory.decode())
    text = _yaml_pointing_at(music) + "# a comment the save must keep\n"

    r = client.post(
        "/api/config/save",
        json={"yaml_text": text, "base_sha256": _sha(config_path)},
    )

    assert r.status_code == 200
    assert "a comment the save must keep" in config_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# POST /api/config/apply — the on-disk document, which a hand edit can change.
# --------------------------------------------------------------------------


def test_apply_refuses_a_violating_on_disk_config_and_keeps_the_old_handle(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Apply's input is the FILE, so a hand edit reaches it without passing Save.

    Two assertions, and the second is the reason the check runs before
    ``_rebuild_beets_handle`` rather than after: that function tears the old
    handle down first (its own docstring says a failure past that point leaves
    the process degraded until restart), and a config in this shape would not
    boot either — so a post-teardown refusal would strand the process with
    nothing to fall back to.
    """
    from app.main import app

    music = Path(beets_library.lib.directory.decode())
    monkeypatch.setattr("app.config.settings.trash_dir", str(music))
    beets_library.config_path.write_text(_yaml_pointing_at(music), encoding="utf-8")
    handle_before = app.state.beets_library

    r = client.post("/api/config/apply")

    assert r.status_code == 422
    detail = r.json()["detail"]
    # ``recovery`` and not ``message``: SettingsBeetsPage prints
    # "Apply failed. " + detail.recovery, so the operator sentence has to be in
    # that field or the page shows its generic fallback instead.
    assert "The Trash directory is the music library" in detail["recovery"]
    assert "MUSICDROP_TRASH_DIR" in detail["recovery"]
    assert app.state.beets_library is handle_before
    # ...and the old handle is still usable — no reset_beets_globals ran, so its
    # SQLite connection is open and the app keeps serving the previous config.
    assert client.get("/api/albums").status_code == 200


def test_apply_still_reloads_an_acceptable_on_disk_config(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """The control: a config that passes the check still swaps the handle."""
    from app.main import app

    handle_before = app.state.beets_library
    r = client.post("/api/config/apply")
    assert r.status_code == 200
    assert app.state.beets_library is not handle_before


def test_apply_lets_an_unparseable_config_reach_the_rebuild(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """A YAML syntax error is NOT a layout refusal, and must not be reported as one.

    The containment gate reads the same file; returning its message for a broken
    document would name the wrong problem and point at the wrong setting. It
    stays silent and lets ``setup_beets`` fail on its own, which is the 500 with
    the restart hint.
    """
    beets_library.config_path.write_text("directory: [unclosed\n", encoding="utf-8")
    r = client.post("/api/config/apply")
    assert r.status_code == 500
    assert "Apply failed during rebuild" in r.json()["detail"]["message"]


# --------------------------------------------------------------------------
# ``include:`` — the key that decides ``directory:`` from a file the editor is
# not showing.
# --------------------------------------------------------------------------


def _with_include(directory: Path, *includes: str) -> str:
    """A document whose own ``directory:`` is ``directory``, plus ``include:`` rows."""
    rows = "".join(f"  - {name}\n" for name in includes)
    return f"directory: {directory}\nlibrary: library.db\ninclude:\n{rows}"


def _layout_rows(client: TestClient, yaml_text: str) -> list[dict[str, object]]:
    r = client.post("/api/config/validate", json={"yaml_text": yaml_text})
    assert r.status_code == 200, r.text
    rows: list[dict[str, object]] = [e for e in r.json()["errors"] if e["type"] == "store_layout"]
    return rows


def test_validate_flags_a_directory_an_include_overrides(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """The document's own ``directory:`` is safe; the included file's is not.

    beets merges every file under ``include:`` at HIGHEST priority
    (``beets/__init__.py:29-38``), so this document's effective music root is the
    beets data directory. Measured in the review round against the first pass of
    this gate, which read ``data["directory"]``: Validate reported clean, Save
    wrote the file, Apply reloaded it, and the process was left serving a layout
    the next start refuses.
    """
    music = Path(beets_library.lib.directory.decode())
    overlay = beets_library.beets_dir / "overlay.yaml"
    overlay.write_text(f"directory: {beets_library.beets_dir}\n", encoding="utf-8")

    rows = _layout_rows(client, _with_include(music, "overlay.yaml"))

    assert len(rows) == 1, rows
    assert "The beets data directory is the music library" in str(rows[0]["msg"])


def test_validate_accepts_a_dangerous_directory_an_include_replaces(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """The inverse, and the control: the top-level value is the refused one.

    A gate that refused any document carrying an ``include:``, or that merged in
    the wrong direction, fails here — the effective root is the included file's
    music dir and the document is acceptable.
    """
    music = Path(beets_library.lib.directory.decode())
    overlay = beets_library.beets_dir / "overlay.yaml"
    overlay.write_text(f"directory: {music}\n", encoding="utf-8")

    rows = _layout_rows(client, _with_include(beets_library.beets_dir, "overlay.yaml"))

    assert rows == []


def test_the_last_include_wins_when_two_of_them_disagree(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """Order inside ``include:`` decides, and the LAST entry is the one that does.

    ``set_file`` inserts at the front of the source list (``confuse/core.py:415``
    — ``RootView.set`` is ``sources.insert(0, ...)``), so each successive include
    outranks the one before it. Both directions are asserted from one fixture,
    because a merge that ignored order would satisfy either half alone.
    """
    music = Path(beets_library.lib.directory.decode())
    (beets_library.beets_dir / "safe.yaml").write_text(f"directory: {music}\n", encoding="utf-8")
    (beets_library.beets_dir / "unsafe.yaml").write_text(
        f"directory: {beets_library.beets_dir}\n", encoding="utf-8"
    )

    assert _layout_rows(client, _with_include(music, "unsafe.yaml", "safe.yaml")) == []
    assert len(_layout_rows(client, _with_include(music, "safe.yaml", "unsafe.yaml"))) == 1


def test_an_include_naming_a_file_that_is_not_there_leaves_the_document_standing(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """An unreadable include is beets' own tolerated case, and both halves matter.

    beets writes the failure to stderr and carries on with what it has
    (``beets/__init__.py:29-38``), so the value that survives is the document's.
    The second half is what keeps the tolerance from becoming a hole: a document
    whose OWN ``directory:`` is refused stays refused when its include is
    missing.
    """
    music = Path(beets_library.lib.directory.decode())

    assert _layout_rows(client, _with_include(music, "not-written-yet.yaml")) == []
    assert len(_layout_rows(client, _with_include(beets_library.beets_dir, "gone.yaml"))) == 1


def test_an_include_that_is_not_a_list_answers_a_lint_row_rather_than_a_500(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """``include:`` as a mapping — a shape beets raises on at startup.

    confuse answers ``ConfigTypeError`` for it, which is neither of the two cases
    beets tolerates. The gate reports the document's own ``directory:`` instead
    of propagating, so a hand-edited draft gets a lint gutter rather than a 500
    from the editor; the config itself would stop the next start.
    """
    music = Path(beets_library.lib.directory.decode())
    text = f"directory: {music}\nlibrary: library.db\ninclude:\n  a: b\n"

    r = client.post("/api/config/validate", json={"yaml_text": text})

    assert r.status_code == 200, r.text
    assert [e for e in r.json()["errors"] if e["type"] == "store_layout"] == []


def test_save_refuses_a_document_whose_include_moves_the_directory(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """Save takes the same helper, so the write is refused before it happens."""
    music = Path(beets_library.lib.directory.decode())
    (beets_library.beets_dir / "overlay.yaml").write_text(
        f"directory: {beets_library.beets_dir}\n", encoding="utf-8"
    )
    config_path = beets_library.config_path
    before = config_path.read_bytes()

    r = client.post(
        "/api/config/save",
        json={
            "yaml_text": _with_include(music, "overlay.yaml"),
            "base_sha256": _sha(config_path),
        },
    )

    assert r.status_code == 422, r.text
    rows = [d for d in r.json()["detail"] if d["type"] == "store_layout"]
    assert len(rows) == 1, r.json()
    assert config_path.read_bytes() == before


def test_apply_refuses_an_on_disk_include_that_moves_the_directory(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """Apply reads the file, and the file's ``include:`` is part of what it reads."""
    from app.main import app

    music = Path(beets_library.lib.directory.decode())
    (beets_library.beets_dir / "overlay.yaml").write_text(
        f"directory: {beets_library.beets_dir}\n", encoding="utf-8"
    )
    beets_library.config_path.write_text(_with_include(music, "overlay.yaml"), encoding="utf-8")
    handle_before = app.state.beets_library

    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert "The beets data directory is the music library" in r.json()["detail"]["recovery"]
    assert app.state.beets_library is handle_before


def test_apply_refuses_after_the_rebuild_when_the_pre_check_missed_it(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backstop, driven by simulating the divergence it exists for.

    ``on_disk_layout_error`` reproduces beets' merge over the file; this test
    blinds that reproduction so the only thing left to catch the layout is the
    check that reads ``new.lib.directory`` off the handle beets actually built.
    The handle IS swapped — the rebuild closed the old library, so there is
    nothing to put back — and the 422 says so.
    """
    from app.main import app

    monkeypatch.setattr("app.beets.config_editor.on_disk_layout_error", lambda *a, **k: None)
    beets_library.config_path.write_text(
        _yaml_pointing_at(beets_library.beets_dir), encoding="utf-8"
    )
    handle_before = app.state.beets_library

    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    recovery = r.json()["detail"]["recovery"]
    assert "The beets data directory is the music library" in recovery
    assert "answer 503" in recovery
    assert app.state.beets_library is not handle_before


def test_a_document_with_no_directory_key_gets_the_schema_row_and_no_layout_row(
    client: TestClient,
    beets_library: LibraryHandle,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """beets would fall back to ``~/Music``; the gutter stays quiet about it.

    The fallback is real — ``directory: ~/Music`` is beets' own default
    (``beets/config_default.yaml``) and :func:`effective_config_paths` reads the
    defaults, so without the guard in ``store_layout_errors`` this document
    produces a refusal about a path the operator did not write. ``HOME`` is
    pointed at ``tmp_path`` and the origin store placed under ``~/Music`` so the
    fallback WOULD trip a rule if it were checked: the assertion is that it is
    not, and that ``KnownKeysSchema`` reports the absent key instead.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("app.config.settings.trash_origins_dir", str(tmp_path / "Music" / "store"))

    r = client.post("/api/config/validate", json={"yaml_text": "library: library.db\n"})

    assert r.status_code == 200, r.text
    errors = r.json()["errors"]
    assert [e for e in errors if e["type"] == "store_layout"] == [], errors
    assert [e for e in errors if e["loc"] == "directory"] != [], errors
