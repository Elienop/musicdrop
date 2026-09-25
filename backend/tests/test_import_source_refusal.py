"""An import start refuses the library, MusicDrop's own folders and slskd's whole folder.

beets refuses no source for where it is (``import_func`` checks only that the
path exists), so a source that holds the library made beets walk the library's
own albums as candidates, re-tag them and move them (BACKLOG, measured
2026-09-19). The refusal is asked in ``BeetsImportRunner.validate``, before a
slot is taken, by every start: ``POST /api/import``, both inbox routes, the
slskd drain and the bank apply runner.

Every refusal below has a control beside it: what the same rule must let
through (a folder inside the library, inside slskd's folder, or beside either).
The layout is built under one tmp ``root`` standing in for ``/``, the shape the
image ships: ``/media/music`` and ``/data/beets``.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import pytest
from beets.library import Library
from fastapi.testclient import TestClient

from app.acquisition.ledger import AcquisitionLedger
from app.acquisition.queue import AcquisitionQueue
from app.bank import store
from app.bank.apply_runner import BankApplyRunner
from app.bank.fingerprint import folder_fingerprint
from app.beets.library import LibraryHandle
from app.beets.store_layout import (
    SOURCE_HOLDS_APP_DATA,
    SOURCE_IS_THE_INBOX,
    SOURCE_IS_THE_LIBRARY,
)
from app.config import Settings
from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import ImportJobRegistry, reset_registry
from app.import_jobs.runner import (
    BeetsImportRunner,
    ImportSourceRefusedError,
    InLibraryCopyError,
    SourcePathMissingError,
)
from app.main import app
from app.models.bank import BankDecision
from app.models.import_models import ImportOptions
from tests.conftest import build_library

T = TypeVar("T")

_SENTINEL = object()


@dataclass(frozen=True)
class _Layout:
    root: Path
    music: Path
    beets: Path
    trash: Path
    origins: Path
    settings: Settings
    lib: Library

    def runner(self) -> BeetsImportRunner:
        return BeetsImportRunner(
            self.lib, self.trash, self.origins, settings=self.settings, beets_dir=self.beets
        )


def _layout(
    tmp_path: Path,
    *,
    trash: str = "",
    library_db: str = "",
    **stores: str,
) -> _Layout:
    """``<root>/media/music`` and ``<root>/data/beets``, with any store moved.

    Every store the refusal lists is set explicitly, so no ``.env`` value can
    move one; an empty string is the setting's own default. The two image
    caches sit under ``<root>/data/cache`` rather than the repo root.
    """
    root = tmp_path / "root"
    music = root / "media" / "music"
    music.mkdir(parents=True, exist_ok=True)
    (music / ".keep").write_bytes(b"")  # a real root has entries
    beets = root / "data" / "beets"
    beets.mkdir(parents=True)
    fields = {
        "beets_dir": str(beets),
        "bank_dir": "",
        "plex_settings_dir": "",
        "slskd_settings_dir": "",
        "playlists_dir": "",
        "inbox_dir": "",
        "playlists_export_dir": "",
        "artist_image_cache_dir": str(root / "data" / "cache" / "artist-images"),
        "cover_thumb_cache_dir": str(root / "data" / "cache" / "cover-thumbs"),
    }
    fields.update(stores)
    settings = Settings(**fields)  # type: ignore[arg-type]  # str fields, set by name
    lib = build_library(library_db or str(beets / "library.db"), str(music))
    return _Layout(
        root=root,
        music=music,
        beets=beets,
        trash=Path(trash) if trash else beets / "trash",
        origins=beets / "trash-origins",
        settings=settings,
        lib=lib,
    )


def _folder(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _refusal(layout: _Layout, *paths: Path, options: ImportOptions | None = None) -> str | None:
    """The sentence ``validate`` refuses with, or ``None`` when it lets the start through."""
    try:
        layout.runner().validate([str(p) for p in paths], options)
    except ImportSourceRefusedError as exc:
        return str(exc)
    return None


# ----- refused: the library -------------------------------------------------------


def test_the_library_root_is_refused(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    assert _refusal(layout, layout.music) == SOURCE_IS_THE_LIBRARY


def test_a_parent_of_the_library_is_refused(tmp_path: Path) -> None:
    """The 2026-09-19 case: beets walked the library's own albums as candidates."""
    layout = _layout(tmp_path)
    assert _refusal(layout, layout.root / "media") == SOURCE_IS_THE_LIBRARY


def test_the_filesystem_root_is_refused_with_the_library_sentence(tmp_path: Path) -> None:
    """``/`` holds the beets dir too; the library sentence is asked first."""
    layout = _layout(tmp_path)
    assert _refusal(layout, layout.root) == SOURCE_IS_THE_LIBRARY


def test_a_symlink_to_the_library_is_refused(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    link = _folder(layout.root / "downloads") / "library-link"
    link.symlink_to(layout.music)
    assert _refusal(layout, link) == SOURCE_IS_THE_LIBRARY


def test_one_refused_member_refuses_the_whole_list(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    fine = _folder(layout.root / "downloads" / "Album")
    assert _refusal(layout, fine, layout.music) == SOURCE_IS_THE_LIBRARY


# ----- refused: MusicDrop's own folders -------------------------------------------


def test_the_beets_dir_and_its_parent_are_refused(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    assert _refusal(layout, layout.beets) == SOURCE_HOLDS_APP_DATA
    assert _refusal(layout, layout.root / "data") == SOURCE_HOLDS_APP_DATA


def test_a_symlink_to_the_beets_dir_is_refused(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    link = _folder(layout.root / "downloads") / "beets-link"
    link.symlink_to(layout.beets)
    assert _refusal(layout, link) == SOURCE_HOLDS_APP_DATA


def test_the_trash_and_an_entry_in_it_are_refused(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    entry = _folder(layout.trash / "Album")
    assert _refusal(layout, layout.trash) == SOURCE_HOLDS_APP_DATA
    assert _refusal(layout, entry) == SOURCE_HOLDS_APP_DATA


def test_an_entry_of_a_trash_inside_the_library_is_refused(tmp_path: Path) -> None:
    """The nearest container wins: ``<library>/.trash`` is met before the library."""
    layout = _layout(tmp_path, trash=str(tmp_path / "root" / "media" / "music" / ".trash"))
    entry = _folder(layout.music / ".trash" / "Album")
    assert _refusal(layout, entry) == SOURCE_HOLDS_APP_DATA


def test_a_folder_in_the_playlist_exports_inside_the_library_is_refused(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    mix = _folder(layout.music / ".playlists" / "Mix")
    assert _refusal(layout, mix) == SOURCE_HOLDS_APP_DATA


def test_the_bank_where_it_is_configured_is_refused(tmp_path: Path) -> None:
    """Outside the beets dir, so this row is what refuses, not the beets dir's."""
    stores = tmp_path / "root" / "stores"
    layout = _layout(tmp_path, bank_dir=str(stores / "bank"))
    inside = _folder(stores / "bank" / "rows")
    assert _refusal(layout, stores / "bank") == SOURCE_HOLDS_APP_DATA
    assert _refusal(layout, stores) == SOURCE_HOLDS_APP_DATA
    assert _refusal(layout, inside) == SOURCE_HOLDS_APP_DATA


def test_a_folder_holding_an_image_cache_is_refused(tmp_path: Path) -> None:
    cache = tmp_path / "root" / "cache" / "thumbs"
    layout = _layout(tmp_path, cover_thumb_cache_dir=str(cache))
    cache.mkdir(parents=True)
    assert _refusal(layout, cache.parent) == SOURCE_HOLDS_APP_DATA


def test_an_inbox_equal_to_the_beets_dir_is_refused_as_app_data(tmp_path: Path) -> None:
    layout = _layout(tmp_path, inbox_dir=str(tmp_path / "root" / "data" / "beets"))
    assert _refusal(layout, layout.beets) == SOURCE_HOLDS_APP_DATA


def test_a_link_inside_slskds_folder_into_the_trash_is_refused(tmp_path: Path) -> None:
    """Asked of where the source RESOLVES: its spelled parent is slskd's folder."""
    inbox = tmp_path / "root" / "downloads" / "slskd"
    layout = _layout(tmp_path, inbox_dir=str(inbox))
    entry = _folder(layout.trash / "Album")
    link = _folder(inbox) / "Album"
    link.symlink_to(entry)
    assert _refusal(layout, link) == SOURCE_HOLDS_APP_DATA


# ----- refused: slskd's whole folder ----------------------------------------------


def test_slskds_whole_folder_is_refused(tmp_path: Path) -> None:
    inbox = tmp_path / "root" / "downloads" / "slskd"
    layout = _layout(tmp_path, inbox_dir=str(inbox))
    inbox.mkdir(parents=True)
    assert _refusal(layout, inbox) == SOURCE_IS_THE_INBOX


def test_an_inbox_that_holds_the_library_is_refused_with_the_library_sentence(
    tmp_path: Path,
) -> None:
    """``MUSICDROP_INBOX_DIR=/media`` opens no door to ``/media``."""
    layout = _layout(tmp_path, inbox_dir=str(tmp_path / "root" / "media"))
    assert _refusal(layout, layout.root / "media") == SOURCE_IS_THE_LIBRARY


# ----- allowed: the controls ------------------------------------------------------


def test_a_folder_inside_the_library_still_starts(tmp_path: Path) -> None:
    """Today's re-import; the copy guard after it still refuses an explicit copy."""
    layout = _layout(tmp_path)
    album = _folder(layout.music / "Artist" / "Album")
    assert _refusal(layout, album) is None
    runner, paths, copy = layout.runner(), [str(album)], ImportOptions(operation="copy")
    with pytest.raises(InLibraryCopyError):
        runner.validate(paths, copy)


def test_a_folder_inside_the_default_inbox_still_starts(tmp_path: Path) -> None:
    """``<beets>/inbox`` is met before the beets dir, so its albums are not app data."""
    layout = _layout(tmp_path)
    album = _folder(layout.beets / "inbox" / "Album")
    assert _refusal(layout, album) is None


def test_a_parent_of_slskds_folder_that_holds_nothing_else_still_starts(tmp_path: Path) -> None:
    downloads = tmp_path / "root" / "downloads"
    layout = _layout(tmp_path, inbox_dir=str(downloads / "slskd"))
    _folder(downloads / "slskd" / "Album")
    assert _refusal(layout, downloads) is None


def test_an_unrelated_folder_still_starts(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    album = _folder(layout.root / "downloads" / "yubal" / "Album")
    assert _refusal(layout, album) is None


def test_a_sibling_whose_name_extends_the_librarys_still_starts(tmp_path: Path) -> None:
    """Whole names, not text: ``/media/music2`` is not inside ``/media/music``."""
    layout = _layout(tmp_path)
    album = _folder(layout.root / "media" / "music2" / "Album")
    assert _refusal(layout, album) is None


def test_a_folder_beside_the_library_db_in_a_wide_folder_still_starts(tmp_path: Path) -> None:
    """``library: /media/library.db`` makes ``/media`` the database's folder.

    Sitting inside that folder proves nothing, so it is only ever "is or holds".
    """
    media = tmp_path / "root" / "media"
    media.mkdir(parents=True)
    layout = _layout(tmp_path, library_db=str(media / "library.db"))
    album = _folder(media / "downloads" / "Album")
    assert _refusal(layout, album) is None


def test_a_folder_inside_the_library_still_starts_when_the_db_sits_in_its_root(
    tmp_path: Path,
) -> None:
    music = tmp_path / "root" / "media" / "music"
    music.mkdir(parents=True)
    layout = _layout(tmp_path, library_db=str(music / "library.db"))
    album = _folder(music / "Artist" / "Album")
    assert _refusal(layout, album) is None


def test_the_library_is_the_nearest_container_when_a_store_holds_it(tmp_path: Path) -> None:
    """A store around the library does not reach through it to its albums."""
    layout = _layout(tmp_path, playlists_dir=str(tmp_path / "root" / "media"))
    album = _folder(layout.music / "Artist" / "Album")
    assert _refusal(layout, album) is None


# ----- order ------------------------------------------------------------------------


def test_a_missing_folder_still_says_it_does_not_exist(tmp_path: Path) -> None:
    """Asked after the existence check: a typo inside the Trash is still a typo."""
    layout = _layout(tmp_path)
    runner, paths = layout.runner(), [str(layout.trash / "missing")]
    with pytest.raises(SourcePathMissingError) as exc:
        runner.validate(paths, None)
    assert str(exc.value) == "That folder doesn’t exist."


# ----- the callers --------------------------------------------------------------------


def _poll(get: Callable[[], T], pred: Callable[[T], bool], *, timeout: float = 5.0) -> T:
    deadline = time.monotonic() + timeout
    value = get()
    while time.monotonic() < deadline and not pred(value):
        time.sleep(0.01)
        value = get()
    return value


def test_the_drain_survives_a_refused_folder_and_records_it_failed(tmp_path: Path) -> None:
    """The drain catches named types only; an uncaught refusal would end its thread."""
    fake = FakeImportRunner()
    fake.validate_error = ImportSourceRefusedError(SOURCE_IS_THE_LIBRARY)
    ledger = AcquisitionLedger(tmp_path / "ledger.json")
    queue = AcquisitionQueue(
        import_registry=ImportJobRegistry(runner=fake),
        ledger=ledger,
        poll_interval=0.01,
        busy_backoff=0.02,
    )
    folder = _folder(tmp_path / "inbox" / "Album")

    queue._process_one(folder)

    status = queue.status()
    assert (status.failed, status.processed, status.error) == (1, 1, SOURCE_IS_THE_LIBRARY)
    assert ledger.entries() == []


def test_a_bank_row_on_slskds_whole_folder_fails_with_the_slskd_sentence(tmp_path: Path) -> None:
    """Through the real runner: a banked decision there would answer for every album."""
    inbox = tmp_path / "root" / "downloads" / "slskd"
    layout = _layout(tmp_path, inbox_dir=str(inbox))
    inbox.mkdir(parents=True)
    (inbox / "01 Track.mp3").write_bytes(b"x" * 64)
    reg = ImportJobRegistry()
    reg.attach_library(
        layout.lib,
        layout.trash,
        trash_origins_dir=layout.origins,
        settings=layout.settings,
        beets_dir=layout.beets,
    )
    bank_dir = tmp_path / "bank-rows"
    item = store.create_item(
        bank_dir,
        folder=str(inbox),
        source="sweep",
        reason="no_match",
        fingerprint=folder_fingerprint(inbox),
    )
    store.decide_item(bank_dir, item.id, BankDecision(action="asis"))

    def no_library() -> LibraryHandle:
        raise RuntimeError("this row must resolve without reading the library")

    runner = BankApplyRunner(
        bank_dir=bank_dir,
        import_registry=reg,
        library=no_library,
        poll_interval=0.01,
        busy_backoff=0.02,
        idle_poll=0.05,
    )
    runner.start()
    try:
        row = _poll(
            lambda: store.get_item(bank_dir, item.id),
            lambda r: r is not None and r.status == "failed",
        )
    finally:
        runner.stop()
    assert row is not None
    assert (row.status, row.error, row.error_recovery) == (
        "failed",
        SOURCE_IS_THE_INBOX,
        "fix_folder",
    )


def test_post_import_answers_the_refusal_as_422() -> None:
    fake = FakeImportRunner()
    fake.validate_error = ImportSourceRefusedError(SOURCE_HOLDS_APP_DATA)
    reset_registry(runner=fake)
    response = TestClient(app).post("/api/import", json={"path": "/data/beets"})
    assert response.status_code == 422
    assert response.json()["detail"] == SOURCE_HOLDS_APP_DATA


@contextmanager
def _inbox_state(inbox: Path) -> Iterator[None]:
    prior = getattr(app.state, "inbox_dir", _SENTINEL)
    app.state.inbox_dir = inbox
    try:
        yield
    finally:
        if prior is _SENTINEL:
            del app.state.inbox_dir
        else:
            app.state.inbox_dir = prior


def _settled_album(inbox: Path, name: str) -> Path:
    album = _folder(inbox / name)
    track = album / "01 track.flac"
    track.write_bytes(b"\0")
    old = time.time() - 3600
    os.utime(track, (old, old))
    os.utime(album, (old, old))
    return album


def test_both_inbox_routes_answer_the_refusal_as_422(tmp_path: Path) -> None:
    """An inbox that holds the library lists the library's own folder."""
    inbox = tmp_path / "inbox"
    _settled_album(inbox, "music")
    fake = FakeImportRunner()
    fake.validate_error = ImportSourceRefusedError(SOURCE_IS_THE_LIBRARY)
    reset_registry(runner=fake)
    with _inbox_state(inbox):
        client = TestClient(app)
        one = client.post("/api/acquisition/inbox/items/import", json={"name": "music"})
        every = client.post("/api/acquisition/review-inbox")
    assert (one.status_code, one.json()["detail"]) == (422, SOURCE_IS_THE_LIBRARY)
    assert (every.status_code, every.json()["detail"]) == (422, SOURCE_IS_THE_LIBRARY)


# ----- the wiring: both places the registry is handed its settings -------------------


def test_the_lifespan_hands_the_registry_what_the_refusal_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings

    music = tmp_path / "music"
    music.mkdir()
    (music / ".keep").write_bytes(b"")
    beets = tmp_path / "beets"
    beets.mkdir()
    (beets / "config.yaml").write_text(
        f"directory: {music}\nlibrary: library.db\nplugins:\n  - musicbrainz\n"
    )
    monkeypatch.setattr(settings, "beets_dir", str(beets))
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        response = client.post("/api/import", json={"path": str(beets)})
    assert (response.status_code, response.json()["detail"]) == (422, SOURCE_HOLDS_APP_DATA)


def test_a_config_apply_hands_the_registry_what_the_refusal_reads(client: TestClient) -> None:
    from app.import_jobs.registry import get_registry

    get_registry().attach_library(None, settings=None, beets_dir=None)
    assert client.post("/api/config/apply").status_code == 200
    handle: LibraryHandle = app.state.beets_library
    response = client.post("/api/import", json={"path": str(handle.beets_dir)})
    assert (response.status_code, response.json()["detail"]) == (422, SOURCE_HOLDS_APP_DATA)
