"""The rename's playlist collateral: .m3u8 re-export for affected playlists."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from beets.library import Library

from app.beets.library import _require_id
from app.playlists import store
from app.playlists.store import StoredEntry
from tests.conftest import make_test_handle


@pytest.mark.anyio
async def test_reexports_only_playlists_containing_the_items(
    rename_lib: Library, tmp_path: Path
) -> None:
    from app.playlists.reexport import reexport_playlists_containing

    handle = make_test_handle(rename_lib, tmp_path)
    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()

    items = sorted(rename_lib.items(), key=lambda i: str(i.title))
    fayrouz_item = next(i for i in items if str(i.albumartist) == "Fayrouz")
    fairuz_item = next(i for i in items if str(i.albumartist) == "Fairuz")

    def entry(item: object) -> StoredEntry:
        return StoredEntry(uid=os.urandom(8).hex(), item_id=_require_id(item.id))  # type: ignore[attr-defined]  # beets Item is untyped

    affected = store.create_playlist(playlists_dir, name="Affected", entries=[entry(fayrouz_item)])
    untouched = store.create_playlist(playlists_dir, name="Untouched", entries=[entry(fairuz_item)])

    count = await reexport_playlists_containing(
        {_require_id(fayrouz_item.id)}, handle, playlists_dir
    )

    assert count == 1
    export_dir = Path(os.fsdecode(rename_lib.directory)) / ".playlists"
    assert (export_dir / f"{affected.id}.m3u8").exists()
    assert not (export_dir / f"{untouched.id}.m3u8").exists()


@pytest.mark.anyio
async def test_empty_item_set_never_lists_playlists(
    rename_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty id set must return 0 WITHOUT touching the playlist store."""
    from app.playlists.reexport import reexport_playlists_containing

    def boom(_dir: Path) -> list[store.StoredPlaylist]:
        raise AssertionError("empty id set must not touch the playlist store")

    monkeypatch.setattr(store, "list_playlists", boom)
    handle = make_test_handle(rename_lib, tmp_path)
    assert await reexport_playlists_containing(set(), handle, tmp_path / "playlists") == 0


@pytest.mark.anyio
async def test_export_failure_does_not_abort_the_fan_out(
    rename_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing export is swallowed per playlist: the rest still export and the
    count still counts the attempt (best-effort semantics).

    Patched on ``app.playlists.reexport`` — the ONE implementation both the async
    wrapper here and the loop-less worker threads go through — so the pin covers
    both entrances rather than only the request path.
    """
    import app.playlists.reexport as reexport_core
    from app.playlists.reexport import reexport_playlists_containing

    handle = make_test_handle(rename_lib, tmp_path)
    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()

    items = sorted(rename_lib.items(), key=lambda i: str(i.title))
    fayrouz_item = next(i for i in items if str(i.albumartist) == "Fayrouz")
    iid = _require_id(fayrouz_item.id)
    store.create_playlist(playlists_dir, name="A", entries=[StoredEntry(uid="a1", item_id=iid)])
    store.create_playlist(playlists_dir, name="B", entries=[StoredEntry(uid="b1", item_id=iid)])

    calls: list[str] = []

    def raise_render(record: object, lib: object, export_dir: object) -> None:
        calls.append("render")
        raise OSError("disk full")

    monkeypatch.setattr(reexport_core, "render_export", raise_render)
    assert await reexport_playlists_containing({iid}, handle, playlists_dir) == 2
    assert len(calls) == 2  # the patched renderer really intercepted both exports
