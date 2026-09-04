"""The rename's playlist collateral: .m3u8 re-export for affected playlists."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from beets.library import Library

from app.beets.library import _require_id
from app.playlists import store
from app.playlists.store import StoredEntry
from tests.conftest import beets_dir_for, make_test_handle


@pytest.mark.anyio
async def test_reexports_only_playlists_containing_the_items(
    rename_lib: Library, tmp_path: Path
) -> None:
    from app.playlists.reexport import reexport_playlists_containing

    handle = make_test_handle(rename_lib, beets_dir_for(tmp_path))
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
    handle = make_test_handle(rename_lib, beets_dir_for(tmp_path))
    assert await reexport_playlists_containing(set(), handle, tmp_path / "playlists") == 0


@pytest.mark.anyio
async def test_export_failure_does_not_abort_the_fan_out(
    rename_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing export is swallowed per playlist: the rest still export, and a
    FAILED write is NOT counted — every surface asserts the count as
    "re-exported N", so counting attempts would report a repair that did not
    happen while the export still names the dead path.

    Patched on ``app.playlists.reexport`` — the ONE implementation both the async
    wrapper here and the loop-less worker threads go through — so the pin covers
    both entrances rather than only the request path.
    """
    import app.playlists.reexport as reexport_core
    from app.playlists.reexport import reexport_playlists_containing

    handle = make_test_handle(rename_lib, beets_dir_for(tmp_path))
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
    # Both writes failed, so the count is 0 — but len(calls) proves the second
    # export was still ATTEMPTED after the first raised (the fan-out survives).
    assert await reexport_playlists_containing({iid}, handle, playlists_dir) == 0
    assert len(calls) == 2  # the patched renderer really intercepted both exports


@pytest.mark.anyio
async def test_partial_export_failure_counts_only_the_written_playlist(
    rename_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One of two exports fails: the count is exactly the successful one."""
    import app.playlists.reexport as reexport_core
    from app.playlists.reexport import reexport_playlists_containing

    handle = make_test_handle(rename_lib, beets_dir_for(tmp_path))
    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()

    items = sorted(rename_lib.items(), key=lambda i: str(i.title))
    fayrouz_item = next(i for i in items if str(i.albumartist) == "Fayrouz")
    iid = _require_id(fayrouz_item.id)
    doomed = store.create_playlist(
        playlists_dir, name="A", entries=[StoredEntry(uid="a1", item_id=iid)]
    )
    store.create_playlist(playlists_dir, name="B", entries=[StoredEntry(uid="b1", item_id=iid)])

    real_render = reexport_core.render_export

    def raise_for_a(record: store.StoredPlaylist, lib: object, export_dir: Path) -> None:
        if record.id == doomed.id:
            raise OSError("disk full")
        real_render(record, lib, export_dir)

    monkeypatch.setattr(reexport_core, "render_export", raise_for_a)
    assert await reexport_playlists_containing({iid}, handle, playlists_dir) == 1
