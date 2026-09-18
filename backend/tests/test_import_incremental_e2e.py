"""Real beets, real files: what a HARDLINK import does to the same folder twice.

A hardlink leaves the download in place, so the folder is still there to be
added again — and beets would happily import the album a second time onto the
same set of files, giving two library rows over one file. beets' own import
history is what refuses that (``importer/session.py:246-256``), it is only
written when ``incremental`` is on (``importer/tasks.py:301-305``), and
``run_import_worker`` is what turns both keys on for a run that keeps the
files.

Nothing here is faked except the MusicBrainz lookup (``tag_album``): the
library is a real ``Library``, the files are real FLACs hardlinked into a real
music dir, and the history is a real pickle under ``tmp_path``. ``statefile``
is repointed per test on purpose — the suite's BEETSDIR is process-wide, so the
default ``state.pickle`` would carry one test's history into the next.
"""

from __future__ import annotations

import os
import shutil
import threading
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
from app.models.import_models import ImportAction, ImportChoice, ParkedAlbum

if TYPE_CHECKING:
    from beets.library import Library

_ALBUM = "OK Computer"
_ARTIST = "Radiohead"


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


def _keep_downloads_config(tmp_path: Path) -> Library:
    """The user's keep-downloads config, plus a real library and a per-test
    history file. Source and library share ``tmp_path`` so a hardlink is
    possible — beets raises on EXDEV rather than falling back."""
    from tests.conftest import build_library

    config["import"]["hardlink"] = True  # the switch this whole slice serves
    config["statefile"] = str(tmp_path / "state.pickle")
    music = tmp_path / "music"
    music.mkdir()
    return build_library(str(tmp_path / "library.db"), str(music))


def _run(
    lib: Library, source: Path, bridge: ImportBridge, **kwargs: Any
) -> tuple[WebImportSession, list[str]]:
    """One import, on this thread (nothing parks at a strong rec)."""
    session = WebImportSession(
        lib,
        None,
        [os.fsencode(str(source))],
        None,
        bridge,
        None,
        unattended=False,
        sweep=False,
        bank_dir=None,
        directive=None,
    )
    errors: list[str] = []
    try:
        run_import_worker(session, **kwargs)
    except Exception as exc:  # reported, never swallowed
        errors.append(f"{exc.__class__.__name__}: {exc}")
    return session, errors


def _run_threaded(
    lib: Library, source: Path, bridge: ImportBridge, **kwargs: Any
) -> tuple[threading.Thread, list[str]]:
    """One import on its own thread, for the runs that block on a decision."""
    errors: list[str] = []

    def worker() -> None:
        _, errs = _run(lib, source, bridge, **kwargs)
        errors.extend(errs)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t, errors


def _skip_one_parked_album(lib: Library, source: Path, bridge: ImportBridge) -> ParkedAlbum | None:
    """Run an import, answer SKIP to whatever it parks, and return the prompt.

    The push is in a ``finally`` and the join is unconditional: a worker left
    blocked in ``park`` holds the process-global config-force lock, and every
    later import in the suite then refuses with ``ImportConfigBusyError``, so
    one failure here would read as a file of unrelated ones.
    """
    thread, errors = _run_threaded(lib, source, bridge)
    parked = bridge.get_parked(timeout=20.0)
    try:
        return parked
    finally:
        if parked is not None:
            bridge.push_choice(parked.album_index, ImportChoice(action=ImportAction.skip))
        thread.join(timeout=20.0)
        assert not thread.is_alive(), "import worker hung"
        assert errors == []


def test_a_hardlinked_folder_added_again_is_skipped_as_already_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug this slice closes. Run one hardlinks the album into the library
    and leaves the download where it was; run two of the SAME folder must land
    nothing and report it as already known — not import a second album row over
    the same inodes.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _keep_downloads_config(tmp_path)
    source = _source_folder(tmp_path)
    sources = sorted(p.name for p in source.iterdir())

    first = ImportBridge()
    _, errors = _run(lib, source, first)
    assert errors == []
    assert len(list(lib.albums())) == 1
    assert first.known_skips() == 0
    # The download survived, which is the whole point — and it is the SAME file.
    assert sorted(p.name for p in source.iterdir()) == sources
    imported = next(iter(lib.items()))
    assert os.stat(os.fsdecode(imported.path)).st_nlink == 2

    # Run two is DEADLINED on a thread, not run inline: if the history stops
    # refusing the folder, the run tags it, finds the library copy and blocks in
    # park() waiting for a duplicate decision nobody will give. Measured — that
    # is what removing the forcing does, and inline it hung the whole suite
    # instead of failing this test.
    from app.models.import_models import DuplicateAction, DuplicateDecision

    second = ImportBridge()
    thread, errors = _run_threaded(lib, source, second)
    thread.join(timeout=20.0)
    prompt = second.get_parked_duplicate(timeout=0)
    try:
        assert prompt is None, "the folder was tagged again — the history did not refuse it"
        assert not thread.is_alive(), "import worker hung"
        assert errors == []
        assert len(list(lib.albums())) == 1, "the album was imported a second time"
        assert second.known_skips() == 1
        # Measured here: a history-skipped folder emits no outcome at all, so
        # this count is the run's only report and cannot double-count skipped.
        assert second.drain_outcomes() == []
    finally:
        if prompt is not None:  # release the worker; it holds the config lock
            second.push_duplicate_decision(
                prompt.album_index, DuplicateDecision(action=DuplicateAction.skip_new)
            )
            thread.join(timeout=20.0)


def test_the_per_run_override_gets_past_the_history_to_the_duplicate_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``incremental: false`` (beets' own ``-I``) is the way past that history.

    What actually happens, measured rather than assumed: the folder is tagged,
    the album is still in the library, so the run reaches MusicDrop's duplicate
    hook and parks the prompt. Nothing has been imported at that point — the
    decision is the user's.
    """
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _keep_downloads_config(tmp_path)
    source = _source_folder(tmp_path)

    _, errors = _run(lib, source, ImportBridge())
    assert errors == []
    assert len(list(lib.albums())) == 1

    from app.models.import_models import DuplicateAction, DuplicateDecision

    bridge = ImportBridge()
    thread, errors = _run_threaded(lib, source, bridge, incremental=False)
    prompt = bridge.get_parked_duplicate(timeout=20.0)
    # Released in a finally, and the assertions read first: a worker left
    # blocked in park holds the process-global config-force lock, and every
    # later import in the suite then refuses with ImportConfigBusyError — one
    # failure here would read as a file of unrelated ones.
    try:
        assert prompt is not None, "the history skipped the folder despite incremental=False"
        assert bridge.known_skips() == 0
        assert prompt.existing != []
        assert prompt.existing[0].album == _ALBUM
    finally:
        if prompt is not None:
            bridge.push_duplicate_decision(
                prompt.album_index, DuplicateDecision(action=DuplicateAction.skip_new)
            )
        thread.join(timeout=20.0)
    assert not thread.is_alive(), "import worker hung"
    assert errors == []
    assert len(list(lib.albums())) == 1  # skipped: still the first import's album


def test_an_album_skipped_under_a_hardlink_config_is_offered_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``incremental_skip_later`` is forced ON with the history, so a folder the
    user SKIPPED is not recorded: beets offers it again next time.

    Without it the skip would be recorded as done and the album would be
    unreachable from the Import page — the folder is still on disk, and every
    later run answers "already known".
    """
    _install_lookup(monkeypatch, BeetsRec.medium)  # medium parks for review
    lib = _keep_downloads_config(tmp_path)
    source = _source_folder(tmp_path)

    first = ImportBridge()
    assert _skip_one_parked_album(lib, source, first) is not None, "nothing parked to skip"
    assert list(lib.albums()) == []  # skipped: nothing landed

    second = ImportBridge()
    parked = _skip_one_parked_album(lib, source, second)
    assert parked is not None, "the skipped folder was recorded as done"
    assert second.known_skips() == 0
