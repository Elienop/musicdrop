"""Real beets, real files: what a HARDLINK import does to the same folder twice.

A hardlink leaves the download in place, so the folder is still there to be
added again — and beets would meet the album a second time. beets' own import
history is what refuses that (``importer/session.py:246-256``), it is only
written when ``incremental`` is on (``importer/tasks.py:301-305``), and
``run_import_worker`` is what turns both keys on for a hardlink run.

Nothing here is faked except the MusicBrainz lookup (``tag_album``): the
library is a real ``Library``, the files are real FLACs hardlinked into a real
music dir, and the history is a real pickle under ``tmp_path``. ``statefile``
is repointed per test on purpose — the suite's BEETSDIR is process-wide, so the
default ``state.pickle`` would carry one test's history into the next.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import beets.importer.tasks as beets_tasks
import pytest
from beets import config
from beets.autotag import AlbumInfo, AlbumMatch, TrackInfo
from beets.autotag.distance import distance
from beets.autotag.match import Proposal, assign_items
from beets.autotag.match import Recommendation as BeetsRec

from app.beets.import_session import ImportBridge, WebImportSession, run_import_worker
from app.beets.library import _require_id, get_album_detail
from app.models.album import OutsideLibrary
from app.models.import_models import (
    DuplicateAction,
    DuplicateDecision,
    DuplicatePrompt,
    ImportAction,
    ImportChoice,
    ParkedAlbum,
)

if TYPE_CHECKING:
    from beets.library import Library

_ALBUM = "OK Computer"
_ARTIST = "Radiohead"
#: How long a run may take before the test calls it hung. Generous: these are
#: real imports, and the cost of being wrong is a suite that never finishes.
_DEADLINE_S = 30.0
#: The window the helper spends unwinding a worker that outlived the loop. A
#: pause reaches the worker at its next decision hook, so this only has to cover
#: one answer plus one hook — measured at ~0.1s per whole import here.
_UNWIND_S = 5.0


def _source_folder(tmp_path: Path, name: str = "okc") -> Path:
    """Two tagged FLACs in their own folder — one album-shaped toppath."""
    from mediafile import MediaFile

    sample = Path(__file__).parent / "fixtures" / "silent.flac"
    source = tmp_path / "downloads" / name
    source.mkdir(parents=True)
    for i in (1, 2):
        dst = source / f"{i:02d} Track {i}.flac"
        shutil.copyfile(sample, dst)
        mf = MediaFile(str(dst))
        mf.artist = _ARTIST
        mf.albumartist = _ARTIST
        mf.album = _ALBUM
        mf.title = f"Airbag {i}"
        mf.track = i
        mf.save()
    return source


def _install_lookup(monkeypatch: pytest.MonkeyPatch, rec: BeetsRec) -> None:
    """Pin beets' lookup to one canned match at ``rec``, built from the items."""

    def fake_tag_album(items: Any, search_ids: Any = None) -> tuple[str, str, Proposal]:
        item_list = list(items)
        tracks = [
            TrackInfo(title=f"Airbag {i}", track_id=f"t{i}", index=i, length=1.0)
            for i in range(1, len(item_list) + 1)
        ]
        info = AlbumInfo(
            tracks=tracks,
            album=_ALBUM,
            artist=_ARTIST,
            album_id="mb-okc",
            data_source="MusicBrainz",
            data_url="https://mb/okc",
            year=1997,
            va=False,
        )
        pairs, extra_items, extra_tracks = assign_items(item_list, info.tracks)
        match = AlbumMatch(
            distance(item_list, info, pairs), info, dict(pairs), extra_items, extra_tracks
        )
        return (_ARTIST, _ALBUM, Proposal([match], rec))

    def fake_tag_item(item: Any, search_ids: Any = None) -> Proposal:
        return Proposal([], BeetsRec.none)

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)
    monkeypatch.setattr(beets_tasks, "tag_item", fake_tag_item)


def _library(tmp_path: Path, operation: str = "hardlink") -> Library:
    """The user's config, a real library and a per-test history file.

    Source and library share ``tmp_path`` so a hardlink is possible — beets
    raises on EXDEV rather than falling back. ``operation`` is the user's own
    filing flag: ``hardlink`` is the switch this slice serves; ``copy`` and
    ``link`` keep the download without opting into anything.
    """
    from tests.conftest import build_library

    config["import"]["copy"] = operation == "copy"
    config["import"][operation] = True
    config["statefile"] = str(tmp_path / "state.pickle")
    music = tmp_path / "music"
    music.mkdir()
    return build_library(str(tmp_path / "library.db"), str(music))


@dataclass
class _Run:
    """What one import did: its errors, and everything it parked."""

    errors: list[str] = field(default_factory=list)
    parked: list[ParkedAlbum] = field(default_factory=list)
    duplicates: list[DuplicatePrompt] = field(default_factory=list)


def _import(
    lib: Library,
    source: Path,
    bridge: ImportBridge,
    *,
    choice: ImportAction = ImportAction.skip,
    duplicate: DuplicateAction = DuplicateAction.skip_new,
    trash_dir: Path | None = None,
    playlists_dir: Path | None = None,
    answer_for: float = _DEADLINE_S,
    **kwargs: Any,
) -> _Run:
    """Run one import on its own thread, answering whatever it parks.

    EVERY import here goes through this helper, including the ones that should
    not park at all: a run that parks unexpectedly blocks its thread forever,
    and the suite has no pytest timeout — inline, that hangs pytest instead of
    failing one test.

    What the code does on the way out, whatever happened inside: if the worker
    is still running when the answering loop ends — its deadline, or a raise
    from either push — the ``finally`` asks the bridge to pause and keeps
    answering both channels for ``_UNWIND_S``. That matters because a worker
    blocked in ``park`` holds the process-global config-force lock: an answer
    releases the block, and ``_check_pause`` then raises beets' own
    ``ImportAbortError`` at the next decision hook, which unwinds
    ``_config_force_lock``. Without it every later import in the process
    refuses with ``ImportConfigBusyError`` and one failure here reads as a file
    of unrelated ones. It is not a guarantee the thread ends — a worker wedged
    somewhere the bridge cannot reach stays wedged — so the assert below still
    stands.

    ``answer_for`` is the answering window; tests override it to build the
    abandoned-park case deliberately.

    ``trash_dir`` wires the post-run Replace pass; without BOTH it and its
    origin sibling the pass skips itself and a Replace leaves the old album in
    the library. ``playlists_dir`` wires the run's single `.m3u8` re-export
    point; unwired, a Replace repairs no export (the shape most tests want).
    """
    from tests.conftest import origins_for

    result = _Run()

    def worker() -> None:
        session = WebImportSession(
            lib,
            None,
            [os.fsencode(str(source))],
            None,
            bridge,
            trash_dir,
            trash_origins_dir=None if trash_dir is None else origins_for(trash_dir),
            unattended=False,
            sweep=False,
            bank_dir=None,
            directive=None,
            playlists_dir=playlists_dir,
        )
        try:
            run_import_worker(session, **kwargs)
        except Exception as exc:  # reported, never swallowed
            result.errors.append(f"{exc.__class__.__name__}: {exc}")

    def answer_once() -> None:
        album = bridge.get_parked(timeout=0.05)
        if album is not None:
            result.parked.append(album)
            bridge.push_choice(album.album_index, ImportChoice(action=choice))
            return
        prompt = bridge.get_parked_duplicate(timeout=0.05)
        if prompt is not None:
            result.duplicates.append(prompt)
            bridge.push_duplicate_decision(prompt.album_index, DuplicateDecision(action=duplicate))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + answer_for
        while thread.is_alive() and time.monotonic() < deadline:
            answer_once()
    finally:
        if thread.is_alive():
            bridge.request_pause()
            unwind = time.monotonic() + _UNWIND_S
            while thread.is_alive() and time.monotonic() < unwind:
                # A push can raise here (the slot we are unblocking may already
                # hold an answer); the point is to end the thread, not to
                # record what it parked.
                with contextlib.suppress(Exception):
                    answer_once()
        thread.join(timeout=_UNWIND_S)
    assert not thread.is_alive(), "import worker hung"
    return result


def _item_paths(lib: Library) -> list[Path]:
    """Every library item's file path, resolved the way beets resolves it."""
    with lib.music_dir_context():
        return [Path(os.fsdecode(item.path)) for item in lib.items()]


def _stop_during_placement(
    monkeypatch: pytest.MonkeyPatch, operation: str, nth: int
) -> dict[str, bool]:
    """Fail the ``nth`` file placement the way a cross-device hardlink fails.

    beets raises ``FilesystemError`` out of ``util.hardlink`` on EXDEV
    (``B/util/__init__.py:586-592``) and the import run does not catch it, so an
    injected raise from the same function reaches exactly the same code. The
    filing flag and the ``util`` function ``Item.move_file`` calls for it share a
    name (``B/library/models.py:1046-1080``). Returns an ``armed`` dict the
    caller flips off before the repair run.
    """
    from beets import util
    from beets.util import FilesystemError

    real = getattr(util, operation)
    armed = {"on": True}
    calls = {"n": 0}

    def guarded(path: Any, dest: Any, *args: Any, **kwargs: Any) -> Any:
        if armed["on"]:
            calls["n"] += 1
            if calls["n"] == nth:
                raise FilesystemError(
                    "Cannot hard link across devices", "link", (path, dest), "injected"
                )
        return real(path, dest, *args, **kwargs)

    monkeypatch.setattr(util, operation, guarded)
    return armed


def _outside(lib: Library, album: Any) -> Any:
    """The album detail's ``outside_library``, for an album object."""
    detail = get_album_detail(lib, _require_id(album.id))
    assert detail is not None
    return detail.outside_library


@pytest.mark.parametrize("nth", [2, 1])
def test_a_stop_during_placement_leaves_rows_naming_the_download(
    nth: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a half-finished import leaves behind, in-process — slice 5's subject.

    beets adds the rows at ``task.add`` (the ``user_query`` stage) and places
    files in the last stage, so a stop during placement leaves the placed rows on
    library paths and the unplaced ones on the DOWNLOAD path. ``nth=2`` stops
    after one track landed; ``nth=1`` is what a cross-device hardlink does, where
    every track fails so the first one does.

    Measured here, not assumed: the run raises, no album is repaired, the detail
    names the download folder either way, and only the ``nth=1`` shape — every
    row in that one folder — may be offered the remedy.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "hardlink")
    source = _source_folder(tmp_path)
    _stop_during_placement(monkeypatch, "hardlink", nth)

    run = _import(lib, source, ImportBridge())
    assert run.errors != [], "the placement failure was swallowed"
    assert "Cannot hard link across devices" in run.errors[0]

    (album,) = list(lib.albums())
    outside_rows = [p for p in _item_paths(lib) if source in p.parents]
    assert len(outside_rows) == 3 - nth  # nth=2 -> one row left behind, nth=1 -> both
    assert sorted(p.name for p in source.iterdir()) == ["01 Track 1.flac", "02 Track 2.flac"]

    outside = _outside(lib, album)
    assert outside is not None
    assert outside.folder == str(source)
    assert outside.holds_every_track is (nth == 1)


@pytest.mark.parametrize("operation", ["move", "copy", "hardlink"])
def test_a_straddling_stop_is_not_offered_the_remedy(
    operation: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop AFTER a track landed leaves rows in two folders, so no remedy.

    The withholding and its REASON, in one test. Re-adding the named folder here
    reaches beets' duplicate question (the placed row is not one of the task's
    source paths, so ``find_duplicates`` does not exclude the album), and Replace
    disposes of the placed copy into Trash. Under ``move`` the download holds
    only the remainder, so the album is demoted to one track with the notice off
    (security seat M-1); under ``copy``/``hardlink`` it ends whole. Rows alone
    cannot tell the two apart, so both are withheld.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, operation)
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    trash.mkdir()
    armed = _stop_during_placement(monkeypatch, operation, 2)

    assert _import(lib, source, ImportBridge()).errors != []
    (album,) = list(lib.albums())
    both = ["01 Track 1.flac", "02 Track 2.flac"]
    left = sorted(p.name for p in source.iterdir())
    assert left == (["02 Track 2.flac"] if operation == "move" else both)

    outside = _outside(lib, album)
    assert outside is not None
    assert outside.folder == str(source)
    assert outside.holds_every_track is False

    # What the app therefore declines to offer, measured rather than asserted in
    # prose: follow the remedy anyway and Replace trashes the placed track.
    armed["on"] = False
    run = _import(lib, source, ImportBridge(), duplicate=DuplicateAction.replace, trash_dir=trash)
    assert run.errors == []
    assert run.duplicates != [], "the straddle did not reach the duplicate question"
    assert [p.name for p in trash.rglob("*") if p.is_file()] == ["01 Airbag 1.flac"]
    (after,) = list(lib.albums())
    detail = get_album_detail(lib, _require_id(after.id))
    assert detail is not None
    assert detail.outside_library is None, "the notice clears while the album may be short"
    assert len(detail.tracks) == (1 if operation == "move" else 2)


#: beets' file operations, less ``reflink``: the optional ``reflink`` package is
#: not installed here, so that arm cannot be measured (``in_place`` has its own
#: test below).
_MEASURABLE_OPERATIONS = ["move", "copy", "link", "hardlink"]


@pytest.mark.parametrize("operation", _MEASURABLE_OPERATIONS)
def test_the_offered_remedy_finishes_the_album_and_trashes_nothing(
    operation: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where ``holds_every_track`` is true, adding that folder again is safe.

    Started the way the UI's plain "Add from folder" starts it — no options. No
    ``incremental`` override: a stopped run writes no history at all (beets only
    records in ``finalize``, after placement), so there is nothing to get past.

    End state under move, copy, link and hardlink: no duplicate question, one
    whole album, nothing in Trash, the notice off, no row naming a missing file
    — and the download intact under the operations that keep it.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, operation)
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    trash.mkdir()
    armed = _stop_during_placement(monkeypatch, operation, 1)

    assert _import(lib, source, ImportBridge()).errors != []
    (stopped,) = list(lib.albums())
    assert _outside(lib, stopped).holds_every_track is True
    armed["on"] = False

    run = _import(lib, source, ImportBridge(), trash_dir=trash)
    assert run.errors == []
    # beets' own shape: every row names a file this task is importing, so
    # ``find_duplicates`` excludes the album (``B/importer/tasks.py:387-397``)
    # and ``remove_replaced`` (``:618-625``) drops the rows at ``task.add``.
    assert run.duplicates == []
    (album,) = list(lib.albums())
    detail = get_album_detail(lib, _require_id(album.id))
    assert detail is not None
    assert detail.outside_library is None
    assert len(detail.tracks) == 2
    assert [p for p in _item_paths(lib) if not p.exists()] == []
    assert [p for p in trash.rglob("*") if p.is_file()] == []
    if operation != "move":
        assert sorted(p.name for p in source.iterdir()) == ["01 Track 1.flac", "02 Track 2.flac"]


def test_adding_an_in_place_albums_folder_again_files_it_and_loses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``in_place`` shape reads ``holds_every_track`` — and the remedy is harmless.

    An ``in_place`` import files nothing, so every row sits in the source folder.
    Adding that folder again through a plain import files the album into the
    library: one album, both tracks, nothing in Trash, the download still there.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "copy")
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    trash.mkdir()

    assert _import(lib, source, ImportBridge(), in_place=True).errors == []
    (placed,) = list(lib.albums())
    assert _outside(lib, placed) == OutsideLibrary(folder=str(source), holds_every_track=True)

    run = _import(lib, source, ImportBridge(), trash_dir=trash)
    assert run.errors == []
    assert run.duplicates == []
    (album,) = list(lib.albums())
    detail = get_album_detail(lib, _require_id(album.id))
    assert detail is not None
    assert detail.outside_library is None
    assert len(detail.tracks) == 2
    assert [p for p in _item_paths(lib) if not p.exists()] == []
    assert [p for p in trash.rglob("*") if p.is_file()] == []
    assert sorted(p.name for p in source.iterdir()) == ["01 Track 1.flac", "02 Track 2.flac"]


def test_an_abandoned_park_leaves_no_worker_holding_the_config_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The helper's own invariant — breaking it poisons the whole process.

    ``answer_for=0`` is an abandoned park: the worker reaches its first
    decision hook with nobody ever answering, which is what the deadline (or a
    raise from a push) leaves behind. Without the unwind that daemon worker
    sits in ``park`` inside ``_config_force_lock`` for the life of the
    interpreter, and every later import refuses with ``ImportConfigBusyError``.

    The oracles: the helper returns at all (its own ``assert not
    thread.is_alive()``), the lock is free, and a second import in this same
    process still runs — which is the one that would have refused.
    """
    from app.beets.import_session import _CONFIG_FORCE_LOCK

    _install_lookup(monkeypatch, BeetsRec.medium)  # medium parks for review
    lib = _library(tmp_path)
    source = _source_folder(tmp_path)

    abandoned = _import(lib, source, ImportBridge(), answer_for=0.0)
    assert abandoned.errors == []
    assert not _CONFIG_FORCE_LOCK.locked()

    later = _import(lib, source, ImportBridge())
    assert later.errors == [], "a leaked config-force lock would have refused this"
    assert later.parked != []


def test_a_hardlinked_folder_added_again_is_skipped_as_already_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug this slice closes. Run one hardlinks the album into the library
    and leaves the download where it was; run two of the SAME folder must land
    nothing and report it as already known — not meet the album again.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path)
    source = _source_folder(tmp_path)
    sources = sorted(p.name for p in source.iterdir())

    first = ImportBridge()
    assert _import(lib, source, first).errors == []
    assert len(list(lib.albums())) == 1
    assert first.known_skips() == 0
    # The download survived, which is the whole point — and it is the SAME file.
    assert sorted(p.name for p in source.iterdir()) == sources
    assert os.stat(_item_paths(lib)[0]).st_nlink == 2

    second = ImportBridge()
    run = _import(lib, source, second)
    assert run.errors == []
    assert run.duplicates == [], "the folder was tagged again — the history did not refuse it"
    assert len(list(lib.albums())) == 1, "the album was imported a second time"
    assert second.known_skips() == 1
    # Measured here: a history-skipped folder emits no outcome at all, so this
    # count is the run's only report and cannot double-count skipped.
    assert second.drain_outcomes() == []


def test_the_per_run_override_gets_past_the_history_to_the_duplicate_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``incremental: false`` (beets' own ``-I``) is the way past that history.

    What actually happens, measured rather than assumed: the folder is tagged,
    the album is still in the library, so the run reaches MusicDrop's duplicate
    hook and parks the prompt. Nothing has been imported at that point — the
    decision is the user's, and this run answers Skip.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path)
    source = _source_folder(tmp_path)

    assert _import(lib, source, ImportBridge()).errors == []
    assert len(list(lib.albums())) == 1

    bridge = ImportBridge()
    run = _import(lib, source, bridge, incremental=False)
    assert run.errors == []
    assert run.duplicates != [], "the history skipped the folder despite incremental=False"
    assert bridge.known_skips() == 0
    assert run.duplicates[0].existing != []
    assert run.duplicates[0].existing[0].album == _ALBUM
    assert len(list(lib.albums())) == 1  # skipped: still the first import's album


def test_the_per_run_override_re_imports_a_folder_whose_album_left_the_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The story the override exists FOR, end to end.

    A hardlink import records the folder, so a user who then deletes the album
    from the library cannot get it back: the folder is still on disk and every
    later run answers "already known". ``incremental: false`` is the one run
    that imports it again — and the library file is still one inode with the
    download, so the hardlink setup survives the round trip.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path)
    source = _source_folder(tmp_path)

    assert _import(lib, source, ImportBridge()).errors == []
    (album,) = list(lib.albums())
    with lib.music_dir_context():
        album.remove(delete=True)  # the user deletes it; the download stays
    assert list(lib.albums()) == []
    downloads = sorted(source.iterdir())
    assert [p.name for p in downloads] == ["01 Track 1.flac", "02 Track 2.flac"]

    bridge = ImportBridge()
    run = _import(lib, source, bridge, incremental=False)
    assert run.errors == []
    assert bridge.known_skips() == 0
    assert len(list(lib.albums())) == 1, "the folder was refused by its own history"
    assert run.duplicates == []  # the library copy is gone: nothing to duplicate
    landed = sorted(_item_paths(lib))
    assert len(landed) == 2
    for library_file, download in zip(landed, downloads, strict=True):
        assert library_file.exists()
        assert os.stat(library_file).st_nlink == 2
        assert os.path.samefile(library_file, download), "the hardlink was not remade"


def test_a_skipped_album_under_a_hardlink_config_is_offered_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``incremental_skip_later`` is forced ON with the history, so a folder the
    user SKIPPED is not recorded: beets offers it again next time.

    Without it the skip would be recorded as done and the album would be
    unreachable from the Import page — the folder is still on disk, and every
    later run answers "already known".
    """
    _install_lookup(monkeypatch, BeetsRec.medium)  # medium parks for review
    lib = _library(tmp_path)
    source = _source_folder(tmp_path)

    first = _import(lib, source, ImportBridge())
    assert first.errors == []
    assert first.parked != [], "nothing parked to skip"
    assert list(lib.albums()) == []  # skipped: nothing landed

    second_bridge = ImportBridge()
    second = _import(lib, source, second_bridge)
    assert second.errors == []
    assert second.parked != [], "the skipped folder was recorded as done"
    assert second_bridge.known_skips() == 0


@pytest.mark.parametrize("operation", ["hardlink", "copy", "link"])
def test_replacing_a_duplicate_leaves_an_album_whose_files_exist(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-import a folder whose album is still in the library, answer Replace.

    Whatever the file operation, the one album left behind must have files on
    disk. ``hardlink`` and ``link`` did not, until the Replace moved the old copy
    to Trash BEFORE beets placed anything: the download and the library file
    resolve to one file, so beets reuses the old album's paths for the new rows
    (``Item.move_file`` skips ``unique_path`` on samefile) and a Trash move made
    afterwards moved those very files. Both were strict xfails here until
    2026-09-18.

    ``link`` was measured here 2026-09-18, not assumed: ``util.samefile`` is
    ``os.path.samefile``, which follows symlinks, so a symlink import hits the
    same shape as a hardlink. ``copy`` is the control: a distinct file, and
    Replace was always fine.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, operation)
    source = _source_folder(tmp_path)
    trash = tmp_path / "trash"
    trash.mkdir()

    assert _import(lib, source, ImportBridge()).errors == []
    assert len(list(lib.albums())) == 1

    run = _import(
        lib,
        source,
        ImportBridge(),
        incremental=False,
        duplicate=DuplicateAction.replace,
        trash_dir=trash,
    )
    assert run.errors == []
    assert run.duplicates != [], "nothing asked about the duplicate"
    assert len(list(lib.albums())) == 1, "Replace left more than one album"
    missing = [p for p in _item_paths(lib) if not p.exists()]
    assert missing == [], f"{len(missing)} item rows point at files that are gone"
