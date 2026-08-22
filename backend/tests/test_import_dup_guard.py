"""Import-gate guard: a typographic variant engages the SAME dup machinery.

beets' ``AlbumImportTask.find_duplicates`` is a byte-exact SQL match on
albumartist+album, so a typographic twin (en dash vs hyphen, a case variant)
sails through and silently mints a sibling one-track album. The guard wraps
``find_duplicates`` per-task at the top of ``choose_match`` with beets' exact
pass unchanged plus a normalized-variant scan, so the SAME park prompt / sweep
bank / unattended SKIP / resolution actions the exact case uses now engage for
the variant.

These tests are hermetic: a real (DB-only) beets Library supplies the in-library
dup row, a real ``ImportTask`` drives ``find_duplicates`` through the installed
guard, and the session hooks (``get_duplicate_action``) are asserted on the
variant-matched album exactly as on an exact one (test_import_sweep's duplicate
test is the exact-side net for the same hooks).
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

import beets.importer.tasks as beets_tasks
import pytest
from beets import config
from beets.autotag import AlbumInfo, AlbumMatch, TrackInfo
from beets.autotag.distance import distance
from beets.autotag.match import Proposal, assign_items
from beets.autotag.match import Recommendation as BeetsRec
from beets.importer.actions import Action
from beets.importer.actions import DuplicateAction as BeetsDuplicateAction
from beets.importer.tasks import ImportTask
from beets.library import Item, Library

from app.bank import store as bank_store
from app.beets.import_session import ImportBridge, WebImportSession
from app.beets.library import _require_id
from app.models.bank import BankApplyDirective
from app.models.import_models import (
    AlbumOutcomeStatus,
    DuplicateAction,
    DuplicateDecision,
)

_EN_DASH = "\u2013"  # U+2013 EN DASH


@pytest.fixture(autouse=True)
def _force_serial() -> Any:
    config["threaded"] = False
    yield
    config["threaded"] = False


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def _match(album: str, *, artist: str = "Radiohead", album_id: str = "a9") -> AlbumMatch:
    """A canned APPLY match whose AlbumInfo carries the given variant title."""
    items = [Item(artist=artist, album=album, title="Chapter One", track=1, length=200.0)]
    tracks = [TrackInfo(title="Chapter One", track_id="t1", index=1, length=200.0)]
    info = AlbumInfo(
        tracks=tracks,
        album=album,
        artist=artist,
        album_id=album_id,
        data_source="MusicBrainz",
        data_url=f"https://musicbrainz.org/release/{album_id}",
        year=2011,
        va=False,
    )
    pairs, extra_items, extra_tracks = assign_items(items, info.tracks)
    return AlbumMatch(distance(items, info, pairs), info, dict(pairs), extra_items, extra_tracks)


def _apply_task(match: AlbumMatch, monkeypatch: pytest.MonkeyPatch) -> ImportTask:
    """An APPLY task whose matched release carries the variant title."""

    def fake_tag_album(
        items: Any, search_ids: Any = None
    ) -> tuple[str | None, str | None, Proposal]:
        return (match.info.artist, match.info.album, Proposal([match], BeetsRec.strong))

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)
    task = ImportTask(toppath=None, paths=[b"/incoming/album"], items=list(match.mapping.keys()))
    task.lookup_candidates([])
    task.set_choice(match)  # APPLY: the chosen release carries the variant title
    return task


def _asis_task(album: str, artist: str | None, monkeypatch: pytest.MonkeyPatch) -> ImportTask:
    """An ASIS task whose FILE TAGS carry the given album/artist (None = absent)."""

    def fake_tag_album(
        items: Any, search_ids: Any = None
    ) -> tuple[str | None, str | None, Proposal]:
        return (artist, album, Proposal([], BeetsRec.none))

    tags: dict[str, Any] = {"album": album, "title": "Chapter One", "track": 1, "length": 200.0}
    if artist is not None:
        tags["artist"] = artist
        tags["albumartist"] = artist
    item = Item(**tags)
    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)
    task = ImportTask(toppath=None, paths=[b"/incoming/album"], items=[item])
    task.lookup_candidates([])
    task.set_choice(Action.ASIS)
    return task


def _lib_album_in(artist: str, album: str, tmp_path: Path, *, path: str | None = None) -> Library:
    """A DB-only library holding ONE album — the in-library side of the twin."""
    if path is None:
        path = str(tmp_path / "music" / f"{artist} - {album}" / "01.mp3")
    item = Item(
        artist=artist,
        albumartist=artist,
        album=album,
        title="Chapter One",
        track=1,
        length=200.0,
        path=os.fsencode(path),
    )
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    lib.add_album([item])
    return lib


def _gate_session(
    bridge: ImportBridge,
    lib: Library,
    *,
    unattended: bool = False,
    sweep: bool = False,
    bank_dir: Path | None = None,
    directive: BankApplyDirective | None = None,
    toppaths: list[bytes] | None = None,
) -> WebImportSession:
    """A hook session (run() never called) wired for the duplicate path."""
    session = WebImportSession.__new__(WebImportSession)
    session.logger = logging.getLogger("test.dupguard")
    session.bridge = bridge
    session._album_index = 0
    session._trash_dir = None
    session._replace_album_ids = set()
    session.lib = lib
    session.unattended = unattended
    session.sweep = sweep
    session._bank_dir = bank_dir
    session._directive = directive
    # toppaths _task_folder scopes by (beets sets these in ImportSession.__init__).
    session.paths = toppaths if toppaths is not None else [b"/incoming"]
    return session


def _run_hook(
    session: WebImportSession, task: ImportTask, dups: Any
) -> tuple[threading.Thread, dict[str, Any]]:
    """Drive the (blocking) get_duplicate_action on a worker thread."""
    result: dict[str, Any] = {}

    def target() -> None:
        result["action"] = session.get_duplicate_action(task, dups)

    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, result


# --------------------------------------------------------------------------
# detection: the guard finds the variant where beets' byte-exact match misses
# --------------------------------------------------------------------------


def test_variant_gate_detects_en_dash_on_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Library holds the hyphen title; the matched release is the en-dash twin.
    lib = _lib_album_in("Radiohead", "Greatest Hits - Chapter One", tmp_path)
    task = _apply_task(_match("Greatest Hits " + _EN_DASH + " Chapter One"), monkeypatch)
    session = _gate_session(ImportBridge(), lib)
    session._install_dup_guard(task)
    assert [a.album for a in task.find_duplicates(lib)] == ["Greatest Hits - Chapter One"]


def test_variant_gate_detects_en_dash_on_asis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ASIS: the incoming FILE TAGS carry the en-dash twin of the library hyphen.
    lib = _lib_album_in("Radiohead", "Greatest Hits - Chapter One", tmp_path)
    task = _asis_task("Greatest Hits " + _EN_DASH + " Chapter One", "Radiohead", monkeypatch)
    session = _gate_session(ImportBridge(), lib)
    session._install_dup_guard(task)
    assert [a.album for a in task.find_duplicates(lib)] == ["Greatest Hits - Chapter One"]


def test_variant_gate_detects_case_variant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lib = _lib_album_in("Radiohead", "greatest hits - chapter one", tmp_path)
    task = _apply_task(_match("GREATEST HITS - CHAPTER ONE"), monkeypatch)
    session = _gate_session(ImportBridge(), lib)
    session._install_dup_guard(task)
    assert [a.album for a in task.find_duplicates(lib)] == ["greatest hits - chapter one"]


def test_variant_gate_still_detects_byte_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The byte-exact path must keep working unchanged (the exact tests are the
    # regression net); the guard never removes an exact hit.
    lib = _lib_album_in("Radiohead", "In Rainbows", tmp_path)
    task = _apply_task(_match("In Rainbows"), monkeypatch)
    session = _gate_session(ImportBridge(), lib)
    session._install_dup_guard(task)
    assert [a.album for a in task.find_duplicates(lib)] == ["In Rainbows"]


def test_variant_gate_does_not_flag_a_different_album(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No false positive: a genuinely different album by the same artist is NOT a
    # duplicate, even though both normalize cleanly.
    lib = _lib_album_in("Radiohead", "Kid A", tmp_path)
    task = _apply_task(_match("Hail to the Thief"), monkeypatch)
    session = _gate_session(ImportBridge(), lib)
    session._install_dup_guard(task)
    assert task.find_duplicates(lib) == []


def test_variant_gate_symbol_only_title_matches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A title that folds differently (symbol-only, kept by the rung-3 fallback)
    # must NOT collapse to "" and match every other symbol-only album.
    lib = _lib_album_in("Radiohead", "+++", tmp_path)
    task = _apply_task(_match("==="), monkeypatch)
    session = _gate_session(ImportBridge(), lib)
    session._install_dup_guard(task)
    assert task.find_duplicates(lib) == []


def test_variant_gate_reimport_is_not_a_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # beets' re-import exclusion must hold for the variant path too: an
    # existing album whose files are all in the task is being re-imported, not
    # duplicated, even when its title is the en-dash twin of the incoming one.
    shared = str(tmp_path / "music" / "Radiohead" / "01.mp3")
    variant = "Greatest Hits " + _EN_DASH + " Chapter One"
    lib = _lib_album_in("Radiohead", variant, tmp_path, path=shared)

    def fake_tag_album(items: Any, search_ids: Any = None) -> tuple[str, str, Proposal]:
        return ("Radiohead", variant, Proposal([], BeetsRec.none))

    in_item = Item(
        artist="Radiohead",
        albumartist="Radiohead",
        album=variant,
        title="Chapter One",
        track=1,
        length=200.0,
        path=os.fsencode(shared),
    )
    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)
    task = ImportTask(toppath=None, paths=[b"/incoming/album"], items=[in_item])
    task.lookup_candidates([])
    task.set_choice(Action.ASIS)
    session = _gate_session(ImportBridge(), lib)
    session._install_dup_guard(task)
    assert task.find_duplicates(lib) == []


def test_variant_gate_no_artist_asis_returns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # beets bakes in the as-is/no-artist skip; the variant path mirrors it.
    lib = _lib_album_in("Radiohead", "In Rainbows", tmp_path)
    task = _asis_task("In Rainbows", None, monkeypatch)
    session = _gate_session(ImportBridge(), lib)
    session._install_dup_guard(task)
    assert task.find_duplicates(lib) == []


# --------------------------------------------------------------------------
# wiring: the guard is installed through choose_match — the real production entry
# --------------------------------------------------------------------------


def test_choose_match_arms_the_guard_on_strong_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wiring net: choose_match itself must install the per-task guard.

    Every other test arms the guard by calling ``_install_dup_guard`` directly;
    this one goes through the REAL production entry point (strong auto-apply —
    choose_match returns without parking/blocking). If the
    ``self._install_dup_guard(task)`` line in choose_match is deleted, this FAILS:
    ``task.find_duplicates`` stays beets' pristine byte-exact bound method, the
    wrapper assertion trips first, and ``find_duplicates`` returns [] on the
    en-dash twin instead of the library's hyphen album.
    """
    lib = _lib_album_in("Radiohead", "Greatest Hits - Chapter One", tmp_path)
    task = _apply_task(_match("Greatest Hits " + _EN_DASH + " Chapter One"), monkeypatch)
    bridge = ImportBridge()
    session = _gate_session(bridge, lib, unattended=True)
    # __new__-built fixture (run()/choose_match paths beyond the guard need init state).
    session._await_album_id = []
    session._astracks_in_flight = False

    # Strong auto-apply path: choose_match RETURNS (no park, no blocking worker).
    choice = session.choose_match(task)
    assert choice is not None

    # The guard wrapper is installed on the task (mypy: dynamic shadow attr).
    assert task.find_duplicates.__name__ == "guarded"
    # The downstream (beets' _resolve_duplicates) sees the variant twin through
    # the entry-point-installed guard, not beets' byte-exact-only match.
    assert [a.album for a in task.find_duplicates(lib)] == ["Greatest Hits - Chapter One"]


# --------------------------------------------------------------------------
# machinery: the variant engages the SAME downstream the exact case already uses
# --------------------------------------------------------------------------


def test_variant_gate_attended_parks_and_emits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = _lib_album_in("Radiohead", "Greatest Hits - Chapter One", tmp_path)
    task = _apply_task(_match("Greatest Hits " + _EN_DASH + " Chapter One"), monkeypatch)
    bridge = ImportBridge()
    session = _gate_session(bridge, lib)
    session._install_dup_guard(task)
    found = task.find_duplicates(lib)
    assert found, "the gate must engage on the en-dash variant"
    task.md_album_index = 7  # type: ignore[attr-defined]  # the index choose_match would stash

    t, result = _run_hook(session, task, found)
    prompt = bridge.get_parked_duplicate(timeout=2.0)
    assert prompt is not None
    assert prompt.album_index == 7
    assert prompt.incoming.album == "Greatest Hits \u2013 Chapter One"
    assert prompt.existing[0].album == "Greatest Hits - Chapter One"
    assert any(o.status is AlbumOutcomeStatus.needs_dup_resolution for o in bridge.drain_outcomes())

    bridge.push_duplicate_decision(7, DuplicateDecision(action=DuplicateAction.keep_both))
    t.join(timeout=2.0)
    assert result["action"] is BeetsDuplicateAction.KEEP  # keep_both → import alongside


def test_variant_gate_unattended_skips_with_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = _lib_album_in("Radiohead", "Greatest Hits - Chapter One", tmp_path)
    task = _apply_task(_match("Greatest Hits " + _EN_DASH + " Chapter One"), monkeypatch)
    bridge = ImportBridge()
    session = _gate_session(bridge, lib, unattended=True)
    session._install_dup_guard(task)
    found = task.find_duplicates(lib)
    assert found
    task.md_album_index = 0  # type: ignore[attr-defined]

    action = session.get_duplicate_action(task, found)

    assert action is BeetsDuplicateAction.SKIP  # library copy kept, new one set aside
    assert bridge.pending_count() == 0  # never parked/blocked
    assert any(o.status is AlbumOutcomeStatus.needs_dup_resolution for o in bridge.drain_outcomes())


def test_variant_gate_sweep_banks_needs_dup_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = _lib_album_in("Radiohead", "Greatest Hits - Chapter One", tmp_path)
    # Real source folder so _task_folder + folder_fingerprint have a real path.
    top = tmp_path / "incoming"
    folder = top / "Greatest Hits - Chapter One"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "01 Chapter One.mp3").write_bytes(b"x" * 64)
    match = _match("Greatest Hits " + _EN_DASH + " Chapter One")

    def fake_tag_album(
        items: Any, search_ids: Any = None
    ) -> tuple[str | None, str | None, Proposal]:
        return (match.info.artist, match.info.album, Proposal([match], BeetsRec.strong))

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)
    task = ImportTask(
        toppath=None, paths=[os.fsencode(str(folder))], items=list(match.mapping.keys())
    )
    task.lookup_candidates([])
    task.set_choice(match)
    bank_dir = tmp_path / "bank"
    bridge = ImportBridge()
    session = _gate_session(
        bridge,
        lib,
        unattended=True,
        sweep=True,
        bank_dir=bank_dir,
        toppaths=[os.fsencode(str(top))],
    )
    session._install_dup_guard(task)
    found = task.find_duplicates(lib)
    assert found
    task.md_album_index = 0  # type: ignore[attr-defined]

    action = session.get_duplicate_action(task, found)

    assert action is BeetsDuplicateAction.SKIP
    assert bridge.pending_count() == 0
    summaries = bank_store.list_items(bank_dir, offset=0, limit=10)
    assert len(summaries) == 1
    row = bank_store.get_item(bank_dir, summaries[0].id)
    assert row is not None
    assert row.reason == "needs_dup_resolution"
    assert row.duplicate is not None
    assert [e.album for e in row.duplicate.existing] == ["Greatest Hits - Chapter One"]


def test_variant_gate_directive_without_decision_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A banked apply row that turns out to be a variant duplicate: without an
    # explicit dup decision this may NOT auto-pick a resolution — SKIP.
    lib = _lib_album_in("Radiohead", "Greatest Hits - Chapter One", tmp_path)
    task = _apply_task(_match("Greatest Hits " + _EN_DASH + " Chapter One"), monkeypatch)
    bridge = ImportBridge()
    session = _gate_session(
        bridge, lib, directive=BankApplyDirective(action="apply", duplicate_action=None)
    )
    session._install_dup_guard(task)
    found = task.find_duplicates(lib)
    assert found
    task.md_album_index = 0  # type: ignore[attr-defined]

    action = session.get_duplicate_action(task, found)

    assert action is BeetsDuplicateAction.SKIP
    assert bridge.pending_count() == 0


def test_variant_gate_directive_replace_trashes_the_twin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With an explicit dup decision, the resolution machinery works on the
    # variant twin exactly as on an exact one (replace records the twin's id).
    lib = _lib_album_in("Radiohead", "Greatest Hits - Chapter One", tmp_path)
    task = _apply_task(_match("Greatest Hits " + _EN_DASH + " Chapter One"), monkeypatch)
    bridge = ImportBridge()
    session = _gate_session(
        bridge,
        lib,
        directive=BankApplyDirective(action="duplicate", duplicate_action=DuplicateAction.replace),
    )
    session._install_dup_guard(task)
    found = task.find_duplicates(lib)
    assert found
    task.md_album_index = 0  # type: ignore[attr-defined]

    action = session.get_duplicate_action(task, found)

    assert action is BeetsDuplicateAction.KEEP  # new imports, old kept in DB
    assert session._replace_album_ids == {_require_id(found[0].id)}  # post-run Trash by id
