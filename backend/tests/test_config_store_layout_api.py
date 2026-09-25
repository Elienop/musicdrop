"""The containment rule on the three request paths that can move ``directory:``.

``M`` is the path this editor moves: Validate lints a draft, Save writes it,
Apply re-reads what is on disk. All three run the same predicate
(``app.beets.store_layout.check_store_layout``), so the gutter and the Save
refusal agree by construction. Apply can still differ from both — it reads the
file rather than the submitted document, which is the divergence
``test_apply_refuses_after_the_rebuild_on_a_real_pre_check_divergence`` drives.

Every refusal case is paired with a control that still passes — a fail-closed
check that refused everything would satisfy each "is it refused?" assertion on
its own.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.beets.config_check import load_config_text
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


def test_validate_flags_a_symlinked_component_below_the_music_root(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows say WHERE the Trash may sit; this says whether it can be REACHED.

    Measured 2026-09-12 (security seat L-3): the report ran the rows only and the
    reachability walk lived at the three destructive call sites, so a Trash below
    the music root through a symlinked component painted a healthy Settings page
    while every delete, restore and Empty-Trash answered 503.
    """
    music = Path(beets_library.lib.directory.decode())
    holder = music / "a"
    holder.mkdir()
    elsewhere = beets_library.beets_dir.parent / "somewhere-else"
    elsewhere.mkdir()
    holder.rmdir()
    holder.symlink_to(elsewhere)
    monkeypatch.setattr("app.config.settings.trash_dir", str(music / "a" / "b" / ".trash"))

    rows = _layout_rows(client, _yaml_pointing_at(music))

    assert len(rows) == 1, rows
    assert "not reachable below the music library" in str(rows[0]["msg"])
    assert list(elsewhere.iterdir()) == [], "a report creates nothing"


def test_validate_accepts_a_trash_below_the_music_root_that_is_reachable(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the same nested layout with real directories all the way.

    A Trash strictly inside the music library is allowed on purpose, and its
    parts do not have to exist yet — the walk stops at the first one that is
    absent without a row.
    """
    music = Path(beets_library.lib.directory.decode())
    monkeypatch.setattr("app.config.settings.trash_dir", str(music / "a" / "b" / ".trash"))

    assert _layout_rows(client, _yaml_pointing_at(music)) == []
    assert not (music / "a").exists(), "a report creates nothing"


def test_validate_flags_a_file_in_the_operators_chain_outside_the_library(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last shape the report read HEALTHY while every delete answered 503.

    Measured 2026-09-12: an EACCES or a symlink loop anywhere in the Trash's
    chain is refused by the rows' own resolve first, but a plain FILE in the
    operator's chain ABOVE the music root reached the reachability walk and
    painted nothing — the walk returned on the OSError. The wording is the
    destructive routes' own, because that is the answer the next delete gives.
    """
    music = Path(beets_library.lib.directory.decode())
    chain = beets_library.beets_dir.parent / "chain"
    chain.write_bytes(b"not a directory")
    monkeypatch.setattr("app.config.settings.trash_dir", str(chain / "trash"))

    rows = _layout_rows(client, _yaml_pointing_at(music))

    assert len(rows) == 1, rows
    assert "could not be created" in str(rows[0]["msg"])
    assert "Not a directory" in str(rows[0]["msg"])
    assert chain.read_bytes() == b"not a directory", "a report creates nothing"


def test_validate_flags_an_attackers_link_at_the_operators_link_target(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The link shape the read-only report has to paint the same way.

    Settings is the only place the operator sees this before a delete does, and
    the walk it runs creates nothing — so the row and the 503 come from one
    walk (security seat L-3). The shape is H-1's: the operator spells the Trash
    through a link of their own and the attacker owns its target
    (``<M>/a -> elsewhere``), which used to read HEALTHY here exactly when the
    Trash escaped the library.
    """
    music = Path(beets_library.lib.directory.decode())
    elsewhere = beets_library.beets_dir.parent / "somewhere-else"
    elsewhere.mkdir()
    (music / "a").symlink_to(elsewhere)
    jump_in = beets_library.beets_dir.parent / "srv-x"
    jump_in.symlink_to(music / "a")
    monkeypatch.setattr("app.config.settings.trash_dir", str(jump_in / ".trash"))

    rows = _layout_rows(client, _yaml_pointing_at(music))

    assert len(rows) == 1, rows
    assert "not reachable below the music library" in str(rows[0]["msg"])
    assert list(elsewhere.iterdir()) == [], "a report creates nothing"


@pytest.mark.skipif(os.getuid() == 0, reason="root searches an unsearchable directory anyway")
def test_validate_flags_a_trash_chain_the_walk_cannot_climb(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fail-closed climb's REPORT arm: a row here, and Save refused with it.

    The refusal was pinned only where a delete meets it
    (``test_a_chain_the_walk_cannot_climb_is_refused_rather_than_read_as_outside``),
    and this path reaches it through its own read-only walk — so the arm that
    reads "cannot answer" as "outside the library" could come back on the report
    alone and Settings would read healthy again, which is the shape security seat
    L-3 was about. Measured 2026-09-13: mode ``0o400`` is readable, so the walk
    opens the Trash, and not searchable, so the ``..`` climb out of it answers
    EACCES. The control is the document itself — the same YAML saves with 200 in
    ``test_save_still_writes_an_acceptable_document``.
    """
    music = Path(beets_library.lib.directory.decode())
    unsearchable = beets_library.beets_dir.parent / "srv-trash"
    unsearchable.mkdir()
    os.chmod(unsearchable, 0o400)
    monkeypatch.setattr("app.config.settings.trash_dir", str(unsearchable))
    config_path = beets_library.config_path
    before = config_path.read_bytes()

    try:
        rows = _layout_rows(client, _yaml_pointing_at(music))
        saved = client.post(
            "/api/config/save",
            json={"yaml_text": _yaml_pointing_at(music), "base_sha256": _sha(config_path)},
        )
    finally:
        os.chmod(unsearchable, 0o700)  # or the tmp_path teardown cannot clean up

    assert len(rows) == 1, rows
    assert rows[0]["loc"] == "directory"
    assert "could not be checked against the music library" in str(rows[0]["msg"])
    assert saved.status_code == 422, saved.text
    refusals = [d for d in saved.json()["detail"] if d["type"] == "store_layout"]
    assert len(refusals) == 1, saved.json()
    assert "could not be checked against the music library" in str(refusals[0]["msg"])
    assert config_path.read_bytes() == before, "a refused Save writes nothing"


def test_validate_paints_no_row_for_a_directory_that_is_not_there_yet(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W1: the read-only report inherited the destructive path's mount refusal.

    Measured 2026-09-12 (code seat W1): a candidate ``directory:`` that does not
    exist, with the Trash spelled below it, painted "could not be opened … Is the
    music share mounted?" — and ``config_editor.save`` turns a row into a 422
    while the editor disables Save on any row, so a typo'd or not-yet-created
    ``directory:`` blocked EVERY beets-config save (plugins, naming, import).
    The rows are deliberately silent for a path that is not there yet; the
    destructive path keeps the refusal
    (``test_an_unmounted_music_root_is_reported_as_the_music_roots_fault``).
    """
    candidate = beets_library.beets_dir.parent / "newmusic"  # never created
    monkeypatch.setattr("app.config.settings.trash_dir", str(candidate / ".trash"))

    assert _layout_rows(client, _yaml_pointing_at(candidate)) == []
    assert not candidate.exists(), "a report creates nothing"


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


def test_validate_answers_ONE_lint_row_for_a_directory_that_will_not_resolve(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """``directory: "/music/\\0evil"`` — a plain double-quoted YAML scalar.

    ``lstat`` raises ``ValueError`` on an embedded NUL, and this route had no
    handler for it: measured in the review round as an UNHANDLED ValueError, a
    500 with a traceback, where the pre-slice route answered a clean
    ``value_error`` row.

    ONE row, not two. Both checks reach the same conclusion about the same key
    from the same ``lstat``, and the editor maps rows to CodeMirror diagnostics
    1:1 with no dedupe, so the second was the same sentence twice in the gutter.
    The control below is what keeps this from swallowing a real layout row.
    """
    r = client.post(
        "/api/config/validate",
        # The YAML source carries a backslash-zero escape; ruamel decodes it to a
        # NUL byte, which is what reaches ``resolve()``.
        json={"yaml_text": 'directory: "/music/\\0evil"\nlibrary: library.db\n'},
    )
    assert r.status_code == 200
    rows = r.json()["errors"]
    assert [(e["type"], e["loc"]) for e in rows] == [("value_error", "directory")], rows
    assert "could not be resolved" in rows[0]["msg"], rows


def test_validate_keeps_the_layout_row_when_the_schema_row_is_about_something_else(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """The control for the dedupe above: ``directory: /``.

    Two rows on one key again, and here they are two different facts — the
    schema's is about writability, the layout's names the loss (the music
    library would contain the beets data directory). Only a refusal about a
    single UNUSABLE VALUE is suppressed, so this pair survives; without that
    distinction the dedupe would take down the only row that explains what the
    document would destroy.
    """
    r = client.post(
        "/api/config/validate",
        json={"yaml_text": "directory: /\nlibrary: library.db\n"},
    )
    assert r.status_code == 200
    rows = r.json()["errors"]
    types = {e["type"] for e in rows}
    assert "store_layout" in types, rows
    assert "value_error" in types, rows


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


def test_apply_reports_an_unparseable_config_as_unreadable_not_as_a_layout(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """A YAML syntax error is NOT a layout refusal, and must not be reported as one.

    The containment gate reads the same file; returning its message for a broken
    document would name the wrong problem and point at the wrong setting.
    """
    beets_library.config_path.write_text("a: 1\ndirectory: [unclosed\nb: 2\n", encoding="utf-8")
    r = client.post("/api/config/apply")
    assert r.status_code == 422
    assert r.json()["detail"]["recovery"] == (
        "beets could not read config.yaml (line 3), so nothing was changed."
        " Fix the file and Apply again."
    )


@pytest.mark.parametrize(
    "text",
    [f"library: library.db\ndirectory: {'9' * 5000}\n", "a: " + "[" * 5000 + "]" * 5000 + "\n"],
    ids=["an-integer-too-long-to-build", "nested-past-the-limit"],
)
def test_apply_answers_its_own_body_for_the_two_shapes_yaml_does_not_call_yaml(
    client: TestClient, beets_library: LibraryHandle, text: str
) -> None:
    """``ValueError`` and ``RecursionError`` are parse failures too.

    The pre-check runs OUTSIDE the rebuild's handler, so either one escaping it
    is a bare 500 with no body. Both are hand-editable: a 5000-digit integer
    hits CPython's 4300-digit ``int()`` limit, and 5000 nested ``[`` exhaust
    PyYAML's recursive parser (measured).
    """
    beets_library.config_path.write_text(text, encoding="utf-8")

    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert r.json()["detail"]["recovery"] == (
        "beets could not read config.yaml, so nothing was changed. Fix the file and Apply again."
    )


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


def _skipped_rows(client: TestClient, yaml_text: str) -> list[object]:
    """The messages of Validate's rows for an include beets would skip."""
    r = client.post("/api/config/validate", json={"yaml_text": yaml_text})
    assert r.status_code == 200, r.text
    assert [a for a in r.json()["advisories"] if a["key"] == "include"] == []
    return [e["msg"] for e in r.json()["errors"] if e["type"] == "include_skipped"]


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


def test_an_include_naming_a_file_that_is_not_there_is_refused_and_the_document_still_checked(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """beets writes the failure to stderr and carries on with what it has
    (``beets/__init__.py:29-38``); Apply and boot refuse it, and since
    2026-09-25 so do Validate and Save. The layout is still asked of the
    document's own value: a document whose OWN ``directory:`` is refused stays
    refused when its include is missing.
    """
    music = Path(beets_library.lib.directory.decode())
    text = _with_include(music, "not-written-yet.yaml")

    assert _layout_rows(client, text) == []
    assert _skipped_rows(client, text) == [
        "beets would skip the include 'not-written-yet.yaml': No such file or directory"
    ]
    assert len(_layout_rows(client, _with_include(beets_library.beets_dir, "gone.yaml"))) == 1


@pytest.mark.parametrize(
    "include_block",
    [
        "include:\n  a: b\n",  # a mapping — ConfigTypeError
        "include: overlay.yaml\n",  # a bare string — ConfigTypeError
        "include:\n  - null\n",  # an entry that is not a filename
        "include:\n  - 7\n",
    ],
)
def test_an_include_beets_raises_on_answers_a_lint_row_rather_than_nothing(
    client: TestClient, beets_library: LibraryHandle, include_block: str
) -> None:
    """Shapes ``setup_beets`` raises ``ConfigTypeError`` on at startup.

    Measured on the parent commit for all four: Validate answered 200 with NO row
    of any type, Save wrote the file, and the Apply after answered 500 while a
    fresh start would not come up — the one thing this gate exists to prevent. It
    is the ``except confuse.ConfigError`` that swallowed them, ``ConfigTypeError``
    being a subclass. The test was named for the row it did not assert.
    """
    music = Path(beets_library.lib.directory.decode())
    text = f"directory: {music}\nlibrary: library.db\n{include_block}"

    r = client.post("/api/config/validate", json={"yaml_text": text})

    assert r.status_code == 200, r.text
    rows = [e for e in r.json()["errors"] if e["type"] == "store_layout"]
    assert len(rows) == 1, r.json()
    assert rows[0]["loc"] == "include", rows
    assert "could not be read" in str(rows[0]["msg"]), rows


@pytest.mark.parametrize("shape", ["fifo", "oversized", "nul", "not-a-mapping", "deep"])
def test_an_include_the_gate_will_not_read_answers_a_lint_row(
    client: TestClient, beets_library: LibraryHandle, shape: str
) -> None:
    """The four the gate refuses to follow, each measured on the parent commit.

    * ``fifo`` — confuse's ``open`` on a FIFO with no writer never returns, and
      ``YamlSource.__init__`` reads eagerly: Validate, Save and Apply each hung
      until the process was killed, pinning a threadpool worker per request. The
      gate opens ``O_NONBLOCK``, so it is ``fstat`` that names this one; a
      non-blocking read of a writer-less FIFO answers EOF, which would report an
      empty overlay for a file beets blocks on.
    * ``oversized`` — a 195 MiB include was read and parsed in 32.7 seconds.
    * ``nul`` — ``open`` raises ``ValueError``, which the old ``except`` missed:
      a bare 500 at all three routes.
    * ``not-a-mapping`` — confuse raises a bare ``TypeError``: a bare 500 too.
    * ``deep`` — nesting past Python's recursion limit raises ``RecursionError``,
      a ``RuntimeError`` the old arm missed: a bare 500 at all three routes,
      where a real ``setup_beets`` over the same file does not come up.
    """
    music = Path(beets_library.lib.directory.decode())
    name = "overlay.yaml"
    target = beets_library.beets_dir / name
    if shape == "fifo":
        os.mkfifo(target)
    elif shape == "oversized":
        with target.open("wb") as fh:
            fh.truncate(2 << 20)
    elif shape == "nul":
        # A double-quoted YAML scalar carrying a backslash-zero escape, the same
        # spelling the ``directory:`` NUL case uses; ruamel decodes it to a NUL.
        name = '"over\\0lay.yaml"'
    elif shape == "deep":
        target.write_text("a: " + "[" * 5000 + "]" * 5000 + "\n", encoding="utf-8")
    else:
        target.write_text("- a\n- b\n", encoding="utf-8")

    rows = _layout_rows(client, _with_include(music, name))

    assert len(rows) == 1, rows
    assert rows[0]["loc"] == "include", rows
    assert "could not be read" in str(rows[0]["msg"]), rows


@pytest.mark.parametrize(
    ("value", "kind"),
    [("42", "int"), ("~", "NoneType"), ("[/x]", "list"), ("010", "int"), ("yes", "bool")],
)
def test_an_include_that_makes_directory_a_non_path_answers_a_lint_row(
    client: TestClient, beets_library: LibraryHandle, value: str, kind: str
) -> None:
    """The overlay the document never shows, so only beets' read can report it.

    Measured on the parent commit for all five: Validate answered 200 with no
    row, Save wrote the file, and Apply answered 500 "directory: must be a
    filename, not int". Now that read runs at Validate, in beets' words; the
    value is not written in config.yaml, so the row has no line.
    """
    overlay = beets_library.beets_dir / "overlay.yaml"
    overlay.write_text(f"directory: {value}\n", encoding="utf-8")
    text = _with_include(Path(beets_library.lib.directory.decode()), "overlay.yaml")

    r = client.post("/api/config/validate", json={"yaml_text": text})

    assert r.status_code == 200, r.text
    assert r.json()["errors"] == [
        {
            "loc": "directory",
            "msg": f"must be a filename, not {kind}",
            "type": "beets_read",
            "line": None,
            "column": None,
        }
    ]


@pytest.mark.parametrize(
    "yaml_text",
    ["a: " + "[" * 5000 + "]" * 5000 + "\n", "a: " + "1" * 5000 + "\n", "a: !!bool ture\n"],
    ids=["nested-past-the-limit", "an-integer-too-long-to-build", "a-mistyped-bool-tag"],
)
def test_a_document_ruamel_will_not_parse_answers_the_parse_row(
    client: TestClient, beets_library: LibraryHandle, yaml_text: str
) -> None:
    """The document's own parse arm, which caught ``YAMLError`` alone.

    Measured on the parent commit: Validate and Save each answered a bare 500 —
    ruamel raises ``RecursionError`` past the nesting limit and ``ValueError``
    on an integer over 4300 digits, and neither is a ``YAMLError``.
    """
    r = client.post("/api/config/validate", json={"yaml_text": yaml_text})
    assert r.status_code == 200, r.text
    assert [e["type"] for e in r.json()["errors"]] == ["yaml_parse"], r.json()

    saved = client.post(
        "/api/config/save",
        json={"yaml_text": yaml_text, "base_sha256": _sha(beets_library.config_path)},
    )
    assert saved.status_code == 422, saved.text
    assert saved.json()["detail"][0]["type"] == "yaml_parse", saved.text


def test_an_include_just_under_the_size_cap_is_still_read(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """The control for the cap: a real include is kilobytes, not megabytes.

    Padded to just under a megabyte with comment lines, so a cap set at the wrong
    order of magnitude — or one applied to every include — refuses a file the
    gate must still follow. Its ``directory:`` is the refused one, which is what
    proves the file was actually merged rather than skipped.
    """
    overlay = beets_library.beets_dir / "overlay.yaml"
    padding = "# " + ("y" * 60) + "\n"
    overlay.write_text(
        f"directory: {beets_library.beets_dir}\n" + padding * 16_000, encoding="utf-8"
    )
    assert 900_000 < overlay.stat().st_size < (1 << 20)

    rows = _layout_rows(
        client, _with_include(Path(beets_library.lib.directory.decode()), "overlay.yaml")
    )

    assert len(rows) == 1, rows
    assert "The beets data directory is the music library" in str(rows[0]["msg"]), rows


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


def test_the_apply_headline_names_the_pair_that_was_refused(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two different refusals, two different headlines.

    The pre-check's 422 ``message`` read "config.yaml would move the music
    library" for EVERY refusal — including the ones where the Trash moved and
    the library did not. It is derived from the refused pair now, so the two
    rows below cannot share a sentence.
    """
    from app.main import app

    music = Path(beets_library.lib.directory.decode())
    beets_library.config_path.write_text(
        _yaml_pointing_at(beets_library.beets_dir), encoding="utf-8"
    )
    first = client.post("/api/config/apply")
    assert first.status_code == 422, first.text
    assert first.json()["detail"]["message"] == (
        "Apply refused: The beets data directory is the music library"
    )

    # The same route, a refusal about the TRASH: the headline moves with it.
    monkeypatch.setattr("app.config.settings.trash_dir", str(music))
    beets_library.config_path.write_text(_yaml_pointing_at(music), encoding="utf-8")
    second = client.post("/api/config/apply")
    assert second.status_code == 422, second.text
    assert second.json()["detail"]["message"] == (
        "Apply refused: The Trash directory is the music library"
    )
    assert app.state.beets_library is not None


def test_a_directory_that_arrives_through_a_merge_key_gets_a_gutter_line(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``<<: *base`` — present in the mapping, absent from ruamel's position map.

    The row was painted with ``line=None`` while the editor disabled Save on it,
    so the operator got a refusal with nothing marked. The fallback answers with
    the ANCHOR's line, which is where the value is actually written — line 3
    here, not the ``<<:`` on line 4 and not a bare 1.

    The control is the row's ``line`` for the same key written plainly, and the
    other control is a MISSING key, which still gets no marker because there is
    no line to mark.
    """
    monkeypatch.setattr("app.config.settings.trash_dir", str(beets_library.beets_dir / "trash"))
    merged = (
        f"library: library.db\n_base: &base\n  directory: {beets_library.beets_dir}\n<<: *base\n"
    )

    r = client.post("/api/config/validate", json={"yaml_text": merged})
    assert r.status_code == 200, r.text
    rows = [e for e in r.json()["errors"] if e["type"] == "store_layout"]
    assert len(rows) == 1, r.json()
    assert rows[0]["loc"] == "directory"
    assert rows[0]["line"] == 3

    plain = client.post(
        "/api/config/validate",
        json={"yaml_text": _yaml_pointing_at(beets_library.beets_dir)},
    )
    plain_rows = [e for e in plain.json()["errors"] if e["type"] == "store_layout"]
    assert plain_rows[0]["line"] == 1

    absent = client.post("/api/config/validate", json={"yaml_text": "library: library.db\n"})
    missing = [e for e in absent.json()["errors"] if e["loc"] == "directory"]
    assert len(missing) == 1
    assert missing[0]["line"] is None


def test_apply_refuses_after_the_rebuild_when_the_file_changes_after_the_gate(
    client: TestClient,
    beets_library: LibraryHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The backstop, driven by the gap it exists for: two reads of one file.

    The gate reads config.yaml, then beets reads it again. An edit landing
    between the two loads a layout the gate never saw. The edit is made by the
    real read's caller, not by stubbing either check, so both checks run on
    real files.

    The handle IS swapped — the rebuild closed the old library, so there is
    nothing to put back — and D5's invariant is asserted with it: the import
    registry holds the SAME library the app now serves, not the closed one.
    """
    from app.beets import config_editor
    from app.beets.setup import read_beets_config as real_read
    from app.beets.store_layout import StoreLayoutError, checked_store_dirs
    from app.import_jobs.registry import get_registry
    from app.main import app

    def _edited_after_the_gate(beets_dir: str) -> object:
        beets_library.config_path.write_text(
            f"library: library.db\ndirectory: {beets_library.beets_dir}\n", encoding="utf-8"
        )
        return real_read(beets_dir)

    monkeypatch.setattr(config_editor, "read_beets_config", _edited_after_the_gate)
    handle_before = app.state.beets_library

    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    live_settings = config_editor._settings(app)
    with pytest.raises(StoreLayoutError) as refused:
        checked_store_dirs(live_settings, app.state.beets_library)
    assert [rec.getMessage() for rec in caplog.records if rec.name == "uvicorn.error"] == [
        f"Apply loaded a config whose store layout is refused: {refused.value}"
    ]
    body = r.json()["detail"]
    assert body["message"] == (
        "Apply loaded config.yaml. The beets data directory is the music library."
    )
    assert "The beets data directory is the music library" in body["recovery"]
    assert body["recovery"].endswith("Then restart MusicDrop.")
    assert app.state.beets_library is not handle_before
    # D5: one library after Apply, whatever the outcome. It used to keep the
    # CLOSED old one, which SQLite reopens on demand — an import then landed in
    # the pre-Apply store while the UI read the new one.
    assert get_registry()._lib is app.state.beets_library.lib
    # And the import that would land in it: measured before this, POST
    # /api/import answered 202 in this state and wrote its files into the beets
    # data dir — the very root the 422 above refused.
    started = client.post("/api/import", json={"path": str(beets_library.beets_dir.parent)})
    assert started.status_code == 503, started.text
    detail = started.json()["detail"]
    # The refusal used to be built as "but {headline}. {exc}" while str(exc)
    # already opens with the headline, so the 503 said it twice.
    assert detail == f"Apply loaded config.yaml. {refused.value}"


def test_a_restore_onto_a_refused_layout_answers_the_restore_not_loaded(
    client: TestClient,
    beets_library: LibraryHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A backstop 422 leaves a refused layout running; the next Apply fails and puts it back.

    Measured before: the 422 said "Apply loaded config.yaml, but" and offered a
    restart, which boot refuses while the rejected value is still in the file.
    """
    from app.beets import config_editor
    from app.beets.setup import read_beets_config as real_read
    from app.beets.store_layout import StoreLayoutError, checked_store_dirs
    from app.main import app

    def _edited_after_the_gate(beets_dir: str) -> object:
        beets_library.config_path.write_text(
            f"library: library.db\ndirectory: {beets_library.beets_dir}\n", encoding="utf-8"
        )
        return real_read(beets_dir)

    monkeypatch.setattr(config_editor, "read_beets_config", _edited_after_the_gate)
    assert client.post("/api/config/apply").status_code == 422
    monkeypatch.setattr(config_editor, "read_beets_config", real_read)
    live_settings = config_editor._settings(app)
    with pytest.raises(StoreLayoutError) as refused:
        checked_store_dirs(live_settings, app.state.beets_library)
    music = beets_library.beets_dir.parent / "music"
    beets_library.config_path.write_text(
        f"directory: {music}\nlibrary: library.db\nplugins:\n  - musicbrainz\nmusicbrainz: no\n",
        encoding="utf-8",
    )
    cause = "musicbrainz must be a dict, not bool"
    caplog.clear()

    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {
        "message": f"Apply failed and put the old config back: {cause}",
        "recovery": (
            f"beets rejected a value in the config: {cause}, so nothing was changed."
            " Fix it and Apply again; MusicDrop will not start until you do."
        ),
    }
    assert [rec.getMessage() for rec in caplog.records if rec.name == "uvicorn.error"] == [
        f"Apply put back a config whose store layout is refused: {refused.value}"
    ]
    started = client.post("/api/import", json={"path": str(music)})
    assert started.status_code == 503, started.text
    assert started.json()["detail"] == f"Apply put the old config back. {refused.value}"


def test_a_document_with_no_directory_key_gets_the_schema_row_and_no_layout_row(
    client: TestClient,
    beets_library: LibraryHandle,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """beets would fall back to ``~/Music``; the gutter stays quiet about it.

    The fallback is real — ``directory: ~/Music`` is beets' own default
    (``beets/config_default.yaml``) and :func:`effective_config_paths` reads the
    defaults, so without the guard in the Validate check this document
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


# --------------------------------------------------------------------------
# Values the SCHEMA has to refuse, because nothing downstream can.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["validate", "save"])
def test_a_directory_that_is_a_symlink_loop_is_a_row_not_a_500(
    client: TestClient, beets_library: LibraryHandle, tmp_path: Path, route: str
) -> None:
    """``directory:`` pointing at a self-referencing symlink.

    ``WritablePath``'s validator runs BEFORE the layout gate and called
    ``resolve()`` unguarded. Pydantic converts a ``ValueError`` from a validator
    into a row and lets ``RuntimeError`` and ``OSError`` escape, so both routes
    answered 500 with a traceback — measured on the parent commit at both. The
    layout gate's own "could not be resolved" refusal never got a turn.
    """
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    body: dict[str, object] = {"yaml_text": _yaml_pointing_at(loop)}
    if route == "save":
        body["base_sha256"] = _sha(beets_library.config_path)

    r = client.post(f"/api/config/{route}", json=body)

    assert r.status_code == (200 if route == "validate" else 422), r.text
    rows = r.json()["errors"] if route == "validate" else r.json()["detail"]
    assert any(e["loc"] == "directory" and "could not be resolved" in e["msg"] for e in rows), rows


@pytest.mark.parametrize(
    ("key", "kind", "fragment"),
    [
        ("directory", "loop", "could not be resolved"),
        ("library", "dir", "is a directory"),
    ],
)
def test_a_validation_row_repr_s_the_path_it_echoes(
    client: TestClient, tmp_path: Path, key: str, kind: str, fragment: str
) -> None:
    """A path holding a newline comes back escaped, not verbatim.

    The row goes to the app log and the CLI as well as to React, so a raw newline
    in an operator-supplied path forges a second line in both.
    ``store_layout._refuse`` has gone through ``repr`` for that reason since the
    boot gate landed; these rows were the exception.
    """
    hostile = tmp_path / "two\nlines"
    if kind == "loop":
        hostile.symlink_to(hostile)
    else:
        hostile.mkdir()
    other = "library: library.db" if key == "directory" else f"directory: {tmp_path}"
    quoted = json.dumps(str(hostile))  # a YAML double-quoted scalar, \n and all

    r = client.post(
        "/api/config/validate",
        json={"yaml_text": f"{key}: {quoted}\n{other}\n"},
    )

    assert r.status_code == 200, r.text
    row = next(e for e in r.json()["errors"] if e["loc"] == key)
    assert fragment in row["msg"], row
    assert "\n" not in row["msg"], row  # the escape, not the byte
    assert repr(str(hostile)) in row["msg"], row


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        ("''", "cannot open as a database"),
        ("{beets_dir}", "is a directory"),
    ],
)
def test_a_library_value_beets_cannot_open_is_refused_at_validate(
    client: TestClient, beets_library: LibraryHandle, value: str, fragment: str
) -> None:
    """``library:`` had no validator at all, so both of these linted CLEAN.

    Measured on the parent commit: zero rows, Save wrote the document, the Apply
    after answered 500 ("unable to open database file") and the next cold start
    died inside beets' ``_create_connection`` with a traceback. An empty value is
    ``Path(".")`` — the beets data directory itself — and a directory is a
    directory; SQLite opens neither.
    """
    music = Path(beets_library.lib.directory.decode())
    written = value.format(beets_dir=beets_library.beets_dir)
    r = client.post(
        "/api/config/validate",
        json={"yaml_text": f"directory: {music}\nlibrary: {written}\n"},
    )

    assert r.status_code == 200, r.text
    rows = r.json()["errors"]
    assert any(e["loc"] == "library" and fragment in e["msg"] for e in rows), rows


def test_the_library_value_beets_actually_ships_still_passes(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """The control for the pair above: ``library: library.db`` is beets' own default.

    A validator that refused every relative value, or every value whose parent is
    a directory, would refuse the shipped config on the first Save.
    """
    music = Path(beets_library.lib.directory.decode())
    r = client.post("/api/config/validate", json={"yaml_text": _yaml_pointing_at(music)})
    assert r.status_code == 200, r.text
    assert r.json()["errors"] == []


# --------------------------------------------------------------------------
# The include reproduction, checked against BEETS rather than against itself.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "include_block",
    [
        "",  # no include: at all
        "include:\n  - overlay.yaml\n",  # the overlay wins
        "include:\n  - gone.yaml\n",  # beets prints and carries on
        "include:\n  - gone.yaml\n  - overlay.yaml\n",  # one absent, one not
        "include:\n  - overlay.yaml\n  - second.yaml\n",  # the LAST one wins
    ],
)
def test_the_include_reproduction_agrees_with_a_real_beets_startup(
    tmp_path: Path, include_block: str
) -> None:
    """``effective_config_paths`` against ``setup_beets`` over the same file.

    Every other include test asserts the app against itself, so a change to
    beets' ``IncludeLazyConfig.read`` — the loop this function reproduces from
    ``beets/__init__.py:29-38`` — would leave the whole suite green while the
    gate silently evaluated a document beets does not load. This is the only test
    that would go red for that.
    """
    import beets

    from app.beets.library import close_library
    from app.beets.setup import open_beets, read_beets_config
    from app.beets.store_layout import effective_config_paths

    beets_dir = tmp_path / "beets"
    beets_dir.mkdir()
    music = tmp_path / "music"
    music.mkdir()
    (beets_dir / "overlay.yaml").write_text(f"directory: {tmp_path / 'from-overlay'}\n")
    (beets_dir / "second.yaml").write_text(f"directory: {tmp_path / 'from-second'}\n")
    text = f"directory: {music}\nlibrary: library.db\n{include_block}"
    (beets_dir / "config.yaml").write_text(text, encoding="utf-8")

    # beets' own read and open, without the boot's refusal of a skipped include.
    handle = open_beets(read_beets_config(str(beets_dir)))
    try:
        from_beets = (
            beets.config["directory"].as_filename(),
            beets.config["library"].as_filename(),
        )
    finally:
        close_library(handle.lib)

    document = load_config_text(text)
    paths = effective_config_paths(document, beets_dir)
    reproduced = (paths.directory, paths.library)
    assert reproduced == from_beets


@pytest.mark.parametrize("shape", ["directory", "socket", "dev-null", "symlink-to-regular"])
def test_the_include_reproduction_agrees_with_beets_on_the_shapes_it_tolerates(
    tmp_path: Path, shape: str
) -> None:
    """The four an ``S_ISREG`` test refused and a real ``setup_beets`` survives.

    Measured at the parent commit: beets booted on every one of them while the
    gate answered a lint row, so Validate painted a config beets loads and Apply
    refused it with 422. The last shape is the one that matters most — beets
    FOLLOWS a symlinked include and merges it, so the gate has to read it too.
    """
    import beets

    from app.beets.library import close_library
    from app.beets.setup import open_beets, read_beets_config
    from app.beets.store_layout import effective_config_paths

    beets_dir = tmp_path / "beets"
    beets_dir.mkdir()
    music = tmp_path / "music"
    music.mkdir()
    name = "overlay.yaml"
    target = beets_dir / name
    if shape == "directory":
        target.mkdir()
    elif shape == "socket":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(target))
        finally:
            sock.close()
    elif shape == "dev-null":
        name = "/dev/null"
    else:
        (beets_dir / "real.yaml").write_text(f"directory: {tmp_path / 'from-link'}\n")
        target.symlink_to(beets_dir / "real.yaml")

    text = f"directory: {music}\nlibrary: library.db\ninclude:\n  - {name}\n"
    (beets_dir / "config.yaml").write_text(text, encoding="utf-8")

    # beets' own read and open, without the boot's refusal of a skipped include.
    handle = open_beets(read_beets_config(str(beets_dir)))
    try:
        from_beets = (
            beets.config["directory"].as_filename(),
            beets.config["library"].as_filename(),
        )
    finally:
        close_library(handle.lib)

    paths = effective_config_paths(load_config_text(text), beets_dir)
    reproduced = (paths.directory, paths.library)
    assert reproduced == from_beets


@pytest.mark.parametrize("shape", ["directory", "socket", "dev-null", "symlink-to-dir"])
def test_an_include_beets_drops_is_an_error_at_validate(
    client: TestClient, beets_library: LibraryHandle, shape: str
) -> None:
    """Four shapes a real beets START survives, and three of them it skips.

    Measured against beets' own read and open over the same file: each one
    makes beets print one stderr line (``/dev/null`` not even that) and load the
    document's own ``directory:``. Since the owner ruling of 2026-09-21, Apply
    and boot refuse the three beets prints; since 2026-09-25 Validate and Save
    do too. None of them is a store-layout row.
    """
    music = Path(beets_library.lib.directory.decode())
    name = "overlay.yaml"
    target = beets_library.beets_dir / name
    if shape == "directory":
        target.mkdir()
    elif shape == "socket":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(target))
        finally:
            sock.close()
    elif shape == "dev-null":
        name = "/dev/null"
    else:
        (beets_library.beets_dir / "adir").mkdir()
        target.symlink_to(beets_library.beets_dir / "adir")

    text = _with_include(music, name)

    assert _layout_rows(client, text) == []
    if shape == "dev-null":
        # ``open`` succeeds and the read is empty, which is a merge of nothing —
        # beets does exactly that, silently, so there is nothing to refuse.
        assert _skipped_rows(client, text) == []
    else:
        reason = "No such device or address" if shape == "socket" else "Is a directory"
        assert _skipped_rows(client, text) == [f"beets would skip the include {name!r}: {reason}"]


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
def test_a_skipped_include_names_why_at_validate(
    client: TestClient, beets_library: LibraryHandle, shape: str, reason: str
) -> None:
    """Owner ruling 2026-09-21: a YAML error in the refusal carries its line number."""
    bad = beets_library.beets_dir / "bad.yaml"
    bad.write_text("a: 1\nfoo: [unclosed\n", encoding="utf-8")
    if shape == "permission":
        bad.chmod(0)
    text = _with_include(Path(beets_library.lib.directory.decode()), "bad.yaml")

    assert _skipped_rows(client, text) == [f"beets would skip the include 'bad.yaml': {reason}"]


@pytest.mark.parametrize(
    ("entry", "shown", "reason"),
    [
        ('"m\\nFAKE \\e[31mx.yaml"', "'m\\nFAKE \\x1b[31mx.yaml'", "No such file or directory"),
        ("''", "''", "Is a directory"),
    ],
    ids=["control-characters", "empty"],
)
def test_a_skipped_include_is_named_escaped_at_validate(
    client: TestClient, beets_library: LibraryHandle, entry: str, shown: str, reason: str
) -> None:
    """Named raw before: a newline and an ESC reached the row, and an empty
    entry read "the include  (Is a directory)"."""
    text = _with_include(Path(beets_library.lib.directory.decode()), entry)

    assert _skipped_rows(client, text) == [f"beets would skip the include {shown}: {reason}"]


def test_a_symlinked_include_is_followed_the_way_beets_follows_it(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """``O_NOFOLLOW`` would have made this file invisible to the gate.

    Measured against a real ``setup_beets``: beets follows the link and the
    overlay's ``directory:`` wins. So the link has to be READ here — refusing it
    would hide the override the gate exists to catch, which is the opposite of
    safe.
    """
    music = Path(beets_library.lib.directory.decode())
    (beets_library.beets_dir / "real.yaml").write_text(
        f"directory: {beets_library.beets_dir}\n", encoding="utf-8"
    )
    (beets_library.beets_dir / "link.yaml").symlink_to(beets_library.beets_dir / "real.yaml")

    rows = _layout_rows(client, _with_include(music, "link.yaml"))

    assert len(rows) == 1, rows
    assert rows[0]["loc"] == "directory", rows


def test_the_gate_reads_an_include_through_one_descriptor(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``set_file``: the gate opens the include ONCE and parses those bytes.

    ``os.stat`` then confuse's ``open`` asked the same NAME twice. Measured on
    the parent commit with a symlink flipped between the two calls, the FIFO hang
    the stat existed to prevent came back: the read did not return until a
    3-second alarm interrupted it. This asserts the second lookup is gone by
    making it fail loudly.
    """
    import confuse

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the gate re-opened the include by name")

    monkeypatch.setattr(confuse.Configuration, "set_file", _refuse)
    music = Path(beets_library.lib.directory.decode())
    (beets_library.beets_dir / "overlay.yaml").write_text(
        f"directory: {beets_library.beets_dir}\n", encoding="utf-8"
    )

    rows = _layout_rows(client, _with_include(music, "overlay.yaml"))

    assert len(rows) == 1, rows  # the overlay was merged, so its directory: won


def test_the_cap_bounds_the_read_and_not_the_reported_size(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """``/dev/zero`` reports ``st_size`` 0 and yields bytes without end.

    Every ``/proc`` file reports 0 too: measured, ``include: [/proc/kallsyms]``
    read 22,162,180 bytes through a 1,048,576-byte ``st_size`` check, at +34 MiB
    of RSS per authenticated request. The read is what has to stop.
    """
    music = Path(beets_library.lib.directory.decode())

    rows = _layout_rows(client, _with_include(music, "/dev/zero"))

    assert len(rows) == 1, rows
    assert rows[0]["loc"] == "include", rows


def test_one_request_reads_a_bounded_number_of_includes(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """The list is what multiplies the per-file cap, so the list is bounded too.

    Measured with the cap per entry: 25 entries naming one 1 MiB file answered
    200 after 12.5 seconds of threadpool CPU, from a 2,964-byte body. The control
    is the same document one entry shorter, which still merges.
    """
    music = Path(beets_library.lib.directory.decode())
    for index in range(33):
        (beets_library.beets_dir / f"o{index}.yaml").write_text("x: 1\n", encoding="utf-8")
    names = [f"o{index}.yaml" for index in range(33)]

    rows = _layout_rows(client, _with_include(music, *names))

    assert len(rows) == 1, rows
    assert rows[0]["loc"] == "include", rows
    assert "lists 33 files" in str(rows[0]["msg"]), rows
    assert _layout_rows(client, _with_include(music, *names[:32])) == []


def test_one_request_reads_a_bounded_total_of_include_bytes(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """Two files that each fit, and together do not.

    The budget is one request's, not one file's: 0.5 s per MiB is ruamel
    parsing, so what has to be bounded is the sum. The control is the first file
    alone, which is well inside it.
    """
    music = Path(beets_library.lib.directory.decode())
    for name in ("big1.yaml", "big2.yaml"):
        (beets_library.beets_dir / name).write_text(
            f"{name[:4]}: {'x' * (600 * 1024)}\n", encoding="utf-8"
        )

    rows = _layout_rows(client, _with_include(music, "big1.yaml", "big2.yaml"))

    assert len(rows) == 1, rows
    assert rows[0]["loc"] == "include", rows
    assert "budget" in str(rows[0]["msg"]), rows
    assert _layout_rows(client, _with_include(music, "big1.yaml")) == []


@pytest.mark.parametrize(
    ("shape", "reason"),
    [
        ("fifo", "is a FIFO; beets would block on it"),
        ("oversized", "takes the include: list over its 1048576-byte budget"),
        ("terminal", "had nothing to read"),
        ("mistyped-tag", "raised KeyError"),
    ],
)
def test_an_include_the_gate_cannot_use_is_named_as_written(
    client: TestClient, beets_library: LibraryHandle, shape: str, reason: str
) -> None:
    """Round 8 named these four by the resolved absolute path; the others as written."""
    music = Path(beets_library.lib.directory.decode())
    target = beets_library.beets_dir / "overlay.yaml"
    terminal: tuple[int, int] | None = None
    if shape == "fifo":
        os.mkfifo(target)
    elif shape == "oversized":
        with target.open("wb") as fh:
            fh.truncate(2 << 20)
    elif shape == "terminal":
        # A terminal with nothing typed: a non-blocking read answers EAGAIN.
        terminal = os.openpty()
        target.symlink_to(os.ttyname(terminal[1]))
    else:
        target.write_text("x: !!bool ture\n", encoding="utf-8")

    try:
        rows = _layout_rows(client, _with_include(music, "overlay.yaml"))
    finally:
        for fd in terminal or ():
            os.close(fd)

    assert [r["msg"] for r in rows] == [
        f"`include:` in config.yaml could not be read: 'overlay.yaml' {reason}."
        " Fix the include: list."
    ], rows


def test_a_repeated_include_entry_is_read_once(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """25 entries naming one file open it once, and it still wins the merge.

    Counted at the reader, because the cost this bounds is the read and the
    parse. The entry stays in the list at its own position — the LAST include
    decides ``directory:``, so a dedupe that dropped the repeat would change
    which file that is, which the second half asserts.
    """
    from app.beets import store_layout

    music = Path(beets_library.lib.directory.decode())
    (beets_library.beets_dir / "first.yaml").write_text("directory: /tmp/first\n", encoding="utf-8")
    (beets_library.beets_dir / "again.yaml").write_text(
        f"directory: {beets_library.beets_dir}\n", encoding="utf-8"
    )
    reads: list[str] = []
    real = store_layout._include_source

    def counted(target: str, written: str, budget: int) -> tuple[object, int]:
        reads.append(target)
        return real(target, written, budget)

    monkeypatch.setattr(store_layout, "_include_source", counted)

    rows = _layout_rows(
        client, _with_include(music, *(["again.yaml", "first.yaml"] * 12), "again.yaml")
    )

    assert sorted(reads) == sorted(
        {str(beets_library.beets_dir / name) for name in ("again.yaml", "first.yaml")}
    )
    # ``again.yaml`` is last, so its ``directory:`` — the beets data dir — is the
    # one the rule refuses.
    assert len(rows) == 1, rows


@pytest.mark.parametrize("value", ["", "."])
def test_apply_refuses_an_on_disk_library_that_names_a_directory(
    client: TestClient, beets_library: LibraryHandle, value: str
) -> None:
    """Two on-disk values SQLite cannot open, both reachable by a hand edit.

    Measured at the parent commit: both answered 500 "Apply failed during rebuild:
    unable to open database file" with the recovery "Restart MusicDrop. The saved
    config is on disk; cold start will load it." — and the cold start died on the
    same file, logging "refusing to start". The recovery sentence promised a
    restart that could not help.

    The empty value is the second spelling: confuse resolves it to the beets data
    directory, which is a directory too. Save and Validate already refuse both
    (``_library_file``), so only a hand edit gets here.
    """
    from app.main import app

    music = Path(beets_library.lib.directory.decode())
    spelling = f"library: {value}\n" if value else "library: ''\n"
    beets_library.config_path.write_text(f"directory: {music}\n{spelling}", encoding="utf-8")
    handle_before = app.state.beets_library

    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message"] == (
        "Apply refused: `library:` in config.yaml is a directory"
    )
    assert "has to name the beets database file" in r.json()["detail"]["recovery"]
    assert app.state.beets_library is handle_before


def test_a_library_that_is_a_directory_draws_one_row_at_validate(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """The schema already paints this key, so the layout row is suppressed.

    Measured: without the suppression the editor shows two rows on ``library:``
    saying the same thing. ``unusable_value`` is what marks a refusal about ONE
    value as droppable; a layout refusal (two paths, one loss) never is.
    """
    music = Path(beets_library.lib.directory.decode())
    text = f"directory: {music}\nlibrary: {beets_library.beets_dir}\n"

    r = client.post("/api/config/validate", json={"yaml_text": text})

    assert r.status_code == 200, r.text
    rows = [e for e in r.json()["errors"] if e["loc"] == "library"]
    assert len(rows) == 1, rows
    assert rows[0]["type"] != "store_layout", rows


def test_a_mistyped_bool_tag_names_its_error_at_validate_and_save(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """ruamel raises ``KeyError('ture')``; the bare ``'ture'`` said nothing."""
    text = "a: !!bool ture\n"

    r = client.post("/api/config/validate", json={"yaml_text": text})
    assert [e["msg"] for e in r.json()["errors"]] == ["KeyError: 'ture'"], r.json()

    saved = client.post(
        "/api/config/save",
        json={"yaml_text": text, "base_sha256": _sha(beets_library.config_path)},
    )
    assert [e["msg"] for e in saved.json()["detail"]] == ["KeyError: 'ture'"], saved.text


def test_a_mistyped_bool_tag_in_an_include_is_a_row_at_validate_and_a_422_at_save(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """PyYAML reads the include; the ``KeyError`` was a bare 500 on both routes."""
    bad = beets_library.beets_dir / "bad.yaml"
    bad.write_text("x: !!bool ture\n", encoding="utf-8")
    text = _with_include(Path(beets_library.lib.directory.decode()), "bad.yaml")
    row = (
        "`include:` in config.yaml could not be read: 'bad.yaml' raised KeyError."
        " Fix the include: list."
    )

    rows = _layout_rows(client, text)
    assert [r["msg"] for r in rows] == [row], rows

    saved = client.post(
        "/api/config/save",
        json={"yaml_text": text, "base_sha256": _sha(beets_library.config_path)},
    )
    assert saved.status_code == 422, saved.text
    assert [e["msg"] for e in saved.json()["detail"]] == [row], saved.text
