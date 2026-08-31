"""Regression: MusicDrop's importer must store paths the way beets does.

beets stores an item's path RELATIVE to the music directory so the library
survives a move of that directory. It does the relativising in
``dbcore/pathutils.normalize_path_for_db``, which consults a ``music_dir``
ContextVar; ``Library.__init__`` arms that var **in the context that opens the
library and nowhere else**. MusicDrop opens the library in the FastAPI lifespan
and then runs every import on a fresh ``threading.Thread``, which starts with an
empty context -- so the importer stored ABSOLUTE paths on every run, from v0.1.0
through v0.34.0. Rows written that way point at the right file today and become
dead pointers the day the music directory moves (and ``beet update`` then drops
them from the DB while the audio survives).

These tests pin the write representation for BOTH callers of
``run_import_worker``: the web/rescan importer (via the real
``BeetsImportRunner``) and Trash restore (via ``restore_album``, which calls the
worker directly and would stay broken if the binding lived in the job runner).

TWO TRAPS make a naive version of this test pass on the unfixed code. Both were
reproduced before this test was written:

1. **Never assert on ``item.path``.** Reading a row back through beets inside a
   bound context re-expands the stored value, so an absolute row and a relative
   row are indistinguishable. Assert on the RAW SQLite blob through a plain
   ``sqlite3`` connection instead.
2. **Never run the import on the test's main thread.** ``build_library`` calls
   ``Library.__init__``, which arms the ContextVar for the calling context -- so
   an import driven inline is bound by accident and the bug is invisible. Both
   tests drive the import on a spawned thread, exactly like production.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest
from beets import config
from beets.library import Item, Library

from app.beets.import_session import ImportBridge
from app.beets.trash_manage import restore_album
from app.import_jobs.runner import BeetsImportRunner
from app.models.bank import BankApplyDirective
from app.models.import_models import ImportOptions
from tests.conftest import build_library, origins_for

SAMPLE = Path(__file__).parent / "fixtures" / "silent.flac"


@pytest.fixture(autouse=True)
def _serial_copy_mode() -> None:
    """Mirror the shipped beets default: serial pipeline, copy-mode imports."""
    config["threaded"] = False
    config["import"]["copy"] = True
    config["import"]["move"] = False


def _seed_album(album_dir: Path) -> Path:
    """Write a two-track tagged album under ``album_dir`` and return it.

    ASIS imports re-read tags off the files, so they must carry real
    album/artist/title/track for beets to compute a destination at all.
    """
    for track, title in ((1, "A"), (2, "B")):
        dst = album_dir / f"0{track} {title}.flac"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SAMPLE, dst)
        item = Item(album="Album", albumartist="Artist", artist="Artist", title=title, track=track)
        item.path = os.fsencode(str(dst))
        item.write()
    return album_dir


def _stored_paths(db_path: Path) -> list[bytes]:
    """Every ``items.path`` as the RAW bytes SQLite holds.

    Deliberately NOT through beets: ``Item.path`` re-expands a relative value
    against the bound music dir, which hides the very difference under test.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return [row[0] for row in con.execute("SELECT path FROM items ORDER BY id")]
    finally:
        con.close()


def _run_import(lib: Library, source: Path) -> None:
    """Drive one ASIS import through the REAL runner (which spawns the thread).

    ASIS + a directive keeps it hermetic: no candidate lookup, no network.
    """
    runner = BeetsImportRunner(lib, trash_dir=None, bank_dir=None)
    done = threading.Event()
    errors: list[str] = []

    def _on_error(message: str) -> None:
        errors.append(message)
        done.set()

    runner.run(
        [str(source)],
        ImportBridge(),
        on_finish=done.set,
        on_error=_on_error,
        options=ImportOptions(operation="default"),
        directive=BankApplyDirective(action="asis"),
    )
    assert done.wait(timeout=30.0), "import worker did not finish in time"
    assert not errors, f"import worker errored: {errors}"


def test_web_import_stores_music_dir_relative_paths(tmp_path: Path) -> None:
    """The web/rescan importer must write music-dir-relative item paths.

    Asserts on the raw SQLite blob (trap 1) after an import driven on a worker
    thread by the real runner (trap 2). Without the ``music_dir_context`` bind
    in ``run_import_worker`` the worker thread has no music dir bound and beets
    stores the absolute path verbatim.
    """
    music = tmp_path / "music"
    music.mkdir()
    db_path = tmp_path / "library.db"
    lib = build_library(str(db_path), str(music))
    source = _seed_album(tmp_path / "inbox" / "Artist - Album")

    _run_import(lib, source)

    stored = _stored_paths(db_path)
    assert stored, "nothing was imported -- the test proves nothing"
    assert stored == [b"Artist/Album/01 A.flac", b"Artist/Album/02 B.flac"], (
        f"item paths were not stored relative to the music dir. Raw SQLite blobs: {stored}"
    )
    # The failure mode this pins: an absolute row starts with a separator.
    for raw in stored:
        assert not raw.startswith(b"/"), f"absolute path stored in library.db: {raw!r}"


def test_trash_restore_stores_music_dir_relative_paths(tmp_path: Path) -> None:
    """Trash restore must write music-dir-relative item paths too.

    ``restore_album`` calls ``run_import_worker`` directly, bypassing
    ``app/import_jobs/runner.py`` entirely -- a fix applied only at the job
    runner leaves this path storing absolute rows. Driven on a thread because
    the API serves this from a worker, and because the main thread is bound by
    ``build_library`` (trap 2).
    """
    music = tmp_path / "music"
    music.mkdir()
    db_path = tmp_path / "library.db"
    lib = build_library(str(db_path), str(music))
    trash = tmp_path / "trash"
    trash.mkdir()
    trashed = _seed_album(trash / "Artist - Album")

    result: list[object] = []
    thread = threading.Thread(
        target=lambda: result.append(
            restore_album(lib, str(trashed), trash_dir=trash, origins_dir=origins_for(trash))
        ),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=30.0)
    assert not thread.is_alive(), "restore worker did not finish in time"
    assert result, f"restore failed: {result}"
    assert getattr(result[0], "restored", False), f"restore failed: {result}"

    stored = _stored_paths(db_path)
    assert stored == [b"Artist/Album/01 A.flac", b"Artist/Album/02 B.flac"], (
        f"restored item paths were not stored relative to the music dir. Raw SQLite blobs: {stored}"
    )
