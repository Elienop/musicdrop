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
    the library.
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


#: Why the same-file operations fail the last assertion below, until the Replace
#: reorder lands. Shared by the two params that hit it.
_SAMEFILE_REASON = (
    "KNOWN: the download and the library file resolve to one file, so beets"
    " reuses the OLD album's paths for the new rows (Item.move_file skips"
    " unique_path on samefile) and MusicDrop's post-run pass then trashes those"
    " very files. Fixed by the Replace reorder (Trash move first, then beets)."
)


@pytest.mark.parametrize("operation", ["hardlink", "copy", "link"])
def test_replacing_a_duplicate_leaves_an_album_whose_files_exist(
    operation: str,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-import a folder whose album is still in the library, answer Replace.

    Whatever the file operation, the one album left behind must have files on
    disk. Two arms do not: they report success, and every item row of the
    surviving album names a path that is now in Trash.

    ``link`` was measured here 2026-09-18, not assumed: ``util.samefile`` is
    ``os.path.samefile``, which follows symlinks, so a symlink import hits the
    same shape as a hardlink — same assertion, same reused paths (no
    ``*.1.flac``). ``copy`` is the control: a distinct file, filed at
    ``*.1.flac``, and Replace is fine.
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
    # Scoped to the ONE assertion below, not to the whole test: on the param the
    # mark covers every assertion above too, so a regression in any of them
    # would report XFAIL for these two operations and stay green. Applied here
    # it is still strict — the Replace reorder turns this red until the mark
    # goes.
    if operation in ("hardlink", "link"):
        request.applymarker(pytest.mark.xfail(strict=True, reason=_SAMEFILE_REASON))
    missing = [p for p in _item_paths(lib) if not p.exists()]
    assert missing == [], f"{len(missing)} item rows point at files that are gone"
