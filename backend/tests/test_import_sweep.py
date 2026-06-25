"""Sweep banking emission (chunk 3 of import banking).

The unattended sweep session must persist a bank row (the SAME payload the
attended park would push) before SKIPping each set-aside album. Hermetic: the
candidate lookup is stubbed at the proven ``beets_tasks.tag_album`` seam, the
bank dir is a tmp_path, and the duplicate test uses a real (empty-file) beets
Library only for the ``found_duplicates`` album row.
"""

import logging
import os
from pathlib import Path
from typing import Any

import beets.importer.tasks as beets_tasks
import pytest
from beets.autotag import AlbumInfo, AlbumMatch, TrackInfo
from beets.autotag.distance import distance
from beets.autotag.match import Proposal, assign_items
from beets.autotag.match import Recommendation as BeetsRec
from beets.importer.actions import Action
from beets.importer.actions import DuplicateAction as BeetsDuplicateAction
from beets.importer.tasks import ImportTask
from beets.library import Item, Library

import app.beets.import_session as session_mod
from app.bank import store
from app.bank.fingerprint import folder_fingerprint
from app.beets.import_session import ImportBridge, WebImportSession
from app.models.import_models import Recommendation


def _build_match(rec_level: BeetsRec) -> AlbumMatch:
    if rec_level == BeetsRec.strong:
        items = [
            Item(artist="Radiohead", album="OK Computer", title="Airbag", track=1, length=234.0)
        ]
        album = "OK Computer"
    else:
        items = [Item(artist="Radiohead", album="Different", title="Airbag", track=1, length=234.0)]
        album = "OK Computer"
    tracks = [TrackInfo(title="Airbag", track_id="t1", index=1, length=234.0)]
    info = AlbumInfo(
        tracks=tracks,
        album=album,
        artist="Radiohead",
        album_id="a1",
        data_source="MusicBrainz",
        data_url="https://mb/a1",
        year=1997,
        va=False,
    )
    pairs, extra_items, extra_tracks = assign_items(items, info.tracks)
    return AlbumMatch(distance(items, info, pairs), info, dict(pairs), extra_items, extra_tracks)


def _patch_tag_album(monkeypatch: pytest.MonkeyPatch, match: AlbumMatch, rec: BeetsRec) -> None:
    def fake_tag_album(items: Any, search_ids: Any = None) -> tuple[str, str, Proposal]:
        return ("Radiohead", "OK Computer", Proposal([match], rec))

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)


def _make_task(
    match: AlbumMatch, monkeypatch: pytest.MonkeyPatch, rec: BeetsRec, paths: list[bytes]
) -> ImportTask:
    _patch_tag_album(monkeypatch, match, rec)
    task = ImportTask(toppath=None, paths=paths, items=list(match.mapping.keys()))
    task.lookup_candidates([])
    return task


def _sweep_session(bridge: ImportBridge, bank_dir: Path) -> WebImportSession:
    """A sweep-mode session without a Library (run() is never called)."""
    session = WebImportSession.__new__(WebImportSession)
    session.logger = logging.getLogger("test.sweep")
    session.bridge = bridge
    session._album_index = 0
    session.unattended = True
    session.sweep = True
    session._bank_dir = bank_dir
    # __init__ is skipped, so default the apply directive the hooks now read.
    session._directive = None
    session._await_album_id = []
    session.paths = []
    return session


def _album_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "incoming" / "Radiohead - OK Computer"
    folder.mkdir(parents=True)
    (folder / "01 Airbag.mp3").write_bytes(b"x" * 64)
    return folder


def test_sweep_banks_uncertain_match_then_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    bank_dir = tmp_path / "bank"
    session = _sweep_session(bridge, bank_dir)
    folder = _album_folder(tmp_path)
    task = _make_task(match, monkeypatch, BeetsRec.medium, paths=[os.fsencode(str(folder))])

    result = session.choose_match(task)

    assert result is Action.SKIP
    assert bridge.pending_count() == 0  # banked, never parked
    summaries = store.list_items(bank_dir, offset=0, limit=10)
    assert len(summaries) == 1
    row = store.get_item(bank_dir, summaries[0].id)
    assert row is not None
    assert row.source == "sweep"
    assert row.reason == "needs_review"
    assert row.status == "needs_review"
    assert row.folder == str(folder)
    assert row.fingerprint == folder_fingerprint(folder)
    assert row.artist == "Radiohead"
    assert row.recommendation == "medium"
    assert row.confidence is not None and row.confidence > 0.0
    # The banked payload IS the live review screen's payload.
    assert row.parked is not None
    assert row.parked.folder == str(folder)
    assert row.parked.candidate.recommendation is Recommendation.medium
    assert row.parked.candidate.options  # ranked alternatives serialized too
    assert row.duplicate is None


def test_sweep_rebank_same_folder_dedupes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    match = _build_match(BeetsRec.medium)
    bank_dir = tmp_path / "bank"
    session = _sweep_session(ImportBridge(), bank_dir)
    folder = _album_folder(tmp_path)
    task = _make_task(match, monkeypatch, BeetsRec.medium, paths=[os.fsencode(str(folder))])
    session.choose_match(task)
    task2 = _make_task(match, monkeypatch, BeetsRec.medium, paths=[os.fsencode(str(folder))])
    session.choose_match(task2)
    # upsert_by_folder refreshed the row instead of writing a second one.
    assert store.count_items(bank_dir) == 1


def test_sweep_banks_no_match_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_tag_album(items: Any, search_ids: Any = None) -> tuple[str, str, Proposal]:
        return ("Artist", "Album", Proposal([], BeetsRec.none))

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)
    bank_dir = tmp_path / "bank"
    session = _sweep_session(ImportBridge(), bank_dir)
    folder = _album_folder(tmp_path)
    task = ImportTask(
        toppath=None,
        paths=[os.fsencode(str(folder))],
        items=[Item(artist="Artist", album="Album", title="X", track=1, length=10.0)],
    )
    task.lookup_candidates([])

    result = session.choose_match(task)

    assert result is Action.SKIP
    summaries = store.list_items(bank_dir, offset=0, limit=10)
    assert len(summaries) == 1
    row = store.get_item(bank_dir, summaries[0].id)
    assert row is not None
    assert row.reason == "no_match"
    assert row.parked is None  # zero candidates: decisions are asis/tracks/ignore
    assert row.confidence == 0.0


def test_sweep_banks_duplicate_prompt_then_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    match = _build_match(BeetsRec.strong)
    bank_dir = tmp_path / "bank"
    session = _sweep_session(ImportBridge(), bank_dir)
    # A real (DB-only) library supplies the found_duplicates album row.
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    session.lib = lib
    dup_item = Item(
        albumartist="Radiohead",
        album="OK Computer",
        title="Airbag",
        track=1,
        length=234.0,
        path=os.fsencode(str(tmp_path / "music" / "ok.mp3")),
    )
    existing_album = lib.add_album([dup_item])
    folder = _album_folder(tmp_path)
    task = _make_task(match, monkeypatch, BeetsRec.strong, paths=[os.fsencode(str(folder))])

    action = session.get_duplicate_action(task, [existing_album])

    assert action is BeetsDuplicateAction.SKIP  # the library copy is kept
    summaries = store.list_items(bank_dir, offset=0, limit=10)
    assert len(summaries) == 1
    row = store.get_item(bank_dir, summaries[0].id)
    assert row is not None
    assert row.reason == "needs_dup_resolution"
    assert row.parked is None
    assert row.duplicate is not None
    assert row.duplicate.incoming.album == "OK Computer"
    assert [e.album_id for e in row.duplicate.existing] == [int(existing_album.id)]


def test_sweep_without_folder_banks_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A pathless task has no folder identity the apply runner could ever
    # re-import: emit-and-skip only, no row, no crash.
    def fake_tag_album(items: Any, search_ids: Any = None) -> tuple[str, str, Proposal]:
        return ("Artist", "Album", Proposal([], BeetsRec.none))

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)
    bank_dir = tmp_path / "bank"
    session = _sweep_session(ImportBridge(), bank_dir)
    task = ImportTask(
        toppath=None,
        paths=None,
        items=[Item(artist="Artist", album="Album", title="X", track=1, length=10.0)],
    )
    task.lookup_candidates([])
    assert session.choose_match(task) is Action.SKIP
    assert store.count_items(bank_dir) == 0


def test_non_sweep_unattended_session_never_banks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The inbox path (unattended=True, sweep=False) keeps chunk-1 behavior:
    # set aside via outcome + SKIP, NO bank row.
    match = _build_match(BeetsRec.medium)
    bank_dir = tmp_path / "bank"
    session = _sweep_session(ImportBridge(), bank_dir)
    session.sweep = False
    folder = _album_folder(tmp_path)
    task = _make_task(match, monkeypatch, BeetsRec.medium, paths=[os.fsencode(str(folder))])
    assert session.choose_match(task) is Action.SKIP
    assert store.count_items(bank_dir) == 0


def test_sweep_bank_write_failure_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A sweep that cannot persist its bank must fail the run loudly (the
    # worker turns it into a failed job), never sweep on losing rows silently.
    match = _build_match(BeetsRec.medium)
    session = _sweep_session(ImportBridge(), tmp_path / "bank")
    folder = _album_folder(tmp_path)
    task = _make_task(match, monkeypatch, BeetsRec.medium, paths=[os.fsencode(str(folder))])

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("bank dir unwritable")

    target = session_mod.bank_store  # type: ignore[attr-defined]  # module alias, not a strict re-export
    monkeypatch.setattr(target, "upsert_by_folder", boom)
    with pytest.raises(RuntimeError, match="bank dir unwritable"):
        session.choose_match(task)
