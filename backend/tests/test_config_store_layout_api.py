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
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """``directory:`` pointed at the beets dir puts ``<B>/trash-origins`` in the library.

    ``loc`` and ``line`` matter as much as the refusal: the frontend paints the
    gutter from those two, and disables Save while any error row is present
    (SettingsBeetsPage) — so a row with no position would refuse the save with
    nothing on screen to explain it.
    """
    r = client.post(
        "/api/config/validate",
        json={"yaml_text": _yaml_pointing_at(beets_library.beets_dir)},
    )
    assert r.status_code == 200
    rows = [e for e in r.json()["errors"] if e["type"] == "store_layout"]
    assert len(rows) == 1, r.json()
    assert rows[0]["loc"] == "directory"
    assert rows[0]["line"] == 1
    assert "The music library contains the Trash origin store" in rows[0]["msg"]


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
    trash = beets_library.beets_dir / "bin"
    monkeypatch.setattr("app.config.settings.trash_dir", str(trash))
    r = client.post("/api/config/validate", json={"yaml_text": _yaml_pointing_at(trash / "music")})
    assert r.status_code == 200
    msgs = [e["msg"] for e in r.json()["errors"]]
    assert any("The Trash directory contains the music library" in m for m in msgs), msgs


# --------------------------------------------------------------------------
# POST /api/config/save — the write, refused BEFORE it happens.
# --------------------------------------------------------------------------


def test_save_refuses_the_same_document_the_gutter_flags(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """422 with the same sentence, and — the part that matters — nothing written.

    A config saved in this shape would also refuse to BOOT, so a Save that wrote
    it first and complained afterwards would leave the operator with a process
    that will not come back up after the next restart.
    """
    config_path = beets_library.config_path
    before = config_path.read_bytes()

    r = client.post(
        "/api/config/save",
        json={
            "yaml_text": _yaml_pointing_at(beets_library.beets_dir),
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
