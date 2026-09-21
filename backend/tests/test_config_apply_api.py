"""End-to-end tests for ``POST /api/config/apply``.

Covers the one-click in-process reload: the handler holds an ``asyncio.Lock``
on ``app.state.beets_swap_lock`` and offloads the blocking rebuild
(``reset_beets_globals`` + ``setup_beets``) to FastAPI's threadpool, then
atomically swaps ``app.state.beets_library`` with the new handle. The 409
import-gate check uses ``get_registry()`` (the live binding — the autouse
``reset_import_registry`` fixture in ``conftest.py`` swaps the module global
between tests, so the handler must NOT import the name eagerly).

The 500 branch monkeypatches ``app.beets.config_editor.open_beets`` (the name
the handler captured at import time) — patching the source module would not
affect the already-bound symbol.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from beets import plugins
from beets.library import Item
from fastapi.testclient import TestClient

from app.beets.library import LibraryHandle


def test_apply_returns_snapshot_with_apply_pending_false(
    client: TestClient, beets_library_config_path: Path
) -> None:
    # Touch the file so apply_pending becomes true; the post-Apply snapshot
    # must report apply_pending=False because setup_beets() captured the
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


def test_apply_500_when_setup_beets_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.beets import config_editor

    def _boom(_read: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(config_editor, "open_beets", _boom)
    r = client.post("/api/config/apply")
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
    assert "message" in detail
    assert "boom" in detail["message"].lower()
    assert "recovery" in detail
    assert "restart" in detail["recovery"].lower()


def test_apply_of_an_unparseable_config_changes_nothing(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """A config.yaml beets cannot parse leaves the running process as it was.

    Measured before the read moved ahead of the teardown: plugins were cleared,
    the read failed, and writes went on with plugin-less templates —
    ``%the{$artist}`` came out literally in the destination.
    """
    from app.main import app

    cfg = beets_library.config_path
    music = Path(beets_library.lib.directory.decode())
    cfg.write_text(
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
    plugins_before = sorted(p.name for p in plugins.find_plugins())
    destination_before = item.destination()
    assert b"Beatles, The" in destination_before  # the control: ``the`` is live

    cfg.write_text("directory: [unclosed\n", encoding="utf-8")
    r = client.post("/api/config/apply")

    assert r.status_code == 422, r.text
    assert r.json()["detail"]["recovery"] == (
        "beets could not read config.yaml, so nothing was changed. Fix the file and Apply again."
    )
    assert app.state.beets_library is handle
    assert sorted(p.name for p in plugins.find_plugins()) == plugins_before
    assert item.destination() == destination_before
    assert client.get("/api/albums").status_code == 200
