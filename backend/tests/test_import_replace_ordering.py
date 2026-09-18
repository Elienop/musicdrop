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
from beets import config
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


def test_a_banked_replace_sharing_a_file_drops_rows_and_moves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    (measured in round 1b — beets imports no ``cover.jpg`` from a download folder
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

    after_run = export.read_text(encoding="utf-8")
    entries = [line for line in after_run.splitlines() if line and not line.startswith("#")]
    assert len(entries) == 1, after_run
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

    Counting the art as presence instead (which is what this test asserted
    before fix round 1b) sent the album to ``trash_album``, which REFUSES here:
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


def test_a_duplicate_of_dangling_symlinks_keeps_its_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEASURED: what Trash does with an album whose files are broken links.

    ``lstat`` succeeds on a dangling symlink, so the album classifies as PRESENT
    rather than as a ghost. ``trash_album`` then moves nothing — beets skips a
    move whose source ``Path.exists()`` is False, and a dangling link is False
    (``library/models.py:1178-1192``) — and its post-condition refuses to drop
    the rows on a move that did not happen. The links stay, the rows stay,
    nothing is imported.

    The alternative reading, "the target is missing so this is a ghost", drops
    the rows and leaves the links behind: measured with another healthy album in
    the library, where the ghost arm's liveness check passes. It then also
    breaks the import itself, because beets copies the new file onto a path a
    dangling link still occupies (``FilesystemError: No such file or directory``).
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    _seed_library_copy(lib, source, monkeypatch)
    old_paths = _item_paths(lib)
    for path in old_paths:  # the file becomes a link to nowhere
        path.unlink()
        path.symlink_to(tmp_path / "gone" / path.name)
        assert path.is_symlink()
        assert not path.exists()  # the premise: the link resolves to nothing
    before_rows = _library_snapshot(lib)

    run, notes = _replace(lib, source, trash_dir=trash)

    assert run.errors == []
    assert notes == ["Replace failed while moving the old copy to Trash. Nothing was imported."]
    assert [p for p in old_paths if not p.is_symlink()] == [], "a link left the library"
    assert _library_snapshot(lib) == before_rows
    assert _tree(trash) == []


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
