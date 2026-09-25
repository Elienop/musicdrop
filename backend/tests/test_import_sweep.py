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
from beets.autotag import AlbumInfo, AlbumMatch, Source, TrackInfo
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
from app.beets.import_session import ImportBridge, WebImportSession, _SourceFiles
from app.beets.library import _require_id
from app.models.bank import BankApplyDirective, BankItem
from app.models.import_models import Recommendation


def _build_match(rec_level: BeetsRec, album_id: str = "a1") -> AlbumMatch:
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
        album_id=album_id,
        data_source="MusicBrainz",
        data_url=f"https://mb/{album_id}",
        year=1997,
        va=False,
    )
    pairs, extra_items, extra_tracks = assign_items(items, info.tracks)
    return AlbumMatch(
        distance(Source.from_items(items).data, info, pairs, len(extra_items)),
        info,
        dict(pairs),
        extra_items,
        extra_tracks,
    )


def _patch_tag_album(monkeypatch: pytest.MonkeyPatch, match: AlbumMatch, rec: BeetsRec) -> None:
    def fake_tag_album(source: Source, search_ids: Any = None) -> Proposal:
        return Proposal([match], rec)

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
    # __init__ is skipped, so seed the record of what the run is READING; both
    # Replace routes ask it which library rows are the import's own.
    session._source_files = _SourceFiles()
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
    assert row.confidence is not None
    assert row.confidence > 0.0
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
    def fake_tag_album(source: Source, search_ids: Any = None) -> Proposal:
        return Proposal([], BeetsRec.none)

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
    # beets only reaches this hook through _resolve_duplicates, which gates on
    # task.choice_flag (stages.py) — set by set_choice, which is also what puts
    # the matched release on task.match. An APPLY task arriving here therefore
    # ALWAYS carries its match; the choice is part of the fixture, not scenery.
    task.set_choice(match)
    task.md_album_index = 3  # type: ignore[attr-defined]  # the index choose_match stashed

    action = session.get_duplicate_action(task, [existing_album])

    assert action is BeetsDuplicateAction.SKIP  # the library copy is kept
    summaries = store.list_items(bank_dir, offset=0, limit=10)
    assert len(summaries) == 1
    row = store.get_item(bank_dir, summaries[0].id)
    assert row is not None
    assert row.reason == "needs_dup_resolution"
    assert row.duplicate is not None
    assert row.duplicate.incoming.album == "OK Computer"
    assert [e.album_id for e in row.duplicate.existing] == [_require_id(existing_album.id)]
    # "decide once": the sweep had already MATCHED this album when the collision
    # was found, so the matched release is banked as the same ParkedAlbum a
    # needs_review row carries. The apply then REPLAYS it (directive_for pins
    # import.search_ids to options[0] for the dup screen's index-less decision)
    # instead of re-running the lookup and taking whatever ranks first later.
    assert row.parked is not None
    assert row.parked.folder == str(folder)
    assert row.parked.candidate.recommendation is Recommendation.strong
    assert row.parked.candidate.options
    # options[0] IS the release the sweep matched — the id the apply pins.
    assert row.parked.candidate.options[0].release_id == "a1"
    assert match.info.album_id == "a1"  # ...which is this match's release
    # Both payloads describe the same album, so both carry its feed index.
    assert row.parked.album_index == 3
    assert row.duplicate.album_index == 3


def test_sweep_duplicate_payload_leads_with_the_matched_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The apply pin is options[0] (the duplicate screen posts no
    # candidate_index), so the MATCHED release must lead the banked options —
    # beets' set_choice does not move the chosen match to the head of
    # task.candidates, so taking candidates[0] would pin the wrong release
    # whenever the sweep applied anything but the top-ranked one.
    other = _build_match(BeetsRec.strong, album_id="a-other")
    match = _build_match(BeetsRec.strong, album_id="a-matched")

    def fake_tag_album(source: Source, search_ids: Any = None) -> Proposal:
        return Proposal([other, match], BeetsRec.strong)

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)
    bank_dir = tmp_path / "bank"
    session = _sweep_session(ImportBridge(), bank_dir)
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    session.lib = lib
    existing_album = lib.add_album(
        [
            Item(
                albumartist="Radiohead",
                album="OK Computer",
                title="Airbag",
                track=1,
                length=234.0,
                path=os.fsencode(str(tmp_path / "music" / "ok.mp3")),
            )
        ]
    )
    folder = _album_folder(tmp_path)
    task = ImportTask(
        toppath=None, paths=[os.fsencode(str(folder))], items=list(match.mapping.keys())
    )
    task.lookup_candidates([])
    task.set_choice(match)  # the SECOND candidate is the one that was applied
    assert [c.info.album_id for c in task.candidates or []] == ["a-other", "a-matched"]

    session.get_duplicate_action(task, [existing_album])

    summaries = store.list_items(bank_dir, offset=0, limit=10)
    row = store.get_item(bank_dir, summaries[0].id)
    assert row is not None
    assert row.parked is not None
    options = row.parked.candidate.options
    # The match leads; the rest follow, none dropped and none duplicated.
    assert [o.release_id for o in options] == ["a-matched", "a-other"]
    assert row.parked.candidate.album_after.album == "OK Computer"


def test_sweep_matchless_duplicate_banks_no_parked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # beets shares this hook with ASIS/RETAG tasks (_resolve_duplicates fires
    # for choice_flag in ASIS/APPLY/RETAG) and set_choice leaves task.match None
    # for those: NOTHING was matched, so there is no release to pin. The row
    # banks honestly unpinned rather than guessing task.candidates[0] — which is
    # still populated here, and would pin a release the sweep never chose.
    match = _build_match(BeetsRec.strong)
    bank_dir = tmp_path / "bank"
    session = _sweep_session(ImportBridge(), bank_dir)
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    session.lib = lib
    existing_album = lib.add_album(
        [
            Item(
                albumartist="Radiohead",
                album="OK Computer",
                title="Airbag",
                track=1,
                length=234.0,
                path=os.fsencode(str(tmp_path / "music" / "ok.mp3")),
            )
        ]
    )
    folder = _album_folder(tmp_path)
    task = _make_task(match, monkeypatch, BeetsRec.strong, paths=[os.fsencode(str(folder))])
    task.set_choice(Action.ASIS)
    assert task.candidates  # the lookup ran; only the CHOICE is absent
    assert task.match is None

    action = session.get_duplicate_action(task, [existing_album])

    assert action is BeetsDuplicateAction.SKIP
    summaries = store.list_items(bank_dir, offset=0, limit=10)
    assert len(summaries) == 1
    row = store.get_item(bank_dir, summaries[0].id)
    assert row is not None
    assert row.reason == "needs_dup_resolution"
    assert row.duplicate is not None  # the prompt still banks
    assert row.parked is None  # nothing matched -> nothing to pin
    assert row.confidence == 0.0


def test_matched_release_payload_is_faithful_field_by_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every banked-payload field, pinned directly on the helper.

    These fields are WRITE-ONLY today (no bank surface renders a dup row's
    parked payload — the dup screen shows the prompt, and a rescan replaces the
    payload wholesale before the candidate screen ever reads it), so a wrong
    value ships silently the day anything consumes them. Two mutations proved
    the gap: has_current_art hardcoded False and cur_artist/cur_album
    transposed both survived the full suite before this pin."""
    match = _build_match(BeetsRec.strong, album_id="mb-pin")
    session = _sweep_session(ImportBridge(), tmp_path / "bank")
    folder = _album_folder(tmp_path)
    task = _make_task(match, monkeypatch, BeetsRec.strong, paths=[os.fsencode(str(folder))])
    # The real path reaches the duplicate hook only after choose_match returned
    # the match and beets recorded it — set_choice is what populates task.match.
    task.set_choice(match)

    payload = session._matched_release_payload(
        task, index=3, recommendation=Recommendation.strong, has_current_art=True
    )

    assert payload is not None
    assert payload.album_index == 3
    assert payload.folder == str(folder)
    candidate = payload.candidate
    assert candidate.has_current_art is True
    # album_before carries the CURRENT tags (cur_artist/cur_album from the
    # lookup) — transposing them is the survived mutation this line kills.
    assert candidate.album_before.artist == "Radiohead"
    assert candidate.album_before.album == "OK Computer"
    assert candidate.recommendation is Recommendation.strong
    assert candidate.options[0].release_id == "mb-pin"


def test_sweep_without_folder_banks_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A pathless task has no folder identity the apply runner could ever
    # re-import: emit-and-skip only, no row, no crash.
    def fake_tag_album(source: Source, search_ids: Any = None) -> Proposal:
        return Proposal([], BeetsRec.none)

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


def _inbox_session(bridge: ImportBridge, bank_dir: Path) -> WebImportSession:
    """slskd's drain: unattended with a bank, and NOT a sweep."""
    session = _sweep_session(bridge, bank_dir)
    session.sweep = False
    return session


def _no_match_task(monkeypatch: pytest.MonkeyPatch, folder: Path) -> ImportTask:
    def fake_tag_album(source: Source, search_ids: Any = None) -> Proposal:
        return Proposal([], BeetsRec.none)

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)
    task = ImportTask(
        toppath=None,
        paths=[os.fsencode(str(folder))],
        items=[Item(artist="Artist", album="Album", title="X", track=1, length=10.0)],
    )
    task.lookup_candidates([])
    return task


def _only_row(bank_dir: Path) -> BankItem:
    summaries = store.list_items(bank_dir, offset=0, limit=10)
    assert len(summaries) == 1
    row = store.get_item(bank_dir, summaries[0].id)
    assert row is not None
    return row


def test_an_unattended_session_with_a_bank_banks_an_unsure_match_as_inbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # slskd's drain banks what it skips exactly as a sweep does (decisions #76):
    # banking follows "unattended with a bank", never ``sweep``.
    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    bank_dir = tmp_path / "bank"
    session = _inbox_session(bridge, bank_dir)
    folder = _album_folder(tmp_path)
    task = _make_task(match, monkeypatch, BeetsRec.medium, paths=[os.fsencode(str(folder))])

    assert session.choose_match(task) is Action.SKIP
    assert bridge.pending_count() == 0  # banked, never parked
    row = _only_row(bank_dir)
    assert (row.source, row.reason, row.status) == ("inbox", "needs_review", "needs_review")
    assert row.parked is not None
    assert row.parked.folder == str(folder)


def test_an_unattended_session_with_a_bank_banks_no_match_as_inbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bank_dir = tmp_path / "bank"
    session = _inbox_session(ImportBridge(), bank_dir)
    folder = _album_folder(tmp_path)

    assert session.choose_match(_no_match_task(monkeypatch, folder)) is Action.SKIP
    row = _only_row(bank_dir)
    assert (row.source, row.reason, row.status) == ("inbox", "no_match", "needs_review")
    assert row.folder == str(folder)


def test_an_unattended_session_with_a_bank_banks_a_duplicate_as_inbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    match = _build_match(BeetsRec.strong)
    bank_dir = tmp_path / "bank"
    session = _inbox_session(ImportBridge(), bank_dir)
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    session.lib = lib
    existing_album = lib.add_album(
        [
            Item(
                albumartist="Radiohead",
                album="OK Computer",
                title="Airbag",
                track=1,
                length=234.0,
                path=os.fsencode(str(tmp_path / "music" / "ok.mp3")),
            )
        ]
    )
    folder = _album_folder(tmp_path)
    task = _make_task(match, monkeypatch, BeetsRec.strong, paths=[os.fsencode(str(folder))])
    task.set_choice(match)
    task.md_album_index = 0  # type: ignore[attr-defined]  # the index choose_match stashed

    action = session.get_duplicate_action(task, [existing_album])

    assert action is BeetsDuplicateAction.SKIP  # the library copy is kept
    row = _only_row(bank_dir)
    assert (row.source, row.reason, row.status) == ("inbox", "needs_dup_resolution", "needs_review")
    assert row.duplicate is not None


def test_an_attended_session_with_a_bank_banks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No match is the one banking gate an attended run reaches (the other two
    # park), so it is the one that proves "attended never banks".
    bank_dir = tmp_path / "bank"
    session = _inbox_session(ImportBridge(), bank_dir)
    session.unattended = False
    folder = _album_folder(tmp_path)

    assert session.choose_match(_no_match_task(monkeypatch, folder)) is Action.SKIP
    assert store.count_items(bank_dir) == 0


def test_a_bank_apply_with_a_bank_banks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The apply run is unattended and, since every run is handed the bank, has
    # one; it answers from its own row, so a no-match there is never re-banked.
    bank_dir = tmp_path / "bank"
    session = _inbox_session(ImportBridge(), bank_dir)
    session._directive = BankApplyDirective(action="apply")
    folder = _album_folder(tmp_path)

    assert session.choose_match(_no_match_task(monkeypatch, folder)) is Action.SKIP
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
