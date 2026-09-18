"""Replace moves the old copy to Trash BEFORE beets places the new one.

Real beets, real FLACs, a real ``Library``, a real Trash dir and a real origin
store. Only the MusicBrainz lookup is canned, through the same ``tag_album`` seam
tests/test_import_incremental_e2e.py uses — and its helpers are reused here so
the two files cannot drift on how an import is driven.

What the ordering buys, measured rather than argued:

* beets removes duplicates at ``importer/stages.py:276`` and places files at
  ``:293``, so a Trash move made inside the duplicate hook happens while the new
  album is still only in the download folder.
* all-or-nothing falls out of it: if the move fails there is nothing to undo,
  because beets has not been told anything yet.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from beets.autotag.match import Recommendation as BeetsRec

import app.beets.import_session as session_mod
from app.beets.existing_album import to_existing_album
from app.beets.import_session import (
    _REPLACE_NO_TRASH,
    ImportBridge,
    WebImportSession,
    run_import_worker,
)
from app.beets.library import _require_id
from app.beets.trash import trash_album as real_trash_album
from app.models.bank import BankApplyDirective
from app.models.import_models import DuplicateAction
from tests.conftest import origins_for
from tests.test_import_incremental_e2e import (
    _ALBUM,
    _DEADLINE_S,
    _import,
    _install_lookup,
    _item_paths,
    _library,
    _source_folder,
)

if TYPE_CHECKING:
    from beets.library import Library


def _seed_library_copy(
    lib: Library, source: Path, monkeypatch: pytest.MonkeyPatch, *, times: int = 1
) -> None:
    """Import ``source`` ``times`` times, keeping every copy.

    The first run auto-applies (strong match); every later one meets the
    duplicate question and answers keep_both, so the library ends with ``times``
    albums sharing one albumartist+album — the shape a Replace then has to
    resolve.
    """
    assert _import(lib, source, ImportBridge()).errors == []
    for _ in range(times - 1):
        run = _import(
            lib,
            source,
            ImportBridge(),
            incremental=False,
            duplicate=DuplicateAction.keep_both,
        )
        assert run.errors == []
    assert len(list(lib.albums())) == times


def _replace(lib: Library, source: Path, *, trash_dir: Path | None) -> tuple[Any, list[str | None]]:
    """Re-import ``source`` answering Replace. Returns the run and its notes."""
    bridge = ImportBridge()
    run = _import(
        lib,
        source,
        bridge,
        incremental=False,
        duplicate=DuplicateAction.replace,
        trash_dir=trash_dir,
    )
    return run, [o.note for o in bridge.drain_outcomes() if o.note is not None]


def _tree(root: Path) -> list[str]:
    """Every path under ``root``, relative and sorted — the disk oracle."""
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def _library_snapshot(lib: Library) -> list[tuple[int, str]]:
    """(album id, item path) for every row — the library oracle."""
    with lib.music_dir_context():
        return sorted(
            (_require_id(item.album_id), os.fsdecode(item.path))
            for item in lib.items()
            if item.album_id
        )


# ----- the happy path -----


def test_replace_trashes_the_old_copy_before_the_new_one_is_placed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for every refusal below: wired Trash, one duplicate, copy mode.

    The old files end up in Trash with an origin record, the one album left has
    every file on disk, and nothing is reported.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    _seed_library_copy(lib, source, monkeypatch)
    old_files = {p.name for p in _item_paths(lib)}

    run, notes = _replace(lib, source, trash_dir=trash)

    assert run.errors == []
    assert notes == []
    assert len(list(lib.albums())) == 1
    assert [p for p in _item_paths(lib) if not p.exists()] == []
    trashed = {Path(p).name for p in _tree(trash) if p.endswith(".flac")}
    assert trashed == old_files
    assert [p for p in _tree(origins_for(trash)) if p.endswith(".json")] != []


def test_two_duplicates_reach_trash_in_distinct_containers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both old copies move, each into its own top-level Trash entry.

    A shared container would collapse two different albums under one entry, which
    is what ``trash_album``'s per-album basedir exists to prevent — and what an
    Empty of one entry would then take both of.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    _seed_library_copy(lib, source, monkeypatch, times=2)

    run, notes = _replace(lib, source, trash_dir=trash)

    assert run.errors == []
    assert notes == []
    containers = sorted(p.name for p in trash.iterdir())
    assert len(containers) == 2, containers
    assert len(list(lib.albums())) == 1
    assert [p for p in _item_paths(lib) if not p.exists()] == []


# ----- all-or-nothing -----


def test_a_failed_first_move_imports_nothing_and_leaves_the_library_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The move raises before anything reaches Trash: Replace becomes Skip.

    beets is answered SKIP, so it never places a file — the library rows, the
    music tree and the download folder are byte-identical to before, and Trash
    stays empty. The user is told in one sentence.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    _seed_library_copy(lib, source, monkeypatch)

    def boom(lib: Any, album: Any, **kwargs: Any) -> str:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(session_mod, "trash_album", boom)
    before_rows = _library_snapshot(lib)
    before_music = _tree(tmp_path / "music")
    before_downloads = _tree(source)

    run, notes = _replace(lib, source, trash_dir=trash)

    assert run.errors == []  # a refused Replace is not a failed import
    assert notes == ["Replace could not move the old copy to Trash. Nothing was imported."]
    assert _library_snapshot(lib) == before_rows
    assert _tree(tmp_path / "music") == before_music
    assert _tree(source) == before_downloads
    assert _tree(trash) == []


def test_a_failure_on_the_second_of_two_says_how_many_already_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The partial state is real, and the sentence has to name it.

    One old copy is in Trash with its origin record; the other is untouched in
    the library; nothing was imported. Saying only "Replace failed" would hide a
    folder that has left the library — and its origin record is what makes
    Restore work for the half that moved.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    _seed_library_copy(lib, source, monkeypatch, times=2)

    calls: list[int] = []

    def flaky(lib: Any, album: Any, **kwargs: Any) -> str:
        calls.append(_require_id(album.id))
        if len(calls) == 1:
            return str(real_trash_album(lib, album, **kwargs))
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(session_mod, "trash_album", flaky)

    run, notes = _replace(lib, source, trash_dir=trash)

    assert run.errors == []
    assert notes == ["Replace moved 1 of 2 old copies to Trash, then failed. Nothing was imported."]
    assert len(calls) == 2
    # One copy moved, with its origin record; one album is left in the library
    # with its files, and nothing new was imported.
    assert len(list(trash.iterdir())) == 1
    assert [p for p in _tree(origins_for(trash)) if p.endswith(".json")] != []
    survivors = list(lib.albums())
    assert len(survivors) == 1
    assert _require_id(survivors[0].id) == calls[1]
    assert [p for p in _item_paths(lib) if not p.exists()] == []


@pytest.mark.parametrize("root_dropped", [True, False])
def test_a_dropped_library_root_refuses_the_replace(
    root_dropped: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The share is gone, so "this album has no file" means nothing.

    With the music root emptied, EVERY duplicate reads as a ghost — and the ghost
    branch answers beets REMOVE, which would drop the whole collision's rows on a
    library that is merely unreachable. ``require_library_root`` is asked first,
    at the decision moment its own docstring names, and the run refuses instead.

    ``root_dropped=False`` is the control: the SAME fixture with the root intact
    replaces for real. Without it this test would pass on a build that refused
    every Replace.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    music = tmp_path / "music"
    _seed_library_copy(lib, source, monkeypatch)
    old_id = _require_id(next(iter(lib.albums())).id)
    if root_dropped:
        for entry in music.iterdir():  # the share dropped: the root is bare
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        assert list(music.iterdir()) == []

    run, notes = _replace(lib, source, trash_dir=trash)

    assert run.errors == []
    if root_dropped:
        assert notes == [
            "Library folder is empty. Is the music share mounted? Nothing was imported."
        ]
        assert lib.get_album(old_id) is not None, "the duplicate's rows were dropped anyway"
        assert [a.id for a in lib.albums()] == [old_id], "something was imported"
        assert _tree(trash) == []
    else:
        assert notes == []
        assert len(list(lib.albums())) == 1
        assert [p for p in _item_paths(lib) if not p.exists()] == []
        assert [p for p in _tree(trash) if p.endswith(".flac")] != []


def test_a_partial_failure_still_repairs_the_exports_the_moved_copy_broke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One copy left the library, so its playlists are already wrong.

    The re-export sits in a ``finally`` for exactly this: the run is about to
    answer SKIP, and the album that DID move has taken its rows with it. Running
    the re-export only on the success path would leave those exports naming files
    that are now in Trash.

    Four entries go in, two of them the trashed copy's; two lines must come out.
    """
    from app.playlists.reexport import export_dir_for, render_export
    from app.playlists.store import StoredEntry, create_playlist

    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()
    _seed_library_copy(lib, source, monkeypatch, times=2)
    every_item = [_require_id(i.id) for a in lib.albums() for i in a.items()]
    assert len(every_item) == 4
    record = create_playlist(
        playlists_dir,
        name="Mix",
        entries=[StoredEntry(uid=f"u{n}", item_id=i) for n, i in enumerate(every_item)],
    )
    render_export(record, lib, export_dir_for(lib))
    export = export_dir_for(lib) / f"{record.id}.m3u8"
    assert export.read_text(encoding="utf-8").count("#EXTINF:") == 4

    calls: list[int] = []

    def flaky(lib: Any, album: Any, **kwargs: Any) -> str:
        calls.append(_require_id(album.id))
        if len(calls) == 1:
            return str(real_trash_album(lib, album, **kwargs))
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(session_mod, "trash_album", flaky)
    directive = BankApplyDirective(action="duplicate", duplicate_action=DuplicateAction.replace)

    assert (
        _run_banked_replace(
            lib, source, trash_dir=trash, directive=directive, playlists_dir=playlists_dir
        )
        == []
    )

    assert len(calls) == 2
    assert export.read_text(encoding="utf-8").count("#EXTINF:") == 2


def test_replace_without_a_trash_folder_imports_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unwired Trash is a refusal, not a silent keep-both.

    Before the reorder this imported the new album and left BOTH copies in the
    library. The control is
    ``test_replace_trashes_the_old_copy_before_the_new_one_is_placed``: the same
    run with the Trash pair wired replaces for real.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    _seed_library_copy(lib, source, monkeypatch)
    before_rows = _library_snapshot(lib)

    run, notes = _replace(lib, source, trash_dir=None)

    assert run.errors == []
    assert notes == [_REPLACE_NO_TRASH]
    assert _library_snapshot(lib) == before_rows


# ----- ghosts: rows with no files -----


def test_replacing_a_ghost_whose_paths_the_import_reoccupies_keeps_the_new_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``copy``-mode destruction the shipped ghost tests miss.

    The user deletes an album's folder outside MusicDrop and re-imports the SAME
    album. beets computes exactly the ghost's old paths for the new files (there
    is no collision to send them to ``*.1.flac``), so a Trash pass running AFTER
    placement moves the freshly imported files — measured, under plain ``copy``.

    ``tests/test_ghost_replace_repro.py`` does not reach it because its ghost is
    tagged with a different track title, so the ghost's rows name paths the
    import never takes. Here the tags are identical by construction: the ghost IS
    the first import.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    _seed_library_copy(lib, source, monkeypatch)
    ghost_paths = _item_paths(lib)
    ghost_id = _require_id(next(iter(lib.albums())).id)
    for path in ghost_paths:  # deleted outside the app -> rows, no files
        path.unlink()
    assert all(not p.exists() for p in ghost_paths)

    run, notes = _replace(lib, source, trash_dir=trash)

    assert run.errors == []
    assert notes == []
    assert lib.get_album(ghost_id) is None, "the ghost's rows survived"
    assert len(list(lib.albums())) == 1
    landed = _item_paths(lib)
    assert sorted(landed) == sorted(ghost_paths), "the new album did not take the ghost's paths"
    assert [p for p in landed if not p.exists()] == []
    assert _tree(trash) == [], "a ghost has nothing to move"


# ----- the banked route, which still trashes AFTER the run -----


def _run_banked_replace(
    lib: Library,
    source: Path,
    *,
    trash_dir: Path,
    directive: BankApplyDirective,
    playlists_dir: Path | None = None,
) -> list[str]:
    """One bank-apply import run on its own thread. Returns any worker errors."""
    bridge = ImportBridge()
    errors: list[str] = []

    def worker() -> None:
        session = WebImportSession(
            lib,
            None,
            [os.fsencode(str(source))],
            None,
            bridge,
            trash_dir,
            trash_origins_dir=origins_for(trash_dir),
            directive=directive,
            playlists_dir=playlists_dir,
        )
        try:
            run_import_worker(session, directive=directive)
        except Exception as exc:  # reported, never swallowed
            errors.append(f"{exc.__class__.__name__}: {exc}")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=_DEADLINE_S)
    assert not thread.is_alive(), "bank apply worker hung"
    return errors


def test_the_banked_pass_drops_rows_naming_a_file_this_run_just_landed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same-file hazard on the route that still trashes after the run.

    A hardlink import leaves the download and the library file as ONE file. The
    library copy is then renamed in the DB only — its files never move — so beets'
    name-keyed ``find_duplicates`` misses it and the duplicate hook never fires:
    ``_seed_replace_from_directive`` is the only mechanism left, and it runs after
    placement. The new album is filed on the old album's paths (samefile, so no
    ``unique_path``), and moving the old album by id would move the new album's
    own files.

    So the pass drops the rows naming those files and leaves the files alone.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "hardlink")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    _seed_library_copy(lib, source, monkeypatch)
    old = next(iter(lib.albums()))
    old_id = _require_id(old.id)
    old_paths = _item_paths(lib)
    old.album = "Kid A"  # renamed in the DB only: the FILES stay where they are
    old.store()
    directive = BankApplyDirective(
        action="duplicate",
        duplicate_action=DuplicateAction.replace,
        replace_existing=[to_existing_album(lib, lib.get_album(old_id))],
    )

    assert _run_banked_replace(lib, source, trash_dir=trash, directive=directive) == []

    assert lib.get_album(old_id) is None, "the renamed copy's rows survived"
    albums = list(lib.albums())
    assert [a.album for a in albums] == [_ALBUM]
    landed = _item_paths(lib)
    assert sorted(landed) == sorted(old_paths), "the import did not reuse the old paths"
    assert [p for p in landed if not p.exists()] == []
    assert _tree(trash) == [], "the only files the old album named are the new album's"


def test_a_healthy_banked_replace_logs_no_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The hook handles it, and the seed must then say nothing.

    With Trash first, SQLite hands the new album the rowid the old one freed
    (measured ``[1] -> landed [1]``), so every banked entry the hook already
    moved would trip the seed's "its id was reused" guard on a perfectly healthy
    run. The seed drops those entries first; the guard itself is untouched, and
    ``test_banked_replace_seed_never_trashes_the_album_it_just_imported`` still
    pins it.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    _seed_library_copy(lib, source, monkeypatch)
    old = next(iter(lib.albums()))
    old_id = _require_id(old.id)
    directive = BankApplyDirective(
        action="duplicate",
        duplicate_action=DuplicateAction.replace,
        replace_existing=[to_existing_album(lib, old)],
    )

    with caplog.at_level(logging.WARNING, logger="app.beets.import_session"):
        assert _run_banked_replace(lib, source, trash_dir=trash, directive=directive) == []

    assert [r.getMessage() for r in caplog.records] == []
    landed = list(lib.albums())
    assert len(landed) == 1
    assert _require_id(landed[0].id) == old_id, "the reused rowid is the premise of this test"
    assert [p for p in _item_paths(lib) if not p.exists()] == []
    assert [p for p in _tree(trash) if p.endswith(".flac")] != []


def test_the_hook_reexports_the_playlists_of_the_copy_it_trashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `.m3u8` collateral, on the route that now moves the files.

    The old copy's rows are gone the moment the hook trashes it, so every export
    holding one of its tracks names a file that is not there. The item ids are
    read BEFORE ``trash_album`` drops the rows — afterwards there is nothing left
    to read them from — and the export is rewritten in the same call.

    Driven through the directive route because it answers the duplicate question
    without a human; the hook and the trashing are the same code either way.
    """
    from app.playlists.reexport import export_dir_for, render_export
    from app.playlists.store import StoredEntry, create_playlist

    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()
    _seed_library_copy(lib, source, monkeypatch)
    old = next(iter(lib.albums()))
    doomed = next(iter(old.items()))
    record = create_playlist(
        playlists_dir,
        name="Mix",
        entries=[StoredEntry(uid="u0", item_id=_require_id(doomed.id))],
    )
    render_export(record, lib, export_dir_for(lib))
    export = export_dir_for(lib) / f"{record.id}.m3u8"
    doomed_line = os.path.relpath(os.fsdecode(doomed.path), str(export_dir_for(lib)))
    assert doomed_line in export.read_text(encoding="utf-8")
    directive = BankApplyDirective(
        action="duplicate",
        duplicate_action=DuplicateAction.replace,
        replace_existing=[to_existing_album(lib, old)],
    )

    assert (
        _run_banked_replace(
            lib, source, trash_dir=trash, directive=directive, playlists_dir=playlists_dir
        )
        == []
    )

    body = export.read_text(encoding="utf-8")
    assert doomed_line not in body
    assert "#EXTINF:" not in body, "the dropped id must lose its line, not be re-pointed"
