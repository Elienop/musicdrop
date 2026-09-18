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
import shutil
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
from beets.library import Album, Item, Library

from app.bank import store as bank_store
from app.beets.import_session import ImportBridge, WebImportSession, _SourceFiles
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


def _lib_album_in(
    artist: str, album: str, tmp_path: Path, *, path: str | None = None, with_file: bool = False
) -> Library:
    """A DB-only library holding ONE album — the in-library side of the twin.

    The album's FOLDER is created and its file is not: the music root is present
    and non-empty (so ``require_library_root`` passes, as it does on a real
    library) while the album itself stays file-less, which is what these gate
    tests are about.

    ``with_file`` writes the real FLAC fixture there instead, for the one test
    that needs the twin's files to actually reach Trash.
    """
    if path is None:
        path = str(tmp_path / "music" / f"{artist} - {album}" / "01.mp3")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if with_file:
        shutil.copyfile(Path(__file__).parent / "fixtures" / "silent.flac", path)
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
    trash_dir: Path | None = None,
) -> WebImportSession:
    """A hook session (run() never called) wired for the duplicate path.

    ``trash_dir`` is what a Replace needs: the hook disposes of the duplicate
    itself now, so an unwired pair makes it refuse and answer SKIP. Wired as a
    pair from one argument, as production resolves it.
    """
    session = WebImportSession.__new__(WebImportSession)
    session.logger = logging.getLogger("test.dupguard")
    session.bridge = bridge
    session._album_index = 0
    session._trash_dir = trash_dir
    session._trash_origins_dir = None if trash_dir is None else trash_dir.parent / "trash-origins"
    session._playlists_dir = None
    session._replace_album_ids = set()
    session._hook_replaced_album_ids = set()
    session._landed_album_ids = set()
    session._replace_was_refused = False
    session._dropped_item_ids = set()
    session.lib = lib
    session.unattended = unattended
    session.sweep = sweep
    session._bank_dir = bank_dir
    session._directive = directive
    # toppaths _task_folder scopes by (beets sets these in ImportSession.__init__).
    session.paths = toppaths if toppaths is not None else [b"/incoming"]
    # __init__ is skipped, so seed the record of what the run is READING; both
    # Replace routes ask it which library rows are the import's own.
    session._source_files = _SourceFiles()
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
    # beets' re-import exclusion (tasks.py find_duplicates) mirrored for the
    # variant path, pinned on BOTH directions of the subset check. To force
    # ONLY the guard's variant scan to engage (not the byte-exact path), the
    # library album is the HYPHEN title and the incoming is the EN-DASH twin.
    #
    # (i)  full re-import — the task's files are a SUPERSET of the existing
    #      album's files (it re-imports them all) -> EXCLUDED (being replaced).
    # (ii) partial import — the existing album has STRICTLY more files than the
    #      task (the task is a strict subset of it) -> FLAGGED, i.e. the subset
    #      is `existing_paths <= task_paths` on the EXISTING side, not the
    #      reverse. A mutant that reversed the subset direction would wrongly
    #      exclude (ii) and trip its assertion.
    base = tmp_path / "music" / "Radiohead"
    lib_files = [str(base / f"{n:02d}.mp3") for n in (1, 2, 3)]
    lib_album = "Greatest Hits - Chapter One"
    incoming = "Greatest Hits " + _EN_DASH + " Chapter One"

    def make_album(files: list[str], album: str, title_fmt: str) -> list[Item]:
        return [
            Item(
                artist="Radiohead",
                albumartist="Radiohead",
                album=album,
                title=title_fmt.format(n=n),
                track=n,
                length=200.0,
                path=os.fsencode(p),
            )
            for n, p in enumerate(files, 1)
        ]

    lib = Library(str(tmp_path / "library.db"), directory=str(base))
    lib_album_obj = lib.add_album(make_album(lib_files, lib_album, "Chapter {n}"))

    def make_task(paths: list[str]) -> ImportTask:
        def fake_tag_album(items: Any, search_ids: Any = None) -> tuple[str, str, Proposal]:
            return ("Radiohead", incoming, Proposal([], BeetsRec.none))

        monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)
        task = ImportTask(
            toppath=None,
            paths=[b"/incoming/album"],
            items=make_album(paths, incoming, "Chapter {n}"),
        )
        task.lookup_candidates([])
        task.set_choice(Action.ASIS)
        return task

    session = _gate_session(ImportBridge(), lib)

    # (i) full re-import: task superset-contains every existing file -> no dup.
    full = make_task(lib_files)
    session._install_dup_guard(full)
    assert full.find_duplicates(lib) == []

    # (ii) partial import: task has only file 1, album has all three -> dup.
    partial = make_task([lib_files[0]])
    session._install_dup_guard(partial)
    flagged = partial.find_duplicates(lib)
    assert [a.id for a in flagged] == [lib_album_obj.id]


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
    # The variant gate routes through the SAME banking as the exact case, so a
    # gated row stores its matched release too: the apply replays the variant
    # release the sweep chose instead of re-running the lookup.
    assert row.parked is not None
    assert row.parked.candidate.options[0].release_id == "a9"  # _match's album_id
    assert row.parked.candidate.album_after.album == "Greatest Hits " + _EN_DASH + " Chapter One"


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


def test_variant_gate_directive_replace_resolves_the_twin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With an explicit dup decision, the resolution machinery works on the
    # variant twin exactly as on an exact one. The twin's file was never created,
    # so it is a ghost: nothing to move, and the hook drops its rows itself.
    lib = _lib_album_in("Radiohead", "Greatest Hits - Chapter One", tmp_path)
    task = _apply_task(_match("Greatest Hits " + _EN_DASH + " Chapter One"), monkeypatch)
    bridge = ImportBridge()
    session = _gate_session(
        bridge,
        lib,
        directive=BankApplyDirective(action="duplicate", duplicate_action=DuplicateAction.replace),
        trash_dir=tmp_path / "trash",
    )
    session._install_dup_guard(task)
    found = task.find_duplicates(lib)
    twin_id = _require_id(found[0].id)
    task.md_album_index = 0  # type: ignore[attr-defined]

    action = session.get_duplicate_action(task, found)

    # KEEP, not REMOVE: beets' REMOVE would re-run find_duplicates with the
    # EXACT query, which is what the guard exists to widen — the variant twin is
    # not in that result, so the album the user replaced would have survived.
    assert action is BeetsDuplicateAction.KEEP
    assert lib.get_album(twin_id) is None  # the hook dropped the twin's rows
    assert session._hook_replaced_album_ids == {twin_id}
    assert session._replace_album_ids == set()  # nothing left for the post-run pass
    assert not (tmp_path / "trash").exists()  # a ghost has nothing to move


def test_variant_gate_directive_replace_moves_a_twin_with_files_to_trash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The widened guard's twin, this time WITH its file on disk.

    The sibling above is a ghost, so it only proves the rows-only arm. A twin the
    exact query cannot see and whose file IS there must reach Trash — reversibly,
    with an origin record — before beets places the incoming album. Nothing else
    in the suite covers a variant twin whose files move.
    """
    lib = _lib_album_in("Radiohead", "Greatest Hits - Chapter One", tmp_path, with_file=True)
    task = _apply_task(_match("Greatest Hits " + _EN_DASH + " Chapter One"), monkeypatch)
    session = _gate_session(
        ImportBridge(),
        lib,
        directive=BankApplyDirective(action="duplicate", duplicate_action=DuplicateAction.replace),
        trash_dir=tmp_path / "trash",
    )
    session._install_dup_guard(task)
    found = task.find_duplicates(lib)
    twin_id = _require_id(found[0].id)
    twin_file = Path(os.fsdecode(next(iter(found[0].items())).path))
    assert twin_file.is_file()  # the premise: this twin is not a ghost
    task.md_album_index = 0  # type: ignore[attr-defined]

    action = session.get_duplicate_action(task, found)

    assert action is BeetsDuplicateAction.KEEP
    assert lib.get_album(twin_id) is None
    assert not twin_file.exists()  # moved, not copied
    moved = [p for p in (tmp_path / "trash").rglob("*") if p.is_file()]
    assert len(moved) == 1, f"expected the twin's file under Trash, found {moved}"
    origins = [p for p in (tmp_path / "trash-origins").rglob("*.json")]
    assert len(origins) == 1, f"a Trash move must record where it came from: {origins}"


@pytest.mark.parametrize("trash_wired", [False, True])
def test_a_refused_hook_latches_that_the_run_replaced_nothing(
    trash_wired: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The WRITE half of the latch that stops the banked seed, driven by the hook.

    ``_replace_was_refused`` is one line, and it is the whole close of "a failed
    hook-trash later trashed the second album": with it unset, a run whose
    Replace refused still lets ``_seed_replace_from_directive`` move the user's
    copies to Trash while their replacement was never imported. Deleting the
    assignment left the whole suite green (measured by the code seat), because
    the only test that read the flag set it by hand.

    Here the hook itself sets it: the Trash pair is unwired, so the Replace
    refuses and answers SKIP. ``trash_wired=True`` is the control — the same
    call with the pair wired disposes of the duplicate and leaves the latch
    clear, so this cannot pass on a build that latches unconditionally.
    """
    lib = _lib_album_in("Radiohead", "In Rainbows", tmp_path, with_file=True)
    task = _apply_task(_match("In Rainbows"), monkeypatch)
    session = _gate_session(
        ImportBridge(),
        lib,
        directive=BankApplyDirective(action="duplicate", duplicate_action=DuplicateAction.replace),
        trash_dir=(tmp_path / "trash") if trash_wired else None,
    )
    found = task.find_duplicates(lib)
    twin_id = _require_id(found[0].id)
    task.md_album_index = 0  # type: ignore[attr-defined]
    assert session._replace_was_refused is False  # the premise

    action = session.get_duplicate_action(task, found)

    if trash_wired:
        assert action is BeetsDuplicateAction.KEEP
        assert lib.get_album(twin_id) is None
        assert session._replace_was_refused is False, "a healthy Replace latched a refusal"
    else:
        assert action is BeetsDuplicateAction.SKIP
        assert lib.get_album(twin_id) is not None, "a refused Replace dropped the rows"
        assert session._replace_was_refused is True, "the banked seed was left free to run"


# --------------------------------------------------------------------------
# the variant signal is a compound (artist, title) key, and the normalization
# ladder is what makes symbol-only titles group at all.
# --------------------------------------------------------------------------


def test_variant_gate_different_artist_same_title_is_not_a_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A different ARTIST with the same title is not a duplicate. Here the titles
    # fold to the identical key; only the artist half of the compound (artist,
    # title) key differs. A guard keyed on title alone (dropping artist) would
    # wrongly flag this; pin the artist half.
    lib = _lib_album_in("Nirvana", "Greatest Hits - Chapter One", tmp_path)
    task = _apply_task(_match("Greatest Hits " + _EN_DASH + " Chapter One"), monkeypatch)
    session = _gate_session(ImportBridge(), lib)
    session._install_dup_guard(task)
    assert task.find_duplicates(lib) == []


def test_variant_gate_symbol_title_rung2_deluxe_pair_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # symbol-only titles: plain normalize() empties BOTH "=" and its
    # "(Deluxe Edition)" twin, but the soft rung (_soft_normalize, rung 2) keeps
    # the shared non-empty key "=" so the pair still groups. A mutant that drops
    # the soft rung (_fuzzy_part = normalize) empties both keys, the guard sees
    # "no usable key", and misses this common deluxe/standard dup shape.
    lib = _lib_album_in("Radiohead", "=", tmp_path)
    task = _apply_task(_match("= (Deluxe Edition)"), monkeypatch)
    session = _gate_session(ImportBridge(), lib)
    session._install_dup_guard(task)
    assert [a.album for a in task.find_duplicates(lib)] == ["="]


# --------------------------------------------------------------------------
# the normalized index is built ONCE per session and rebuilt on library
# change (count + max-id signature).
# --------------------------------------------------------------------------


def test_variant_index_invalidates_when_library_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pin (b): an album added by an earlier task in the same session run must
    # be visible to the NEXT task's guard. The library is empty when task 1 runs
    # (so its guard builds an EMPTY index), then task 1's apply lands as a real
    # library album, then task 2's guard must SEE IT. A "never invalidate the
    # cache" mutant keeps the empty index and misses it.
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    assert list(lib.albums()) == []
    session = _gate_session(ImportBridge(), lib)

    # Task 1: empty lib -> no dup found.
    added_path = str(tmp_path / "music" / "Greatest Hits - Chapter One" / "01.mp3")
    t1 = _apply_task(_match("Greatest Hits - Chapter One"), monkeypatch)
    session._install_dup_guard(t1)
    assert t1.find_duplicates(lib) == []

    # Simulate task 1's APPLY landing that album into the library.
    lib.add_album(
        [
            Item(
                artist="Radiohead",
                albumartist="Radiohead",
                album="Greatest Hits - Chapter One",
                title="Chapter One",
                track=1,
                length=200.0,
                path=os.fsencode(added_path),
            )
        ]
    )

    # Task 2: the EN-DASH twin now must SEE the album task 1 added.
    t2 = _apply_task(_match("Greatest Hits " + _EN_DASH + " Chapter One"), monkeypatch)
    session._install_dup_guard(t2)
    assert [a.album for a in t2.find_duplicates(lib)] == ["Greatest Hits - Chapter One"]


def test_variant_index_built_once_until_library_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pin (a): the expensive full-scan (lib.albums(), NO query) runs EXACTLY
    # ONCE per session WHILE THE LIBRARY IS UNCHANGED — the quiescent shape
    # (sweeps whose tasks bank/skip; re-scans). An APPLIED task mutates the
    # library and legitimately rebuilds on the next call, so this test's claim
    # is scoped to quiescence on purpose; the invalidation test pins the other
    # half. beets'
    # exact path queries with an argument (lib.albums(dup_query)), so a zero-arg
    # full-scan is uniquely the guard's normalized-index build; a "rebuild per
    # task" mutant would scan once per guard call.
    lib = _lib_album_in("Radiohead", "Greatest Hits - Chapter One", tmp_path)
    full_scans: list[int] = []
    real_albums = lib.albums

    def spy(*args: Any, **kwargs: Any) -> Any:
        if not args and not kwargs:
            full_scans.append(1)
        return real_albums(*args, **kwargs)

    monkeypatch.setattr(lib, "albums", spy)
    session = _gate_session(ImportBridge(), lib)

    t1 = _apply_task(_match("Greatest Hits " + _EN_DASH + " Chapter One"), monkeypatch)
    session._install_dup_guard(t1)
    assert [a.album for a in t1.find_duplicates(lib)] == ["Greatest Hits - Chapter One"]

    # Second guard on the SAME unchanged library (a case+dash variant twin).
    t2 = _apply_task(_match("greatest hits " + _EN_DASH + " chapter one"), monkeypatch)
    session._install_dup_guard(t2)
    assert [a.album for a in t2.find_duplicates(lib)] == ["Greatest Hits - Chapter One"]

    assert full_scans == [1]  # built once, reused by the second task


def test_variant_gate_childless_album_is_excluded_as_reimport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A library album with ZERO item rows (a childless album) has an EMPTY
    # file set, and the empty set is a (trivial) subset of the incoming task's
    # files -- so it is EXCLUDED as a re-import, exactly as on the exact path.
    # The old guard's `existing_paths and existing_paths <= task_paths` made the
    # empty set a NON-subset and wrongly flagged it; the guard now mirrors beets'
    # `existing_paths <= task_paths` (empty included). Force the variant path by
    # keeping the titles a non-byte-identical twin (hyphen vs en-dash).
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    childless = Album(lib, albumartist="Radiohead", album="Greatest Hits - Chapter One")
    lib.add(childless)
    assert [al.id for al in lib.albums()] == [childless.id]
    assert list(childless.items()) == []

    task = _apply_task(_match("Greatest Hits " + _EN_DASH + " Chapter One"), monkeypatch)
    session = _gate_session(ImportBridge(), lib)
    session._install_dup_guard(task)
    assert task.find_duplicates(lib) == []


def test_variant_index_survives_rowid_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SQLite `id INTEGER PRIMARY KEY` (no AUTOINCREMENT) REUSES the rowid of a
    # deleted max-id row, so delete-then-reinsert of the newest album — the
    # exact shape beets' merge takes through remove_replaced + add_album —
    # leaves (COUNT, MAX(id)) unchanged. Only `lib.revision` in the signature
    # catches it; without it the index serves stale Album objects bound to a
    # reused id, and a duplicate prompt could show one album while `replace`
    # trashes another. Pin: after the swap, the guard must see the NEW title
    # under the reused id and must NOT match the dead one.
    lib = _lib_album_in("Radiohead", "Filler Album", tmp_path)
    kid_path = str(tmp_path / "music" / "Radiohead - Kid A" / "01.mp3")
    kid = Item(
        artist="Radiohead",
        albumartist="Radiohead",
        album="Kid A",
        title="Chapter One",
        track=1,
        length=200.0,
        path=os.fsencode(kid_path),
    )
    kid_album = lib.add_album([kid])
    session = _gate_session(ImportBridge(), lib)

    # Guard call 1 caches an index holding the max-id row ("Kid A").
    t1 = _apply_task(_match("kid a"), monkeypatch)
    session._install_dup_guard(t1)
    assert [a.album for a in t1.find_duplicates(lib)] == ["Kid A"]
    assert kid_album.id is not None
    max_id = int(kid_album.id)

    # Merge shape: the max-id album is deleted, a new one reuses its rowid.
    kid_album.remove(delete=False, with_items=True)
    replacement = Item(
        artist="Radiohead",
        albumartist="Radiohead",
        album="Amnesiac",
        title="Chapter One",
        track=1,
        length=200.0,
        path=os.fsencode(str(tmp_path / "music" / "Radiohead - Amnesiac" / "01.mp3")),
    )
    new_album = lib.add_album([replacement])
    assert new_album.id is not None
    assert int(new_album.id) == max_id  # the rowid really was reused

    # A twin of the NEW title must resolve to the live row...
    t2 = _apply_task(_match("AMNESIAC"), monkeypatch)
    session._install_dup_guard(t2)
    hits = t2.find_duplicates(lib)
    assert [(a.id, a.album) for a in hits] == [(max_id, "Amnesiac")]
    # ...and the dead title must no longer match anything.
    t3 = _apply_task(_match("kid a"), monkeypatch)
    session._install_dup_guard(t3)
    assert t3.find_duplicates(lib) == []


def test_variant_index_sees_out_of_process_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `lib.revision` is in-memory: a SECOND connection to the same db file (a
    # stray `beet` CLI in production) never bumps it. `PRAGMA data_version` is
    # what notices such writers — it changes on any OTHER connection's commit.
    # Shape chosen as the WORST case: the foreign writer deletes the MAX-id
    # album and reinserts (SQLite reuses the rowid), so COUNT and MAX(id) both
    # come back identical and revision never moved — every row-set aggregate
    # is blind here; only data_version catches it. Without it the cache would
    # serve a ghost Album bound to a reused id (round-3 review's reproduced
    # wrong-album-replace hazard).
    lib = _lib_album_in("Radiohead", "Filler Album", tmp_path)
    ok_path = str(tmp_path / "music" / "Radiohead - OK Computer" / "01.mp3")
    ok = Item(
        artist="Radiohead",
        albumartist="Radiohead",
        album="OK Computer",
        title="Chapter One",
        track=1,
        length=200.0,
        path=os.fsencode(ok_path),
    )
    lib.add_album([ok])
    session = _gate_session(ImportBridge(), lib)

    # Prime the cache on THIS connection.
    t1 = _apply_task(_match("ok computer"), monkeypatch)
    session._install_dup_guard(t1)
    assert [a.album for a in t1.find_duplicates(lib)] == ["OK Computer"]

    # Foreign writer: delete the MAX-id album ("OK Computer") and reinsert a
    # different one — SQLite reuses the freed max rowid, so COUNT and MAX are
    # unchanged and this lib's revision never moved.
    other = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    victim = next(a for a in other.albums() if a.album == "OK Computer")
    assert victim.id is not None
    victim_id = int(victim.id)
    victim.remove(delete=False, with_items=True)
    reborn = other.add_album(
        [
            Item(
                artist="Radiohead",
                albumartist="Radiohead",
                album="In Rainbows",
                title="Chapter One",
                track=1,
                length=200.0,
                path=os.fsencode(str(tmp_path / "music" / "Radiohead - In Rainbows" / "01.mp3")),
            )
        ]
    )
    assert reborn.id is not None
    assert int(reborn.id) == victim_id  # the rowid really was reused

    # The live title resolves through the reused id; the dead one matches nothing.
    t2 = _apply_task(_match("in rainbows"), monkeypatch)
    session._install_dup_guard(t2)
    assert [(a.id, a.album) for a in t2.find_duplicates(lib)] == [(victim_id, "In Rainbows")]
    t3 = _apply_task(_match("ok computer"), monkeypatch)
    session._install_dup_guard(t3)
    assert t3.find_duplicates(lib) == []
