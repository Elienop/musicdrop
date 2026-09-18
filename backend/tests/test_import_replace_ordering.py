"""Replace moves the old copy to Trash BEFORE beets places the new one.

Real beets, real FLACs, a real ``Library``, a real Trash dir and a real origin
store. Only the MusicBrainz lookup is canned, through the same ``tag_album`` seam
tests/test_import_incremental_e2e.py uses — and its helpers are reused here so
the two files cannot drift on how an import is driven.

What the ordering buys, measured rather than argued:

* beets asks the duplicate hook from ``_resolve_duplicates``
  (``importer/stages.py:337``) and places files in ``manipulate_files`` at
  ``:294``, so a Trash move made inside the hook happens while the new album is
  still only in the download folder.
* all-or-nothing falls out of it: if the move fails there is nothing to undo,
  because beets has not been told anything yet.
* the hook answers ``KEEP``, so ``task.remove_duplicates`` — the only caller of
  which is the ``REMOVE`` arm at ``:275-276`` — never runs, and nothing is
  disposed of except what this hook was handed.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from beets import config
from beets import util as beets_util
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
from app.models.import_models import DuplicateAction, ImportAction
from tests.conftest import origins_for
from tests.test_import_incremental_e2e import (
    _ALBUM,
    _ARTIST,
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


def _replace(
    lib: Library,
    source: Path,
    *,
    trash_dir: Path | None,
    choice: ImportAction = ImportAction.skip,
) -> tuple[Any, list[str | None]]:
    """Re-import ``source`` answering Replace. Returns the run and its notes.

    ``choice`` is the review answer that precedes the duplicate question; the
    default never applies because every source here matches strongly enough to
    auto-apply. ``asis`` is for the one test that needs beets to take the file
    tags, which is where its album-level fields get rewritten inside ``add``.
    """
    bridge = ImportBridge()
    run = _import(
        lib,
        source,
        bridge,
        choice=choice,
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


def _half_finished_import(
    lib: Library, source: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """The state a mid-album placement failure leaves. Returns (landed, stranded).

    beets' own ``FilesystemError`` out of ``util.copy`` on the SECOND track:
    ``task.add`` has already committed the album, so the library keeps one row
    naming the music folder and one still naming the download folder (measured,
    ENOSPC). Re-importing the folder is the documented repair for it, which is
    why a Replace has to survive the shape rather than refuse it.
    """
    real_copy = beets_util.copy
    calls: list[int] = []

    def flaky_copy(path: Any, dest: Any, replace: bool = False) -> Any:
        calls.append(1)
        if len(calls) == 2:
            raise beets_util.FilesystemError(
                OSError(errno.ENOSPC, "No space left on device"), "copy", (path, dest)
            )
        return real_copy(path, dest, replace=replace)

    monkeypatch.setattr(beets_util, "copy", flaky_copy)
    try:
        run = _import(lib, source, ImportBridge())
    finally:
        monkeypatch.setattr(beets_util, "copy", real_copy)
    assert run.errors != [], "the fixture must actually fail one placement"
    music = Path(os.fsdecode(lib.directory))
    rows = _item_paths(lib)
    inside = [p for p in rows if p.is_relative_to(music)]
    outside = [p for p in rows if not p.is_relative_to(music)]
    assert len(inside) == 1, rows
    assert outside == [source / "02 Track 2.flac"], rows
    return inside[0], outside[0]


def _extra_matching_album(
    lib: Library, *, folder: Path, name: str = "01 later.flac", title: str = "Later"
) -> tuple[int, Path]:
    """A library album with the SAME albumartist+album as the import, added directly.

    Directly and not by import, because the point is an album that appears in
    the collision bucket WITHOUT ever appearing on a prompt. ``folder`` is
    absolute on purpose: the same helper builds one inside the music folder and
    one legitimately outside it.
    """
    from beets.library import Item

    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    shutil.copyfile(Path(__file__).parent / "fixtures" / "silent.flac", path)
    with lib.music_dir_context():
        album = lib.add_album(
            [
                Item(
                    artist=_ARTIST,
                    albumartist=_ARTIST,
                    album=_ALBUM,
                    title=title,
                    track=1,
                    length=1.0,
                    path=os.fsencode(str(path)),
                )
            ]
        )
        album.store()
    return _require_id(album.id), path


def _refile_into_its_own_folder(lib: Library, album: Any, folder: Path) -> list[Path]:
    """Move an album's files to ``folder``, rows and all, leaving its NAME alone.

    Its own folder is what lets a test make one duplicate of a collision
    unreadable without touching the other; leaving albumartist+album alone is
    what keeps beets' name-keyed ``find_duplicates`` finding both.
    """
    folder.mkdir(parents=True, exist_ok=True)
    moved: list[Path] = []
    with lib.music_dir_context():
        for item in album.items():
            dest = folder / Path(os.fsdecode(item.path)).name
            shutil.move(os.fsdecode(item.path), dest)
            item.path = os.fsencode(str(dest))
            item.store()
            moved.append(dest)
    return moved


class _FakeTask:
    """The one thing ``_SourceFiles.note`` reads: ``task.items[*].path``.

    A stand-in, not a beets task, so the helper's own arms can be reached with
    paths no real import would produce (a vanished source, a locked folder).
    """

    def __init__(self, paths: list[Path]) -> None:
        self.items = [SimpleNamespace(path=os.fsencode(str(p))) for p in paths]


def _respell_row(lib: Library, old: Path, new: Path) -> None:
    """Rewrite the row naming ``old`` so it names ``new`` — the same file, spelled twice.

    Moves nothing: the caller supplies a second spelling of one file (a symlink
    alias of its folder), which is what a downloads tree reachable by two names
    leaves behind. The premise is asserted at the call site with ``st_ino``.
    """
    with lib.music_dir_context():
        for item in lib.items():
            if item.path and Path(os.fsdecode(item.path)) == old:
                item.path = os.fsencode(str(new))
                item.store()


def _add_row_naming(lib: Library, album: Any, path: Path) -> Path:
    """Give ``album`` one more item row naming ``path``, and return the path.

    Used to hang a row naming one of the import's OWN source files on an album
    that is about to be refused, which is the only way to see WHERE the
    source-row drop sits: the file itself is untouched either way, so the row
    is the oracle.
    """
    from beets.library import Item

    with lib.music_dir_context():
        item = Item(
            artist=_ARTIST,
            albumartist=_ARTIST,
            album=_ALBUM,
            title="Stray",
            track=9,
            length=1.0,
            path=os.fsencode(str(path)),
        )
        lib.add(item)
        item.album_id = _require_id(album.id)
        item.store()
    return path


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

    And the names are pinned, which is what makes this an ordering test rather
    than a counting one: with the old copy gone before beets files anything, the
    new files take the template's own names (``01 Airbag 1.flac``). Trash after
    placement and beets finds its destinations occupied, so it writes
    ``01 Airbag 1.1.flac`` beside them — a library the user did not ask for,
    which every count-only assertion here passes happily.
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
    landed = _item_paths(lib)
    assert [p for p in landed if not p.exists()] == []
    assert sorted(p.name for p in landed) == ["01 Airbag 1.flac", "02 Airbag 2.flac"]
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
    # "failed while moving", not "could not move": nothing had moved here, but
    # the same arm answers after a move that DID happen, and a sentence that
    # denies the move would send the user looking in the wrong place. The
    # count-bearing sentence is pinned by the two-copy test below.
    assert notes == ["Replace failed while moving the old copy to Trash. Nothing was imported."]
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
    arm drops the rows, which would take the whole collision's rows on a library
    that is merely unreachable. ``require_library_root`` is asked first, at the
    decision moment its own docstring names, and the run refuses instead.

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

    The run is about to answer SKIP, and the album that DID move has taken its
    rows with it. Running the re-export only on the success path would leave
    those exports naming files that are now in Trash — which is what this test
    pins. That the re-export sits in a ``finally`` rather than after the
    ``try/except`` is pinned separately, by
    ``test_a_run_that_fails_after_the_hook_still_repairs_the_export``: here the
    worker returns normally, so both spellings pass.

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
    for path in ghost_paths:  # deleted outside the app -> rows, no files
        path.unlink()
    assert all(not p.exists() for p in ghost_paths)

    run, notes = _replace(lib, source, trash_dir=trash)

    assert run.errors == []
    assert notes == []
    # NOT ``get_album(ghost_id) is None``: the hook drops the ghost's rows BEFORE
    # beets inserts the new album, and SQLite hands the freed rowid straight to
    # that insert (measured: ghost [1] -> landed [1]), so the id is not an
    # identity any more. What the ghost's departure means here is the two
    # assertions below: ONE album, and every file it names is on disk — which the
    # ghost's own rows could not have satisfied.
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
    bridge: ImportBridge | None = None,
) -> list[str]:
    """One bank-apply import run on its own thread. Returns any worker errors.

    ``bridge`` is for the callers that need the outcome NOTES as well: pass one
    in and drain it afterwards (:func:`_bank_replace_notes`).
    """
    bridge = ImportBridge() if bridge is None else bridge
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


def _bank_replace_notes(
    lib: Library, source: Path, *, trash_dir: Path, directive: BankApplyDirective
) -> list[str | None]:
    """:func:`_run_banked_replace`, asserting no worker error and returning the notes."""
    bridge = ImportBridge()
    assert (
        _run_banked_replace(lib, source, trash_dir=trash_dir, directive=directive, bridge=bridge)
        == []
    )
    return [o.note for o in bridge.drain_outcomes() if o.note is not None]


def test_a_banked_replace_sharing_a_file_drops_rows_and_moves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The same-file hazard on the route that still trashes after the run.

    A hardlink import leaves the download and the library file as ONE file. The
    library copy is then renamed in the DB only — its files never move — so beets'
    name-keyed ``find_duplicates`` misses it and the duplicate hook never fires:
    ``_seed_replace_from_directive`` is what is left, and it runs after
    placement. The new album is filed on the old album's paths (samefile, so no
    ``unique_path``), and moving the old album by id would move the new album's
    own files.

    So the pass asks per ALBUM whether it shares a file with anything this run
    landed, and if it does: drop its rows, move nothing at all.

    The files stay in the library UNTRACKED, which disk sync (DB from disk, one
    way) will not pick up — so the warning has to name the album and its folder.
    An id cannot: this one is a rowid that is free the moment the rows go.
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

    with caplog.at_level(logging.WARNING, logger="app.beets.import_session"):
        assert _run_banked_replace(lib, source, trash_dir=trash, directive=directive) == []

    assert lib.get_album(old_id) is None, "the renamed copy's rows survived"
    albums = list(lib.albums())
    assert [a.album for a in albums] == [_ALBUM]
    landed = _item_paths(lib)
    assert sorted(landed) == sorted(old_paths), "the import did not reuse the old paths"
    assert [p for p in landed if not p.exists()] == []
    assert _tree(trash) == [], "the only files the old album named are the new album's"
    shares = [r.getMessage() for r in caplog.records if "shares a file" in r.getMessage()]
    assert len(shares) == 1, [r.getMessage() for r in caplog.records]
    assert f"Radiohead - Kid A ({old_paths[0].parent})" in shares[0], shares[0]


def test_a_symlinked_library_root_is_still_the_same_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two spellings of one file, which is why the question is asked by IDENTITY.

    Same shape as the test above, with one change: the old album's rows are
    rewritten to name its files through a SYMLINK to the music root. Nothing
    moves, so both albums still point at the same bytes — but now the strings
    differ, and ``normpath`` does not resolve symlinks. Comparing stored paths
    reads "not shared" and trashes the album, taking the files beets just filed
    for the album that landed. ``(st_dev, st_ino)`` through ``os.stat`` answers
    the question the guard is actually asking.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "hardlink")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    music = Path(os.fsdecode(lib.directory))
    spelling = tmp_path / "music-by-another-name"
    spelling.symlink_to(music, target_is_directory=True)
    _seed_library_copy(lib, source, monkeypatch)
    old = next(iter(lib.albums()))
    old_id = _require_id(old.id)
    old_paths = _item_paths(lib)
    with lib.music_dir_context():
        for item in old.items():
            through_link = spelling / Path(os.fsdecode(item.path)).relative_to(music)
            assert through_link.is_file(), through_link
            item.path = os.fsencode(str(through_link))  # the second spelling
            item.store()
    old.album = "Kid A"  # renamed in the DB only, as above: the hook never fires
    old.store()
    directive = BankApplyDirective(
        action="duplicate",
        duplicate_action=DuplicateAction.replace,
        replace_existing=[to_existing_album(lib, lib.get_album(old_id))],
    )

    assert _run_banked_replace(lib, source, trash_dir=trash, directive=directive) == []

    assert lib.get_album(old_id) is None, "the renamed copy's rows survived"
    landed = _item_paths(lib)
    assert sorted(landed) == sorted(old_paths), "the import did not reuse the old paths"
    assert [p for p in landed if not p.exists()] == []
    assert _tree(trash) == [], "the shared files must not move, whatever they are called"


def _banked_renamed_copy(
    lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, int, list[Path], BankApplyDirective]:
    """A library copy the duplicate hook cannot reach, in a folder of its own.

    Renamed AND refiled: beets' exact ``find_duplicates`` misses the new name, so
    the hook never fires and the banked pass is what runs — and the album owning
    its own folder is what lets a test make it unreadable without also breaking
    the import's own copy into the album folder.
    """
    source = _source_folder(tmp_path)
    _seed_library_copy(lib, source, monkeypatch)
    old = next(iter(lib.albums()))
    old_id = _require_id(old.id)
    with lib.music_dir_context():
        old.album = "Kid A"
        old.store()
        old.move()
    directive = BankApplyDirective(
        action="duplicate",
        duplicate_action=DuplicateAction.replace,
        replace_existing=[to_existing_album(lib, lib.get_album(old_id))],
    )
    return source, old_id, _item_paths(lib), directive


def test_a_banked_replace_of_a_readable_copy_moves_it_to_trash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The banked pass's OTHER arm, and the control for the two tests around it.

    Nothing is shared and everything is readable, so this album is trashed as
    before: its files leave the library with an origin record and its rows go
    with them. Without this, "moves nothing" could pass by moving nothing ever.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    trash = tmp_path / "trash"
    source, old_id, old_paths, directive = _banked_renamed_copy(lib, tmp_path, monkeypatch)

    assert _run_banked_replace(lib, source, trash_dir=trash, directive=directive) == []

    assert lib.get_album(old_id) is None
    assert [p for p in old_paths if p.exists()] == [], "the old files stayed in the library"
    assert [p for p in _tree(trash) if p.endswith(".flac")] != []
    assert [p for p in _tree(origins_for(trash)) if p.endswith(".json")] != []
    assert [p for p in _item_paths(lib) if not p.exists()] == []


def test_a_link_mode_import_onto_a_symlinked_source_shares_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identity is asked THROUGH symlinks, because a link-mode import can be one.

    The chain this builds, which is what ``link: yes`` produces over a download
    folder that points into the library: the landed album's file is a symlink to
    the download entry, and that entry is itself a symlink to the old album's
    file. So the only real bytes are the old album's, and moving it to Trash
    leaves the album that just landed pointing at nothing.

    ``os.stat`` resolves the whole chain and the two albums come out sharing a
    file, so the pass drops the old rows and moves nothing. ``os.lstat`` would
    answer with the symlink's own inode, find no overlap, and trash the file the
    new album is built on — which is why ``_file_identities`` follows.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    trash = tmp_path / "trash"
    source, old_id, old_paths, directive = _banked_renamed_copy(lib, tmp_path, monkeypatch)
    # The download points INTO the library: each entry becomes a symlink to the
    # old album's file (track order is the pairing — both were made from it).
    for path, target in zip(sorted(source.glob("*.flac")), sorted(old_paths), strict=True):
        path.unlink()
        path.symlink_to(target)
    config["import"]["copy"] = False
    config["import"]["link"] = True

    assert _run_banked_replace(lib, source, trash_dir=trash, directive=directive) == []

    assert lib.get_album(old_id) is None, "the renamed copy's rows survived"
    landed = _item_paths(lib)
    assert [p for p in landed if not p.is_symlink()] == [], "the premise: link mode symlinked"
    assert [p for p in landed if not p.exists()] == [], "a landed file no longer resolves"
    assert [p for p in old_paths if not p.is_file()] == [], "the real bytes left the library"
    assert _tree(trash) == [], "the shared file must not move"


def test_two_albums_sharing_only_their_cover_still_share_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The identity question counts ``artpath``, and this pins that directly.

    Moving an album takes its cover with it (``Album.move`` → ``move_art``), so
    two albums whose artpath is the same file share a file even when no track
    does — and beets creates exactly that pairing itself: a re-import whose new
    album lands on a replaced album's path inherits its artpath
    (``importer/tasks.py:582``, ``self.album.artpath = replaced_album.artpath``).
    Trashing the old one then moves the cover the album that just landed points
    at.

    A unit pin, not a run: the identity sets are read DURING the post-run pass,
    and nothing in a plugin-free import sets an artpath on the landed album
    (measured — beets imports no ``cover.jpg`` from a download folder
    without ``fetchart``), so the pairing cannot be built end to end here.
    """
    from beets.library import Item

    from app.beets.import_session import _file_identities, _landed_file_identities

    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    _seed_library_copy(lib, source, monkeypatch)
    landed_album = next(iter(lib.albums()))
    cover = _item_paths(lib)[0].parent / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff\xd9")
    with lib.music_dir_context():
        landed_album.artpath = os.fsencode(str(cover))
        landed_album.store()
        # The other album: rows only, no track file of its own anywhere, and the
        # SAME cover — the inherited-artpath shape.
        other = lib.add_album(
            [
                Item(
                    albumartist="Radiohead",
                    album="Kid A",
                    title="Everything In Its Right Place",
                    track=1,
                    path=os.fsencode(str(tmp_path / "gone" / "01 nothing.flac")),
                )
            ]
        )
        other.artpath = os.fsencode(str(cover))
        other.store()

        own, unreadable = _file_identities(lib, other)
        landed, landed_unreadable = _landed_file_identities(lib, {_require_id(landed_album.id)})

    assert not unreadable
    assert not landed_unreadable
    assert own & landed, "the shared cover is a shared file"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits this test sets")
def test_an_unreadable_file_leaves_the_banked_copy_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ "Cannot read it" is not "not the same file", so nothing is moved.

    The banked pass decides by identity, and a file it cannot stat has none. An
    EACCES here is not hypothetical: it is what a folder whose permissions the
    user changed, or a share that came back read-only, looks like. Treating the
    unanswered question as "not shared" would trash an album that may hold the
    files this run just landed, so the album keeps both its rows and its files
    and the skip is logged.

    The control is ``test_a_banked_replace_of_a_readable_copy_moves_it_to_trash``
    directly above: the same fixture without the permission change, where the
    files DO reach Trash.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    trash = tmp_path / "trash"
    source, old_id, old_paths, directive = _banked_renamed_copy(lib, tmp_path, monkeypatch)
    folder = old_paths[0].parent
    folder.chmod(0o000)  # no search permission -> os.stat raises EACCES
    try:
        with caplog.at_level(logging.WARNING, logger="app.beets.import_session"):
            assert _run_banked_replace(lib, source, trash_dir=trash, directive=directive) == []
    finally:
        folder.chmod(0o755)

    assert lib.get_album(old_id) is not None, "an unreadable album's rows were dropped"
    assert [p for p in old_paths if not p.exists()] == [], "its files were moved"
    assert _tree(trash) == []
    assert any("could not be read" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


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


def test_a_replace_leaves_the_export_equal_to_a_fresh_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `.m3u8` collateral, as ONE invariant: when the run ends, the export on
    disk equals a fresh render of the store.

    The old copy's rows go the moment the hook trashes it, so every export
    holding one of its tracks names a file that is not there. The item ids are
    read BEFORE the rows go — afterwards there is nothing left to read them from
    — and the run's single re-export point renders every affected playlist once,
    after ``session.run()`` returns.

    What the fresh render shows here, MEASURED: the entry is re-pointed at the
    album that landed rather than dropped, because SQLite hands the new item the
    rowid the trashed one freed. That is the outcome the user wants (the playlist
    still holds that song, now the replacement) and it is why an earlier version
    of this test — asserting the entry loses its line — was pinning the wrong
    thing. The invariant below holds either way; the count and the on-disk file
    are what say which one happened.

    The old copy is RETITLED and moved first, so its file names differ from the
    ones the import will compute. Without that the fixture is vacuous: in
    ``copy`` mode a re-import of the same source reuses both the rowids and the
    filenames, so an export nobody rewrote is already byte-equal to a fresh
    render and deleting the re-export leaves this test green (measured by the
    code seat). The album-level fields are untouched, so beets' name-keyed
    ``find_duplicates`` still hits and the hook still fires.

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
    with lib.music_dir_context():  # retitled + refiled: the old names differ
        for item in old.items():
            item.title = f"{item.title} (first rip)"
            item.store()
            item.move()
    doomed = next(iter(old.items()))
    record = create_playlist(
        playlists_dir,
        name="Mix",
        entries=[StoredEntry(uid="u0", item_id=_require_id(doomed.id))],
    )
    render_export(record, lib, export_dir_for(lib))
    export = export_dir_for(lib) / f"{record.id}.m3u8"
    doomed_line = os.path.relpath(os.fsdecode(doomed.path), str(export_dir_for(lib)))
    assert "(first rip)" in doomed_line, "the premise: the old file has its own name"
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

    after_run = export.read_text(encoding="utf-8")
    entries = [line for line in after_run.splitlines() if line and not line.startswith("#")]
    assert len(entries) == 1, after_run
    assert doomed_line not in after_run, "the export still names the copy that left"
    named = (export_dir_for(lib) / entries[0]).resolve()
    assert named.is_file(), f"the export names a file that is not there: {named}"
    assert named in [p.resolve() for p in _item_paths(lib)], "not a file the store now holds"

    # The invariant itself: rendering again from the store as it now is produces
    # byte-identical output. A run that re-exported too early (before the rows
    # went) or not at all fails here.
    render_export(record, lib, export_dir_for(lib))
    assert export.read_text(encoding="utf-8") == after_run


# ----- what answering beets KEEP (rather than REMOVE) protects -----

_COMP = "Blue Note Sampler"


def _compilation_source(tmp_path: Path) -> Path:
    """Two FLACs with DIFFERENT track artists and NO albumartist tag.

    The shape ``align_album_level_fields`` rewrites: with no albumartist
    consensus beets calls the album a compilation inside ``task.add`` and stamps
    every item ``albumartist = Various Artists``.
    """
    from mediafile import MediaFile

    sample = Path(__file__).parent / "fixtures" / "silent.flac"
    source = tmp_path / "downloads" / "sampler"
    source.mkdir(parents=True)
    for i, artist in enumerate(("Artist One", "Artist Two"), start=1):
        dst = source / f"{i:02d} Track {i}.flac"
        shutil.copyfile(sample, dst)
        mf = MediaFile(str(dst))
        mf.artist = artist
        mf.album = _COMP
        mf.title = f"Song {i}"
        mf.track = i
        mf.save()
    return source


def _seed_album(lib: Library, music: Path, *, albumartist: str, title: str) -> tuple[int, Path]:
    """One library album with one real file, added directly (no import)."""
    from beets.library import Item

    folder = music / albumartist / _COMP
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"01 {title}.flac"
    shutil.copyfile(Path(__file__).parent / "fixtures" / "silent.flac", path)
    item = Item(
        artist=albumartist,
        albumartist=albumartist,
        album=_COMP,
        title=title,
        track=1,
        length=1.0,
        path=os.fsencode(str(path)),
    )
    album = lib.add_album([item])
    album.store()
    return _require_id(album.id), path


def test_an_as_is_compilation_replace_leaves_the_album_the_user_never_saw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the hook answers KEEP and disposes of the copies itself.

    beets' ``ImportTask.remove_duplicates`` (``tasks.py:246``) does not reuse the
    list the hook was handed: it RE-RUNS ``find_duplicates``. For an as-is album
    the query key comes from the items' own tags via ``get_most_common_tags``,
    whose last step replaces the artist with the albumartist when there is a
    consensus (``util/__init__.py:842-844``) — and ``task.add`` has meanwhile
    stamped every item ``albumartist = Various Artists``. So the hook asks about
    ``("Artist One", album)`` and beets' removal asks about
    ``("Various Artists", album)``.

    The library here holds both spellings. The user is shown, and consents to
    replacing, exactly one of them; the other must be untouched, files and rows.
    Answering KEEP is what guarantees that, because the second query never runs.
    """
    _install_lookup(monkeypatch, BeetsRec.none)  # no auto-apply: the album parks
    lib = _library(tmp_path, "copy")
    music = Path(os.fsdecode(lib.directory))
    trash = tmp_path / "trash"
    shown_id, shown_file = _seed_album(lib, music, albumartist="Artist One", title="shown")
    hidden_id, hidden_file = _seed_album(lib, music, albumartist="Various Artists", title="hidden")
    source = _compilation_source(tmp_path)

    run, notes = _replace(lib, source, trash_dir=trash, choice=ImportAction.asis)

    assert run.errors == []
    assert notes == []
    # The premise: one prompt, naming only the album whose artist the FILES say.
    assert len(run.duplicates) == 1, run.duplicates
    assert [e.album_artist for e in run.duplicates[0].existing] == ["Artist One"]
    # The album the user consented to: gone from the library, files in Trash.
    assert lib.get_album(shown_id) is None
    assert not shown_file.exists()
    assert [p for p in _tree(trash) if p.endswith(".flac")] != []
    # The album the user was never shown: rows and file exactly as they were.
    assert lib.get_album(hidden_id) is not None, "beets removed an album nobody was shown"
    assert hidden_file.is_file()
    # And the import itself landed, as a compilation.
    landed = [a for a in lib.albums() if _require_id(a.id) != hidden_id]
    assert len(landed) == 1, [a.album for a in lib.albums()]
    assert landed[0].albumartist == "Various Artists"
    assert [p for p in _item_paths(lib) if not p.exists()] == []


def test_a_replace_never_runs_beets_own_duplicate_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mechanism the test above depends on, pinned directly.

    ``manipulate_files`` calls ``task.remove_duplicates`` only when the hook
    answered ``REMOVE`` (``importer/stages.py:276``). A spy over the beets method
    therefore records nothing on a Replace that succeeded — and records a call
    the moment the hook answers REMOVE again, which is the whole difference
    between disposing of what the user was shown and disposing of what a second
    query returns.
    """
    from beets.importer.tasks import ImportTask

    calls: list[str] = []
    real_remove = ImportTask.remove_duplicates

    def spy(self: Any, lib_arg: Any) -> None:
        calls.append(str(self.chosen_info().get("album")))
        real_remove(self, lib_arg)

    monkeypatch.setattr(ImportTask, "remove_duplicates", spy)
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    _seed_library_copy(lib, source, monkeypatch)

    run, notes = _replace(lib, source, trash_dir=tmp_path / "trash")

    assert run.errors == []
    assert notes == []
    assert calls == [], "beets' own duplicate removal ran on a healthy Replace"
    assert len(list(lib.albums())) == 1


# ----- classification: here, gone, or unanswerable -----


@pytest.mark.parametrize("mode", [0o444, 0o000])
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits this test sets")
def test_an_unreadable_duplicate_refuses_the_whole_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    """A duplicate whose files cannot be READ stops the Replace before anything moves.

    ``os.path.exists`` answers False for EACCES exactly as it does for a file
    that is not there, so a two-valued "has a file?" test reads a folder the user
    (or a share that came back read-only) made unreadable as a ghost — and the
    ghost arm drops rows without moving anything, orphaning every file under it.
    Both modes here refuse: 0o444 leaves the directory without the search bit, so
    even ``lstat`` on a file inside it fails.

    The classification runs over every duplicate BEFORE the first move, so this
    costs nothing: no rows dropped, no file moved, nothing imported, one sentence.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    _seed_library_copy(lib, source, monkeypatch)
    old_paths = _item_paths(lib)
    before_rows = _library_snapshot(lib)
    folder = old_paths[0].parent
    folder.chmod(mode)
    try:
        run, notes = _replace(lib, source, trash_dir=trash)
    finally:
        folder.chmod(0o755)

    assert run.errors == []  # a refused Replace is not a failed import
    assert notes == [
        "Replace could not read the old copy's files, so nothing was moved or imported."
    ]
    assert _library_snapshot(lib) == before_rows, "rows changed on a refused Replace"
    assert [p for p in old_paths if not p.exists()] == []
    assert _tree(trash) == []


def test_a_ghost_whose_cover_survived_is_replaced_and_its_cover_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Presence is the TRACK files, so a surviving cover does not block a Replace.

    The natural flow this serves: the user deletes a bad rip, keeps the
    ``cover.jpg`` they curated, imports a better rip and presses Replace. An
    album with no track file left is a ghost whatever its ``artpath`` says — its
    rows are dropped by ``album.remove(delete=False)``, which does not read the
    art path at all (``library/models.py:396-400``), so the cover is not moved,
    not deleted and not renamed. It is still byte-identical at its own path
    afterwards, and nothing about it is in Trash.

    Counting the art as presence instead sent the album to ``trash_album``,
    which REFUSES here (measured, which is why the ruling reversed):
    beets skips the move of every missing track, and with no item moved
    ``Album.move_art`` computes the art destination from the album's first item,
    whose stored path never changed, so ``new_art == old_art`` and it returns
    without moving (``library/models.py:434-436``). Nothing moved, so the
    mover's post-condition stopped the row drop and the Replace imported
    nothing. Owner ruling: this flow must work.

    The control is ``test_replace_trashes_the_old_copy_before_the_new_one_is_placed``
    — the same route with the tracks PRESENT, where the files do reach Trash.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    _seed_library_copy(lib, source, monkeypatch)
    old = next(iter(lib.albums()))
    old_id = _require_id(old.id)
    old_paths = _item_paths(lib)
    cover = old_paths[0].parent / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff\xd9")  # the file the user put there
    cover_bytes = cover.read_bytes()
    with lib.music_dir_context():
        old.artpath = os.fsencode(str(cover))
        old.store()
    for path in old_paths:  # every TRACK is gone; the cover is not
        path.unlink()

    run, notes = _replace(lib, source, trash_dir=trash)

    assert run.errors == []
    assert notes == []
    # The import happened, and the ghost's rows are gone. NOT by id: the hook
    # drops them before beets inserts, so the new album takes the freed rowid
    # (measured [1] -> landed [1]). One album, every file it names on disk.
    assert len(list(lib.albums())) == 1
    landed = _item_paths(lib)
    assert [p for p in landed if not p.exists()] == []
    assert sorted(p.name for p in landed) == ["01 Airbag 1.flac", "02 Airbag 2.flac"]
    # The cover: same path, same bytes, and nothing of it in Trash.
    assert cover.is_file(), "the curated cover was moved or deleted"
    assert cover.read_bytes() == cover_bytes
    assert _tree(trash) == [], "a ghost has nothing to move, cover included"
    assert old_id == _require_id(next(iter(lib.albums())).id)  # the reused rowid


@pytest.mark.parametrize("broken", [True, False])
def test_a_duplicate_of_dangling_symlinks_refuses_the_replace(
    broken: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A library path occupied by a link to nowhere is where the NEW audio goes.

    MEASURED by the security seat: beets' ``unique_path`` asks
    ``os.path.exists``, which is False for a dangling link, so placement takes
    that exact name and writes THROUGH the link — the new album's FLACs land
    wherever the link pointed, outside the music library, in ``copy``,
    ``hardlink`` and ``reflink:auto`` alike, with no error and no note. A later
    cleanup of that location destroys the user's only copy of an album the app
    still lists.

    So the whole Replace is refused before anything moves, with its own
    sentence. Replace does not delete anything, links included (owner ruling):
    the rows stay, the links stay, and nothing is written outside the library.

    ``broken=False`` is the control — the SAME fixture with the links pointing
    at real files, which replaces for real. Without it this test would pass on a
    build that refused every Replace, and it is also the pin that a LIVE symlink
    at a library path still counts as present.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    _seed_library_copy(lib, source, monkeypatch)
    old_paths = _item_paths(lib)
    for path in old_paths:  # the file becomes a link out of the library
        target = outside / path.name
        if not broken:
            shutil.move(path, target)
        else:
            path.unlink()
        path.symlink_to(target)
        assert path.is_symlink()
        assert path.exists() is not broken  # the premise
    before_rows = _library_snapshot(lib)
    before_outside = _tree(outside)

    run, notes = _replace(lib, source, trash_dir=trash)

    assert run.errors == []
    if broken:
        assert notes == ["The old copy's files are broken links. Nothing was imported."]
        assert _library_snapshot(lib) == before_rows
        assert [p for p in old_paths if not p.is_symlink()] == [], "a link left the library"
        assert _tree(outside) == before_outside, "the new album's audio landed outside"
        assert _tree(trash) == []
    else:
        assert notes == []
        assert len(list(lib.albums())) == 1
        assert [p for p in _item_paths(lib) if not p.exists()] == []
        assert [p for p in _tree(trash) if p.endswith(".flac")] != []


def test_a_refused_store_layout_imports_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Trash pair is re-checked at the moment of use, on the hook route too.

    The pair is resolved once — when the registry is handed the library at
    lifespan or after an Apply — and an import can run hours later. Pointing
    Trash AT the music library is the shape measured to cost the whole library on
    Empty Trash; here it would relocate the old copy inside the library under a
    container name. Refused, so nothing moves and nothing is imported.

    The control is ``test_replace_trashes_the_old_copy_before_the_new_one_is_placed``:
    the same route with an accepted layout, where the files do reach Trash.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    _seed_library_copy(lib, source, monkeypatch)
    music = Path(os.fsdecode(lib.directory))
    before_rows = _library_snapshot(lib)
    before_music = _tree(music)

    run, notes = _replace(lib, source, trash_dir=music)  # Trash IS the music library

    assert run.errors == []
    assert notes == [_REPLACE_NO_TRASH]
    assert _library_snapshot(lib) == before_rows
    assert _tree(music) == before_music


def test_a_run_that_fails_after_the_hook_still_repairs_the_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-export is in a ``finally``, because the rows go during ``run()``.

    The hook has already trashed the old copy and dropped its rows by the time
    beets places anything. If the run then fails, every playlist holding one of
    those tracks still has an export naming a file that is not there — so the
    repair cannot hang off a successful return.

    ``ImportTask.add`` is made to raise, which is after the duplicate hook and
    before anything lands, so no new row can reuse the freed ids: the entry is
    dropped from the export rather than re-pointed.
    """
    from beets.importer.tasks import ImportTask

    from app.playlists.reexport import export_dir_for, render_export
    from app.playlists.store import StoredEntry, create_playlist

    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()
    _seed_library_copy(lib, source, monkeypatch)
    doomed = next(iter(next(iter(lib.albums())).items()))
    record = create_playlist(
        playlists_dir, name="Mix", entries=[StoredEntry(uid="u0", item_id=_require_id(doomed.id))]
    )
    render_export(record, lib, export_dir_for(lib))
    export = export_dir_for(lib) / f"{record.id}.m3u8"
    assert "#EXTINF:" in export.read_text(encoding="utf-8")

    def boom(self: Any, lib_arg: Any) -> None:
        raise RuntimeError("the run died after the hook")

    monkeypatch.setattr(ImportTask, "add", boom)
    bridge = ImportBridge()
    run = _import(
        lib,
        source,
        bridge,
        incremental=False,
        duplicate=DuplicateAction.replace,
        trash_dir=trash,
        playlists_dir=playlists_dir,
    )

    assert run.errors != [], "the fixture must actually fail the run"
    assert [p for p in _tree(trash) if p.endswith(".flac")] != [], "the hook did trash the copy"
    body = export.read_text(encoding="utf-8")
    assert "#EXTINF:" not in body, "the export still names the file the hook moved"
    render_export(record, lib, export_dir_for(lib))
    assert export.read_text(encoding="utf-8") == body


# ----- containment: a Replace never moves a file that is not in the library -----


def test_a_replace_leaves_a_half_finished_imports_download_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery from a half-finished import must not eat the download.

    MEASURED before the containment rule: after a placement failure the library
    holds one row in the music folder and one still naming the download folder,
    and re-importing that folder — the documented repair — sent the WHOLE album
    to ``trash_album``, which moved the import's own source file out of the
    download folder. beets then skipped the vanished source, kept a row naming
    nothing, and reported ``errors: []`` with ``notes: []``. The only copy of
    that track sat in a Trash container that does not look related to the
    current album, one Empty away from gone.

    So the row is dropped and its file left alone, and the rest of the album
    goes to Trash as before. The origin record is the same fix's second half:
    with the straddling row gone, ``_album_root`` describes the album's music
    folder instead of ``commonpath(music, downloads)`` — ``/`` under the shipped
    layout, on a data-recovery surface.

    The control is ``test_replace_trashes_the_old_copy_before_the_new_one_is_placed``:
    a whole album inside the library, where every file does reach Trash.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    landed, stranded = _half_finished_import(lib, source, monkeypatch)
    before_downloads = _tree(source)
    assert stranded.is_file()

    run, notes = _replace(lib, source, trash_dir=trash)

    assert run.errors == []
    assert notes == []
    assert _tree(source) == before_downloads, "the Replace moved the import's own source file"
    # One album, and every row it holds names a file that is there.
    assert len(list(lib.albums())) == 1
    rows = _item_paths(lib)
    assert [p for p in rows if not p.exists()] == []
    assert sorted(p.name for p in rows) == ["01 Airbag 1.flac", "02 Airbag 2.flac"]
    # The half that WAS in the library left, reversibly and on its own.
    assert [p for p in _tree(trash) if p.endswith(".flac")] == [
        "Radiohead - OK Computer/Radiohead/OK Computer/01 Airbag 1.flac"
    ]
    # ``landed`` still exists, and is the NEW album's file: copy mode reuses the
    # freed name. What says the old one left is the Trash entry above.
    assert landed in rows
    records = [json.loads(p.read_text()) for p in sorted(origins_for(trash).rglob("*.json"))]
    assert len(records) == 1, records
    assert records[0]["origin"] == str(Path(os.fsdecode(lib.directory)) / _ARTIST / _ALBUM)


def test_a_replace_whose_only_move_put_nothing_in_trash_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The partial note counts containers in Trash, not calls to the mover.

    An album classifies ``present`` on the strength of a row naming a file this
    import is reading; dropping that row leaves the mover only ghost rows, so it
    rmdirs the container it made and Trash stays empty (measured by the security
    seat). Counting that as a move made the note claim a folder the user could
    go and look for: "moved 1 of 2 old copies to Trash" with one container.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    _seed_library_copy(lib, source, monkeypatch, times=2)
    ghostish, healthy = sorted(lib.albums(), key=lambda a: _require_id(a.id))
    for path in [Path(os.fsdecode(i.path)) for i in ghostish.items()]:
        path.unlink()  # its own files are gone; only the source row keeps it "present"
    _add_row_naming(lib, ghostish, source / "01 Track 1.flac")
    healthy_id = _require_id(healthy.id)
    calls: list[int] = []

    def flaky_trash(lib_arg: Any, album: Any, **kwargs: Any) -> str:
        calls.append(_require_id(album.id))
        if _require_id(album.id) == healthy_id:
            raise OSError("the container could not be written")
        return str(real_trash_album(lib_arg, album, **kwargs))

    monkeypatch.setattr(session_mod, "trash_album", flaky_trash)

    run, notes = _replace(lib, source, trash_dir=trash)

    assert run.errors == []
    assert calls[0] != healthy_id, "fixture premise: the source-row album goes first"
    assert notes == ["Replace failed while moving the old copy to Trash. Nothing was imported."]
    assert [p for p in _tree(trash) if p.endswith(".flac")] == [], "nothing reached Trash"
    assert (source / "01 Track 1.flac").is_file()


def test_a_replace_leaves_an_aliased_download_row_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ownership survives a SECOND SPELLING of the same file — the hook route.

    MEASURED by the security seat against the byte-set rule: with the stranded
    row rewritten to a symlink alias of the download folder (one inode, two
    spellings), the ownership test missed it and the mover took the import's own
    source file to Trash, left a row naming a file that is no longer there, and
    wrote an origin record naming ``commonpath(music, alias)`` — ``/`` under the
    shipped layout. That is the whole of S1 and S5 again, through a spelling.

    beets misses the alias too (``PathQuery``/``find_duplicates`` compare stored
    bytes), and that is exactly why mirroring it is not enough here: beets' miss
    only fails to exclude an album, ours drives a mover.

    The premise is asserted, not assumed: the alias and the real path are the
    same inode. The control that this does not over-match is
    ``test_a_hardlinked_library_copy_still_reaches_trash``.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    _landed, stranded = _half_finished_import(lib, source, monkeypatch)
    alias_root = tmp_path / "dl-alias"
    alias_root.symlink_to(tmp_path / "downloads")
    aliased = alias_root / source.name / stranded.name
    assert aliased.stat().st_ino == stranded.stat().st_ino, "fixture premise: one file"
    assert os.fsdecode(aliased) != os.fsdecode(stranded), "fixture premise: two spellings"
    _respell_row(lib, stranded, aliased)
    before_downloads = _tree(source)

    run, notes = _replace(lib, source, trash_dir=trash)

    assert run.errors == []
    assert notes == []
    assert _tree(source) == before_downloads, "the Replace moved the import's own source file"
    assert stranded.is_file()
    rows = _item_paths(lib)
    assert [p for p in rows if not p.exists()] == [], "a row names a file that is not there"
    assert len(list(lib.albums())) == 1
    records = [json.loads(p.read_text()) for p in sorted(origins_for(trash).rglob("*.json"))]
    assert [r["origin"] for r in records] == [
        str(Path(os.fsdecode(lib.directory)) / _ARTIST / _ALBUM)
    ], "the origin record names the album's music folder, not a common parent"


def test_a_banked_replace_leaves_an_aliased_download_row_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same second spelling, on the post-run route — measured there too.

    beets' own ``record_replaced`` / ``remove_replaced`` deletes the library
    rows whose path is one of the task's source files before this pass runs, so
    round 3 left this route with no ownership rule of its own. Both halves of
    that justification are narrower than the route: the deletion is a
    ``PathQuery`` on stored BYTES (so an aliased spelling survives it —
    measured: the download was moved to Trash with ``notes: []``), and it only
    covers tasks that reach ``task.add``.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    _seed_library_copy(lib, source, monkeypatch)
    old = next(iter(lib.albums()))
    old_id = _require_id(old.id)
    _kept, respelled_from = sorted(_item_paths(lib))
    alias_root = tmp_path / "dl-alias"
    alias_root.symlink_to(tmp_path / "downloads")
    read_by_the_run = source / "02 Track 2.flac"
    aliased = alias_root / source.name / read_by_the_run.name
    assert aliased.stat().st_ino == read_by_the_run.stat().st_ino, "fixture premise: one file"
    # The library copy's second row now names a file the run IS reading, spelled
    # through the alias; its own file goes where an earlier repair left it.
    respelled_from.unlink()
    _respell_row(lib, respelled_from, aliased)
    with lib.music_dir_context():
        old.album = "Kid A"  # renamed in the DB only: the hook never fires
        old.store()
    directive = BankApplyDirective(
        action="duplicate",
        duplicate_action=DuplicateAction.replace,
        replace_existing=[to_existing_album(lib, lib.get_album(old_id))],
    )

    assert _run_banked_replace(lib, source, trash_dir=trash, directive=directive) == []

    assert read_by_the_run.is_file(), "the post-run pass moved a file the import was reading"
    assert _tree(source) == ["01 Track 1.flac", "02 Track 2.flac"]
    assert lib.get_album(old_id) is None, "the banked copy's rows survived"
    assert [p for p in _tree(trash) if p.endswith(".flac")] == [
        "Radiohead - Kid A/Radiohead/Kid A/01 Airbag 1.flac"
    ], "its own file left; the aliased row was dropped, not moved"
    assert [p for p in _item_paths(lib) if not p.exists()] == []


def test_a_hardlinked_library_copy_still_reaches_trash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sharing an INODE with a source file is not owning it — the ownership control.

    Under ``hardlink`` every library file IS the download file: one inode, two
    directory entries. Keying ownership on the inode alone would read the whole
    old album as "the import's own", drop its rows and leave its files in the
    music folder untracked — the invisible-orphan shape S1 was fixed to prevent.

    The entry key answers this one correctly because it carries the HOLDING
    directory as well: same inode, different folder, so the album is the
    library's and goes to Trash whole.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "hardlink")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    _seed_library_copy(lib, source, monkeypatch)
    library_file = sorted(_item_paths(lib))[0]
    assert library_file.stat().st_ino == (source / "01 Track 1.flac").stat().st_ino, (
        "fixture premise: hardlink mode makes the library file the download file"
    )

    run, notes = _replace(lib, source, trash_dir=trash)

    assert run.errors == []
    assert notes == []
    assert [p for p in _tree(trash) if p.endswith(".flac")] == [
        f"{_ARTIST} - {_ALBUM}/{_ARTIST}/{_ALBUM}/01 Airbag 1.flac",
        f"{_ARTIST} - {_ALBUM}/{_ARTIST}/{_ALBUM}/02 Airbag 2.flac",
    ], "the old copy's rows were read as the import's own and dropped"
    assert _tree(source) == ["01 Track 1.flac", "02 Track 2.flac"]
    assert len(list(lib.albums())) == 1


def test_a_source_file_that_cannot_be_keyed_still_matches_its_own_bytes(
    tmp_path: Path,
) -> None:
    """What happens when the key cannot be built — stated, not guessed.

    A source path that is GONE and one whose folder cannot be read both add no
    entry to the set, so an alias of either is not recognised. Neither is read
    as "absent" or as "ours": the exact bytes still match, which is the round-3
    behaviour, and the two causes are safe for the reasons ``_SourceFiles``
    records (a gone file cannot be moved; an unreadable one makes the album
    classify ``unknown``, which refuses the whole Replace).
    """
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    present = source / "01 Track 1.flac"
    missing = tmp_path / "downloads" / "vanished" / "01 Track 1.flac"
    locked_dir = tmp_path / "downloads" / "locked"
    locked_dir.mkdir()
    locked = locked_dir / "02 Track 2.flac"
    shutil.copyfile(present, locked)

    files = session_mod._SourceFiles()
    locked_dir.chmod(0o000)
    try:
        files.note(_FakeTask([present, missing, locked]))
    finally:
        locked_dir.chmod(0o755)

    assert len(files.entries) == 1, "only the file that could be keyed"
    for path in (present, missing, locked):
        assert files.covers(lib, os.fsencode(str(path))), path
    assert not files.covers(lib, os.fsencode(str(source / "02 Track 2.flac")))


def test_a_banked_replace_takes_a_row_outside_the_music_folder_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row outside the music folder belongs to the album, so it goes WITH it.

    This route drops the rows the RUN is reading, like the hook's
    (``test_a_banked_replace_leaves_an_aliased_download_row_alone``). A row
    naming some OTHER folder is a different thing — an earlier half-finished
    import, an ``in_place`` album, a moved ``directory:`` — and that file is the
    album's own. Round 2 dropped such a row silently where the build before it
    moved it to Trash reversibly; this asserts the Trash entry.
    The copy is renamed in the DB only, which is what makes beets' name-keyed
    ``find_duplicates`` miss it and leaves this pass as the only thing running.

    The control is ``test_a_banked_replace_of_a_readable_copy_moves_it_to_trash``:
    the same pass on an album wholly inside the library.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    _seed_library_copy(lib, source, monkeypatch)
    old = next(iter(lib.albums()))
    old_id = _require_id(old.id)
    kept, stranded_from = sorted(_item_paths(lib))
    stray_dir = tmp_path / "downloads" / "an-earlier-half-done-import"
    stray_dir.mkdir(parents=True)
    stray = stray_dir / stranded_from.name
    with lib.music_dir_context():
        for item in old.items():
            if Path(os.fsdecode(item.path)) == stranded_from:
                shutil.move(stranded_from, stray)
                item.path = os.fsencode(str(stray))
                item.store()
        old.album = "Kid A"  # renamed in the DB only: the hook never fires
        old.store()
    assert sorted(_item_paths(lib)) == sorted([kept, stray])
    directive = BankApplyDirective(
        action="duplicate",
        duplicate_action=DuplicateAction.replace,
        replace_existing=[to_existing_album(lib, lib.get_album(old_id))],
    )

    assert _run_banked_replace(lib, source, trash_dir=trash, directive=directive) == []

    assert lib.get_album(old_id) is None, "the banked copy's rows survived"
    assert _tree(stray_dir) == [], "the row outside the music folder was dropped, not moved"
    assert [p for p in _tree(trash) if p.endswith(".flac")] == [
        "Radiohead - Kid A/Radiohead/Kid A/01 Airbag 1.flac",
        "Radiohead - Kid A/Radiohead/Kid A/02 Airbag 2.flac",
    ], "both of the album's files, wherever they lived; container named as the DB spells it"
    assert [p for p in _item_paths(lib) if not p.exists()] == []


def test_an_album_outside_the_music_folder_still_reaches_trash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Outside the music folder" is not "not ours to move" — the contrast to S1.

    Three supported ways an album's files live outside ``directory:``, none of
    them an accident: ``in_place`` is one of MusicDrop's own file operations
    (all five beets flags off), ``directory:`` is editable and changing it moves
    no files, and a symlinked album folder is how a library spans disks. Round 2
    asked the LOCATION question, so all three had their rows silently dropped
    on a successful Replace where the build before it moved them to Trash
    reversibly — no Trash entry, no note, and Disk Sync is disk-to-DB only, so
    the files became invisible everywhere.

    The question the S1 finding actually wanted is OWNERSHIP: only a row naming
    a file THIS import is reading is dropped. This album's file is one the
    import never opens, so it goes to Trash with an origin record naming its
    real folder — the same property
    ``test_a_replace_leaves_a_half_finished_imports_download_alone`` pins for
    the in-root case.

    The healthy sibling is a fixture premise, not scenery: with the collision's
    only file outside the music folder, that folder is EMPTY and
    ``require_library_root`` refuses the Replace before any of this (measured —
    "Library folder is empty. Is the music share mounted?"). Its album name
    differs, so it is not in the bucket.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path, name="sibling")
    trash = tmp_path / "trash"
    nas = tmp_path / "nas" / "rips" / _ALBUM
    _seed_library_copy(lib, source, monkeypatch)
    with lib.music_dir_context():  # out of the bucket, in the music folder
        sibling = next(iter(lib.albums()))
        sibling.album = "Kid A"
        sibling.store()
        sibling.move()  # and out of the destination, so the names below are free
    source = _source_folder(tmp_path)
    _out_id, out_file = _extra_matching_album(
        lib, folder=nas, name="01 Airbag 1.flac", title="Airbag 1"
    )

    run, notes = _replace(lib, source, trash_dir=trash)

    assert run.errors == []
    assert notes == []
    # NOT by id: the hook drops the rows before beets inserts, so the new album
    # can take the freed rowid. The file and the Trash entry are the oracle.
    assert not out_file.exists(), "the file stayed put and the rows were dropped silently"
    assert _tree(nas) == [], "its folder still holds the file"
    assert [p for p in _tree(trash) if p.endswith(".flac")] == [
        f"{_ARTIST} - {_ALBUM}/{_ARTIST}/{_ALBUM}/01 Airbag 1.flac"
    ]
    records = [json.loads(p.read_text()) for p in sorted(origins_for(trash).rglob("*.json"))]
    assert [r["origin"] for r in records] == [str(nas)], "the origin record names its real folder"
    # And the import landed, on the clean names: the collision left first.
    with lib.music_dir_context():
        landed = [a for a in lib.albums() if a.album == _ALBUM]
        assert len(landed) == 1, [a.album for a in lib.albums()]
        names = sorted(Path(os.fsdecode(i.path)).name for i in landed[0].items())
    assert names == ["01 Airbag 1.flac", "02 Airbag 2.flac"]
    assert [p for p in _item_paths(lib) if not p.exists()] == []


# ----- the bank route: a decision that no longer fits the library is STALE -----

_STALE = "The library changed since this was set aside. Decide again."


def test_a_bank_replace_refuses_when_the_bucket_gained_an_album(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A banked decision that no longer accounts for the collision is stale consent.

    MEASURED before any of this: a directive built at sweep time, applied
    against a library that had since gained a second matching album, disposed of
    BOTH — the second one's rows dropped and its file moved to Trash, having
    appeared on no prompt. The attended route cannot do that (the prompt's
    ``existing`` list and the disposal read one list, in one call, with the park
    in between); a directive can, because ``found_duplicates`` is re-derived
    hours later.

    Disposing of the intersection and importing beside the rest was the wrong
    repair, measured twice: it put a consent filter above the all-or-nothing
    gates (``test_a_bucket_member_the_prompt_did_not_name_is_still_classified``),
    and where it did dispose it left the user an unasked-for second copy with
    the new album permanently filed at ``01 Airbag 1.1.flac``, and nothing on
    the wire to say so.

    So the whole Replace refuses: nothing moved, nothing imported, one sentence,
    and the bank row fails retryable so the user decides again against what the
    library now holds.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    music = Path(os.fsdecode(lib.directory))
    _seed_library_copy(lib, source, monkeypatch)
    banked = next(iter(lib.albums()))
    banked_id = _require_id(banked.id)
    banked_paths = _item_paths(lib)
    directive = BankApplyDirective(
        action="duplicate",
        duplicate_action=DuplicateAction.replace,
        replace_existing=[to_existing_album(lib, banked)],
    )
    later_id, later_file = _extra_matching_album(
        lib, folder=music / _ARTIST / "OK Computer (2nd rip)"
    )
    before_rows = _library_snapshot(lib)

    notes = _bank_replace_notes(lib, source, trash_dir=trash, directive=directive)

    assert notes == [_STALE]
    # BOTH albums untouched, rows and files, and nothing imported.
    assert lib.get_album(banked_id) is not None, "the banked album's rows were dropped"
    assert lib.get_album(later_id) is not None, "the unnamed album's rows were dropped"
    assert [p for p in banked_paths if not p.exists()] == []
    assert later_file.is_file()
    assert _library_snapshot(lib) == before_rows
    assert _tree(trash) == []


def test_a_stale_consent_refusal_can_be_decided_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal's remedy, end to end on the session side.

    MEASURED by the security seat: "Decide again" re-queued the row with the
    SAME stored prompt, so the next apply rebuilt the same consent set and
    refused identically — forever. The one writer that cleared the prompt
    (rescan) cleared it to None, which the gate reads as "nothing to compare"
    and allows unbounded.

    So the refusing apply PUBLISHES the collision it saw, non-blocking: the feed
    row carries it, the apply runner writes it onto the bank row
    (``test_a_published_prompt_refreshes_the_failed_rows_collision``), and the
    user's next decision is made against what the library now holds. Here the
    second directive is built from the published prompt exactly as the runner
    would rebuild it, and it succeeds: the whole bucket goes, and the import
    lands on the clean names.

    Publishing must not make the run look blocked — nobody will answer this
    prompt — so ``has_unanswered_park`` is asserted False.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    music = Path(os.fsdecode(lib.directory))
    _seed_library_copy(lib, source, monkeypatch)
    banked = next(iter(lib.albums()))
    banked_id = _require_id(banked.id)
    stale_directive = BankApplyDirective(
        action="duplicate",
        duplicate_action=DuplicateAction.replace,
        replace_existing=[to_existing_album(lib, banked)],
    )
    later_id, later_file = _extra_matching_album(
        lib, folder=music / _ARTIST / "OK Computer (2nd rip)"
    )

    bridge = ImportBridge()
    assert (
        _run_banked_replace(lib, source, trash_dir=trash, directive=stale_directive, bridge=bridge)
        == []
    )
    assert [o.note for o in bridge.drain_outcomes() if o.note is not None] == [_STALE]
    refreshed = bridge.get_parked_duplicate(timeout=0)
    assert refreshed is not None, "the refusal published no prompt, so deciding again cannot work"
    assert bridge.has_unanswered_park() is False, "a published prompt must not block the job"
    assert sorted(e.album_id for e in refreshed.existing) == sorted([banked_id, later_id])

    # Decide again, on the refreshed collision.
    assert (
        _run_banked_replace(
            lib,
            source,
            trash_dir=trash,
            directive=BankApplyDirective(
                action="duplicate",
                duplicate_action=DuplicateAction.replace,
                replace_existing=list(refreshed.existing),
            ),
        )
        == []
    )

    assert not later_file.exists(), "the second decision left the album it was shown"
    assert [p for p in _tree(trash) if p.endswith(".flac")] == [
        f"{_ARTIST} - {_ALBUM} (1)/{_ARTIST}/{_ALBUM}/01 Later.flac",
        f"{_ARTIST} - {_ALBUM}/{_ARTIST}/{_ALBUM}/01 Airbag 1.flac",
        f"{_ARTIST} - {_ALBUM}/{_ARTIST}/{_ALBUM}/02 Airbag 2.flac",
    ]
    assert len(list(lib.albums())) == 1
    assert sorted(p.name for p in _item_paths(lib)) == ["01 Airbag 1.flac", "02 Airbag 2.flac"]


def test_a_bank_replace_still_disposes_of_a_bucket_its_prompt_covers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: a directive that DOES account for the bucket disposes of all of it.

    Same fixture, same second album, one difference — the directive names it
    too. Without this, "refuses" could pass on a build that refused every
    banked Replace.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    music = Path(os.fsdecode(lib.directory))
    _seed_library_copy(lib, source, monkeypatch)
    banked = next(iter(lib.albums()))
    later_id, later_file = _extra_matching_album(
        lib, folder=music / _ARTIST / "OK Computer (2nd rip)"
    )
    directive = BankApplyDirective(
        action="duplicate",
        duplicate_action=DuplicateAction.replace,
        replace_existing=[
            to_existing_album(lib, banked),
            to_existing_album(lib, lib.get_album(later_id)),
        ],
    )

    assert _run_banked_replace(lib, source, trash_dir=trash, directive=directive) == []

    assert [p for p in _tree(trash) if p.endswith(".flac")] == [
        f"{_ARTIST} - {_ALBUM} (1)/{_ARTIST}/{_ALBUM}/01 Later.flac",
        f"{_ARTIST} - {_ALBUM}/{_ARTIST}/{_ALBUM}/01 Airbag 1.flac",
        f"{_ARTIST} - {_ALBUM}/{_ARTIST}/{_ALBUM}/02 Airbag 2.flac",
    ]
    assert not later_file.exists(), "the album the directive named was left in place"
    assert len(list(lib.albums())) == 1
    assert [p for p in _item_paths(lib) if not p.exists()] == []
    # The filename oracle: both copies left BEFORE placement, so the new album
    # took the clean names. A refusal would leave both in the way, and beets'
    # ``unique_path`` would have filed the new album at ``01 Airbag 1.1.flac``.
    assert sorted(p.name for p in _item_paths(lib)) == ["01 Airbag 1.flac", "02 Airbag 2.flac"]


def test_a_bucket_member_the_prompt_did_not_name_is_still_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gates read beets' WHOLE live bucket, before consent is consulted.

    MEASURED by the security seat with the consent filter above them: a bucket
    member the consent set excluded was never classified, the hook returned
    ``KEEP``, and beets wrote the new album's two FLACs THROUGH that member's
    dangling links to outside the music library — ``notes: []``, Trash empty,
    nothing reading as missing, and any later cleanup of that location
    destroying the user's only copy.

    The refusals are not about what we may dispose of; they are about where
    beets is about to WRITE and what it cannot read. So the broken-link sentence
    is what comes back here, not the stale-consent one — the gate fires first,
    on an album no prompt named.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    music = Path(os.fsdecode(lib.directory))
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    _seed_library_copy(lib, source, monkeypatch)
    banked = next(iter(lib.albums()))
    directive = BankApplyDirective(
        action="duplicate",
        duplicate_action=DuplicateAction.replace,
        replace_existing=[to_existing_album(lib, banked)],
    )
    # The member nobody banked, whose one file is a link to nowhere.
    stranger_id, stranger_file = _extra_matching_album(
        lib, folder=music / _ARTIST / "OK Computer (stranger)"
    )
    stranger_file.unlink()
    stranger_file.symlink_to(outside / "gone.flac")
    assert stranger_file.is_symlink()
    assert not stranger_file.exists()  # the premise: it resolves to nothing
    before_rows = _library_snapshot(lib)

    notes = _bank_replace_notes(lib, source, trash_dir=trash, directive=directive)

    assert notes == ["The old copy's files are broken links. Nothing was imported."]
    assert _tree(outside) == [], "the new album's audio was written outside the library"
    assert lib.get_album(stranger_id) is not None
    assert _library_snapshot(lib) == before_rows
    assert stranger_file.is_symlink(), "a link left the library"
    assert _tree(trash) == []


# ----- order and freshness inside the disposal loop -----


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits this test sets")
def test_the_second_of_two_duplicates_being_unreadable_refuses_the_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The up-front classification covers EVERY duplicate, not the first.

    MEASURED by the code seat with the filter narrowed to ``states[0]``: the
    whole suite stayed green while a two-duplicate Replace trashed album 1,
    dropped album 2's rows on an EACCES read and left album 2's files on disk
    with no rows and no note.

    The premise is the POSITION: the prompt below is asserted to name the
    readable copy first, so a check that only asks about the first duplicate
    cannot see this one.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    _seed_library_copy(lib, source, monkeypatch, times=2)
    first, second = sorted(lib.albums(), key=lambda a: _require_id(a.id))
    second_id = _require_id(second.id)
    second_paths = _refile_into_its_own_folder(
        lib, second, Path(os.fsdecode(lib.directory)) / _ARTIST / "OK Computer (2)"
    )
    before_rows = _library_snapshot(lib)
    second_paths[0].parent.chmod(0o000)
    try:
        run, notes = _replace(lib, source, trash_dir=trash)
    finally:
        second_paths[0].parent.chmod(0o755)

    assert run.errors == []
    assert len(run.duplicates) == 1, run.duplicates
    assert [e.album_id for e in run.duplicates[0].existing] == [
        _require_id(first.id),
        second_id,
    ], "the premise: the unreadable duplicate is the SECOND one"
    assert notes == [
        "Replace could not read the old copy's files, so nothing was moved or imported."
    ]
    assert _library_snapshot(lib) == before_rows, "rows changed on a refused Replace"
    assert [p for p in second_paths if not p.exists()] == []
    assert _tree(trash) == []


def test_two_duplicates_over_one_file_set_replace_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One file set, two albums: the second is a ghost by the time its turn comes.

    A ``hardlink`` keep-both builds this by construction — measured, albums 1
    and 2 name the SAME two paths. The up-front pass classifies both as
    ``present``; the first disposal moves the files; and an arm chosen from that
    stale reading sent album 2 to ``trash_album``, which moved nothing and then
    refused to drop the rows, leaving the library's only album naming two files
    that are not there and importing nothing (measured by the code seat).

    Re-reading each album's state at its own turn makes the second take the
    row-drop arm, so both copies go and the import lands.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "hardlink")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    _seed_library_copy(lib, source, monkeypatch, times=2)
    shared = sorted({os.fsdecode(i.path) for a in lib.albums() for i in a.items()})
    assert len(shared) == 2, "the premise: two albums over ONE set of two files"

    run, notes = _replace(lib, source, trash_dir=trash)

    assert run.errors == []
    assert notes == []
    assert len(list(lib.albums())) == 1
    assert [p for p in _item_paths(lib) if not p.exists()] == []
    assert [p for p in _tree(trash) if p.endswith(".flac")] == [
        f"{_ARTIST} - {_ALBUM}/{_ARTIST}/{_ALBUM}/01 Airbag 1.flac",
        f"{_ARTIST} - {_ALBUM}/{_ARTIST}/{_ALBUM}/02 Airbag 2.flac",
    ], "one container: the second album had nothing left of its own to move"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits this test sets")
def test_a_duplicate_that_turns_unreadable_mid_pass_stops_the_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closed ``else``: a state that is neither here nor gone raises.

    Reachable because the state is re-read per album — the folder is made
    unreadable between the up-front classification and the second album's own
    turn. Falling through to the row-drop instead would drop the rows of an
    album whose files are all present, which is the shape the three-valued
    classification exists to prevent.

    The unanswerable album also carries a row naming one of the import's OWN
    source files, so this pins WHERE that row-drop sits: inside the ``present``
    arm, after the arm is chosen. Above the arm choice it would run for this
    album too, and a refusal would have cost it a row.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    _seed_library_copy(lib, source, monkeypatch, times=2)
    _first, second = sorted(lib.albums(), key=lambda a: _require_id(a.id))
    second_id = _require_id(second.id)
    second_paths = _refile_into_its_own_folder(
        lib, second, Path(os.fsdecode(lib.directory)) / _ARTIST / "OK Computer (2)"
    )
    source_row = _add_row_naming(lib, second, source / "01 Track 1.flac")
    # Fixture premise, measured rather than assumed: the row must name a file
    # the import is actually reading (a source FILE, not a library name), and
    # it must belong to the album that is about to be refused. With either half
    # wrong the assertion below passes whatever the code does.
    assert source_row.exists(), "the row must name one of the task's own source files"
    with lib.music_dir_context():
        second_rows = [Path(os.fsdecode(i.path)) for i in second.items()]
    assert source_row in second_rows, "the row must belong to the album that gets refused"
    folder = second_paths[0].parent

    def then_lock_the_next_one(lib_arg: Any, album: Any, **kwargs: Any) -> str:
        moved = str(real_trash_album(lib_arg, album, **kwargs))
        folder.chmod(0o000)  # the race, made deterministic
        return moved

    monkeypatch.setattr(session_mod, "trash_album", then_lock_the_next_one)
    try:
        run, notes = _replace(lib, source, trash_dir=trash)
    finally:
        folder.chmod(0o755)

    assert run.errors == []
    assert notes == ["Replace moved 1 of 2 old copies to Trash, then failed. Nothing was imported."]
    assert lib.get_album(second_id) is not None, "an unanswerable album's rows were dropped"
    assert [p for p in second_paths if not p.exists()] == []
    assert source_row in _item_paths(lib), "the row-drop ran before the arm was chosen"
    assert len(list(lib.albums())) == 1, "something was imported after a refused Replace"


def test_a_failed_move_beside_a_ghost_costs_the_ghost_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Albums with files go first, so a failed move never costs a ghost its rows.

    MEASURED in the other order: the ghost's rows were dropped, the next move
    failed, and the note read "Replace moved 1 of 2 old copies to Trash" with
    Trash empty — a sentence about moves, counting a row-drop. Nothing was
    imported either, so the user lost a ghost's metadata for no gain.

    Present-first makes the only reachable order the honest one: the move is
    attempted first, fails, and the pass stops before any ghost is touched.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    _seed_library_copy(lib, source, monkeypatch, times=2)
    ghost, present = sorted(lib.albums(), key=lambda a: _require_id(a.id))
    ghost_id = _require_id(ghost.id)
    ghost_paths = _refile_into_its_own_folder(
        lib, ghost, Path(os.fsdecode(lib.directory)) / _ARTIST / "OK Computer (2)"
    )
    for path in ghost_paths:  # rows, no files: a ghost at position ONE
        path.unlink()
    present_paths = [Path(os.fsdecode(i.path)) for i in present.items()]
    before_rows = _library_snapshot(lib)

    def boom(lib_arg: Any, album: Any, **kwargs: Any) -> str:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(session_mod, "trash_album", boom)

    run, notes = _replace(lib, source, trash_dir=trash)

    assert run.errors == []
    assert notes == ["Replace failed while moving the old copy to Trash. Nothing was imported."]
    assert lib.get_album(ghost_id) is not None, "the ghost paid for the other album's failure"
    assert _library_snapshot(lib) == before_rows
    assert [p for p in present_paths if not p.exists()] == []
    assert _tree(trash) == []


def test_a_raise_after_the_files_moved_still_repairs_the_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The item ids are read BEFORE the disposal call, not after it.

    ``trash_album`` can raise once the files have already moved — the
    origin-record write, or ``album.remove`` — and then the rows still name the
    Trash location while every export naming them is stale. MEASURED with the
    ids recorded after the call: ``export rewritten: False``, and the export
    still named a music-folder file that is not there.

    Recording first costs at most one redundant render (the re-export draws from
    the store as it finally is), and this is the invariant it buys: when the run
    ends, the export on disk equals a fresh render.
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
        playlists_dir, name="Mix", entries=[StoredEntry(uid="u0", item_id=_require_id(doomed.id))]
    )
    render_export(record, lib, export_dir_for(lib))
    export = export_dir_for(lib) / f"{record.id}.m3u8"
    doomed_line = os.path.relpath(os.fsdecode(doomed.path), str(export_dir_for(lib)))
    assert doomed_line in export.read_text(encoding="utf-8")

    def move_then_die(lib_arg: Any, album: Any, *, trash_dir: Path, origins_dir: Path) -> str:
        album.move(basedir=os.fsencode(str(trash_dir / "container")))
        raise OSError(errno.EIO, "died after the files moved")

    monkeypatch.setattr(session_mod, "trash_album", move_then_die)
    bridge = ImportBridge()
    run = _import(
        lib,
        source,
        bridge,
        incremental=False,
        duplicate=DuplicateAction.replace,
        trash_dir=trash,
        playlists_dir=playlists_dir,
    )

    assert run.errors == []
    assert [o.note for o in bridge.drain_outcomes() if o.note is not None] == [
        "Replace failed while moving the old copy to Trash. Nothing was imported."
    ]
    assert [p for p in _tree(trash) if p.endswith(".flac")] != [], "the fixture must move files"
    after_run = export.read_text(encoding="utf-8")
    assert doomed_line not in after_run, "the export still names a file that moved"
    render_export(record, lib, export_dir_for(lib))
    assert export.read_text(encoding="utf-8") == after_run


# ----- the banked pass: same inode is not the same directory entry -----


def test_a_refiled_hardlink_sibling_still_reaches_trash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under ``hardlink`` the old copy and the new album are one inode by design.

    Two hardlinks of one file are two directory entries. Moving the old album's
    entry does not touch the new album's, so the old folder is trashable — and
    reading "shares an inode" as "shares a file" left it behind as untracked
    files in the library, invisible to disk sync, with the only trace a WARNING
    (measured by the code seat).

    So identity is the ENTRY, not the inode: the file's ``(st_dev, st_ino)`` AND
    its holding directory's. The over-refusal that remains is two hardlinks of
    one file side by side in ONE folder, which is left in place.

    The controls either way are two tests above:
    ``test_a_banked_replace_sharing_a_file_drops_rows_and_moves_nothing`` (one
    entry, two albums) and
    ``test_a_link_mode_import_onto_a_symlinked_source_shares_the_file`` (a link
    chain ending at the old entry).
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "hardlink")
    trash = tmp_path / "trash"
    source, old_id, old_paths, directive = _banked_renamed_copy(lib, tmp_path, monkeypatch)
    source_inodes = {p.stat().st_ino for p in sorted(source.glob("*.flac"))}
    assert {p.stat().st_ino for p in old_paths} == source_inodes, (
        "the premise: the old copy is hardlinked to the download"
    )
    assert {p.parent for p in old_paths} != {source}, "and it lives in its own folder"

    assert _run_banked_replace(lib, source, trash_dir=trash, directive=directive) == []

    landed = _item_paths(lib)
    assert {p.stat().st_ino for p in landed} == source_inodes, (
        "the premise: the landed album is the SAME inodes at new paths"
    )
    assert lib.get_album(old_id) is None
    assert [p for p in old_paths if p.exists()] == [], "the old folder was left untracked"
    assert [p for p in _tree(trash) if p.endswith(".flac")] != []
    assert [p for p in _tree(origins_for(trash)) if p.endswith(".json")] != []
    assert [p for p in landed if not p.exists()] == []


def test_an_unreadable_landed_file_skips_the_whole_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One EACCES blinds BOTH sides of the comparison, so both are pinned.

    The album's own side is
    ``test_an_unreadable_file_leaves_the_banked_copy_in_place``. This is the
    landed side: a file of the album this run just imported that cannot be
    stat-ed has no identity either, so no duplicate can be ruled out as sharing
    it and the pass moves nothing at all. Fault-injected at
    ``_landed_file_identities`` rather than by permissions, because the landed
    album does not exist until the run is already inside the pass.

    The control is ``test_a_banked_replace_of_a_readable_copy_moves_it_to_trash``:
    the same fixture without the injection, where the files reach Trash.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    trash = tmp_path / "trash"
    source, old_id, old_paths, directive = _banked_renamed_copy(lib, tmp_path, monkeypatch)

    def blind(lib_arg: Any, landed_album_ids: Any) -> tuple[set[Any], bool]:
        return set(), True

    monkeypatch.setattr(session_mod, "_landed_file_identities", blind)
    with caplog.at_level(logging.WARNING, logger="app.beets.import_session"):
        assert _run_banked_replace(lib, source, trash_dir=trash, directive=directive) == []

    assert lib.get_album(old_id) is not None, "the banked copy's rows were dropped"
    assert [p for p in old_paths if not p.exists()] == [], "its files were moved"
    assert _tree(trash) == []
    assert any("a shared file could not be ruled out" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def _album_naming_a_path_through_a_file(lib: Library) -> Any:
    """One DB-only album whose row names ``<artist>/cover.jpg/01.flac``.

    A path whose parent is a regular FILE: ``stat`` answers ENOTDIR, not ENOENT.
    """
    from beets.library import Item

    blocker = Path(os.fsdecode(lib.directory)) / _ARTIST / "cover.jpg"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_bytes(b"jpeg")
    with lib.music_dir_context():
        return lib.add_album(
            [Item(albumartist=_ARTIST, album=_ALBUM, path=os.fsencode(str(blocker / "01.flac")))]
        )


def test_a_row_whose_path_runs_through_a_file_reads_as_gone(tmp_path: Path) -> None:
    """ENOTDIR is a missing path, not an unreadable one — classification side.

    Reading it as "could not be read" would refuse every Replace that met such a
    row instead of treating the album as the ghost it is.
    """
    from app.beets.import_session import _album_file_state, _FileState

    lib = _library(tmp_path, "copy")
    album = _album_naming_a_path_through_a_file(lib)
    with lib.music_dir_context():
        assert _album_file_state(lib, album) is _FileState.absent


def test_a_row_whose_path_runs_through_a_file_is_not_unreadable(tmp_path: Path) -> None:
    """The same ENOTDIR on the identity side of the banked pass.

    "Could not be read" there means the album keeps its rows AND its files, so a
    row that simply names nowhere must not trip it — otherwise one such row
    freezes the whole pass.
    """
    from app.beets.import_session import _file_identities

    lib = _library(tmp_path, "copy")
    album = _album_naming_a_path_through_a_file(lib)
    with lib.music_dir_context():
        assert _file_identities(lib, album) == (set(), False)
