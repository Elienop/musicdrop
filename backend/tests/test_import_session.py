import logging
import os
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

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

from app.beets.import_mapping import embedded_art
from app.beets.import_session import (
    ImportBridge,
    InLibraryCopyError,
    WebImportSession,
    is_in_library_source,
    run_import_worker,
)
from app.models.import_models import (
    AlbumOutcome,
    AlbumOutcomeStatus,
    ImportAction,
    ImportChoice,
    ImportSearch,
    Recommendation,
)


@pytest.fixture(autouse=True)
def _force_serial_imports() -> Iterator[None]:
    # Imports always run single-threaded (config["threaded"] = False). Reset it
    # around every test so none leaks the flag to the next — the worker test
    # deliberately flips it True to prove run_import_worker overrides it.
    config["threaded"] = False
    yield
    config["threaded"] = False


def _build_match(rec_level: BeetsRec) -> AlbumMatch:
    """A canned AlbumMatch. For a strong rec we make a perfect (distance 0)
    match; otherwise we mistitle the album so the distance is non-trivial.
    """
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


def _build_other_match() -> AlbumMatch:
    """A distinct release the 'search' re-lookup resolves to (different album)."""
    items = [
        Item(artist="Radiohead", album="Amnesiac", title="Pyramid Song", track=1, length=200.0)
    ]
    tracks = [TrackInfo(title="Pyramid Song", track_id="t9", index=1, length=200.0)]
    info = AlbumInfo(
        tracks=tracks,
        album="Amnesiac",
        artist="Radiohead",
        album_id="a9",
        data_source="MusicBrainz",
        data_url="https://mb/a9",
        year=2001,
        va=False,
    )
    pairs, extra_i, extra_t = assign_items(items, info.tracks)
    return AlbumMatch(distance(items, info, pairs), info, dict(pairs), extra_i, extra_t)


def _patch_tag_album(monkeypatch: pytest.MonkeyPatch, match: AlbumMatch, rec: BeetsRec) -> None:
    """Patch the seam beets uses to fetch candidates so no network is hit.

    ImportTask.lookup_candidates calls beets.importer.tasks.tag_album (imported
    at module top in tasks.py); replacing it with a canned Proposal is the
    cleanest hermetic seam (spike-verified).
    """

    def fake_tag_album(items: Any, search_ids: Any = None) -> tuple[str, str, Proposal]:
        return ("Radiohead", "OK Computer", Proposal([match], rec))

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)


def _make_session(bridge: ImportBridge) -> WebImportSession:
    """Construct a session without a real Library (we never call run()).

    __new__ skips ImportSession.__init__ (which needs a Library); we set only
    what the hooks read. This keeps the test focused on the decision logic.
    """
    session = WebImportSession.__new__(WebImportSession)
    session.logger = logging.getLogger("test.import")
    session.bridge = bridge
    # __init__ is skipped, so set the park-index counter the hook relies on.
    session._album_index = 0
    # __init__ is skipped, so default the attended flag the hooks now read.
    session.unattended = False
    # __init__ is skipped, so default the sweep flag + bank dir the hooks read.
    session.sweep = False
    session._bank_dir = None
    # __init__ is skipped, so default the apply directive the hooks now read.
    session._directive = None
    # __init__ is skipped, so seed the album-id stash choose_match appends to.
    session._await_album_id = []
    # __init__ is skipped, so default the astracks-in-flight flag choose_item
    # now reads (armed by choose_match when a park is decided "as tracks").
    session._astracks_in_flight = False
    # __init__ is skipped, so the in-library guard's session.paths read has a
    # value; empty means the guard no-ops (these tests drive run() directly).
    session.paths = []
    return session


def _make_task(match: AlbumMatch, monkeypatch: pytest.MonkeyPatch, rec: BeetsRec) -> ImportTask:
    _patch_tag_album(monkeypatch, match, rec)
    task = ImportTask(toppath=None, paths=[b"/music/album"], items=list(match.mapping.keys()))
    task.lookup_candidates([])  # populates cur_artist/cur_album/candidates/rec
    return task


def test_strong_rec_auto_applies_top_match(monkeypatch: pytest.MonkeyPatch) -> None:
    match = _build_match(BeetsRec.strong)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.strong)

    task.choose_match(session)  # calls session.choose_match -> set_choice

    assert task.choice_flag is Action.APPLY
    assert task.apply is True
    assert task.match is match
    # Nothing was parked: a strong rec auto-applies (mirrors beets).
    assert bridge.pending_count() == 0


def test_uncertain_rec_parks_then_applies_pushed_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)

    # choose_match blocks until a choice is pushed; run it on a worker thread.
    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    parked = bridge.get_parked(timeout=2.0)
    assert parked is not None
    assert parked.candidate.recommendation.value == "medium"

    bridge.push_choice(parked.album_index, ImportChoice(action=ImportAction.apply))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)

    assert task.choice_flag is Action.APPLY
    assert task.match is match


def test_search_choice_relooks_up_and_reparks_new_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.beets.import_session as session_mod

    match = _build_match(BeetsRec.medium)
    other = _build_other_match()
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)
    monkeypatch.setattr(session_mod, "relookup", lambda t, s: ([other], BeetsRec.strong))

    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    first = bridge.get_parked(timeout=2.0)
    assert first is not None
    assert first.candidate.search_revision == 0
    assert first.candidate.album_after.album == "OK Computer"

    bridge.push_choice(
        first.album_index,
        ImportChoice(action=ImportAction.search, search=ImportSearch(release_id="a9")),
    )

    second = bridge.get_parked(timeout=2.0)
    assert second is not None
    assert second.album_index == first.album_index
    assert second.candidate.search_revision == 1
    assert second.candidate.search_feedback is None
    assert second.candidate.album_after.album == "Amnesiac"  # the re-looked-up release

    # A re-emitted needs_review outcome flips the registry row back from `decided`.
    assert any(o.status is AlbumOutcomeStatus.needs_review for o in bridge.drain_outcomes())

    bridge.push_choice(second.album_index, ImportChoice(action=ImportAction.apply))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)
    assert task.match is other  # apply selects from the NEW candidate list


def test_search_with_no_results_keeps_previous_and_sets_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.beets.import_session as session_mod

    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)
    monkeypatch.setattr(session_mod, "relookup", lambda t, s: ([], BeetsRec.none))

    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    first = bridge.get_parked(timeout=2.0)
    assert first is not None
    bridge.push_choice(
        first.album_index,
        ImportChoice(action=ImportAction.search, search=ImportSearch(release_id="nope")),
    )

    second = bridge.get_parked(timeout=2.0)
    assert second is not None
    assert second.candidate.search_revision == 1
    assert second.candidate.search_feedback is not None
    assert "No release found" in second.candidate.search_feedback
    assert second.candidate.album_after.album == "OK Computer"  # previous match kept

    bridge.push_choice(second.album_index, ImportChoice(action=ImportAction.apply))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)
    assert task.match is match  # original candidate still applies


def test_uncertain_rec_skip_choice_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)

    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    parked = bridge.get_parked(timeout=2.0)
    assert parked is not None
    bridge.push_choice(parked.album_index, ImportChoice(action=ImportAction.skip))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)

    assert task.choice_flag is Action.SKIP
    assert task.skip is True
    assert task.match is None


def test_unattended_choose_match_skips_instead_of_parking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unattended: a non-strong match emits the needs_review outcome (so the feed
    # records the set-aside) but returns SKIP instead of parking + blocking.
    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    session.unattended = True
    task = _make_task(match, monkeypatch, BeetsRec.medium)

    result = session.choose_match(task)

    assert result is Action.SKIP
    assert bridge.pending_count() == 0  # did NOT park
    outcomes = bridge.drain_outcomes()
    assert any(o.status is AlbumOutcomeStatus.needs_review for o in outcomes)


def test_unattended_worker_runs_to_completion_without_parking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard: an unattended worker never blocks the worker thread.

    Drives the real worker entrypoint ``run_import_worker`` over a real
    ``WebImportSession(unattended=True)``; a true import would read + group the
    folder and then hit ``choose_match``, so we stub ``run()`` to drive the REAL
    unattended ``choose_match`` for a canned non-strong proposal (via
    ``_patch_tag_album``/``_make_task``). It must complete (no park, no deadlock)
    and record the album as set aside (needs_review).
    """
    from app.beets.import_session import run_import_worker

    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    session.unattended = True
    # run_import_worker's post-run trash pass reads these (trash_dir=None -> it
    # returns before touching lib); __init__ is bypassed, so set them here.
    session._trash_dir = None
    session._replace_album_ids = set()
    task = _make_task(match, monkeypatch, BeetsRec.medium)

    def fake_run(self: WebImportSession) -> None:
        task.choose_match(self)

    monkeypatch.setattr(WebImportSession, "run", fake_run)

    done = threading.Event()

    def worker() -> None:
        run_import_worker(session, move=None)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    # If the unattended path ever parked, the worker would block here forever.
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)

    assert task.choice_flag is Action.SKIP  # set aside, not applied
    assert bridge.pending_count() == 0  # nothing parked
    outcomes = bridge.drain_outcomes()
    assert any(o.status is AlbumOutcomeStatus.needs_review for o in outcomes)


def test_abort_choice_raises_import_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    from beets.importer.session import ImportAbortError

    config["threaded"] = False
    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)

    raised: dict[str, bool] = {"abort": False}
    done = threading.Event()

    def worker() -> None:
        try:
            task.choose_match(session)
        except ImportAbortError:
            raised["abort"] = True
        finally:
            done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    parked = bridge.get_parked(timeout=2.0)
    assert parked is not None
    # The abort action makes the session raise beets' ImportAbortError out of
    # choose_match; beets' run() loop (chunk 2) catches it to stop the import.
    bridge.push_choice(parked.album_index, ImportChoice(action=ImportAction.abort))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)
    assert raised["abort"] is True
    # The aborted album's reply slot was cleaned up (no bridge leak).
    assert bridge.pending_count() == 0


@pytest.mark.anyio
async def test_bridge_ferries_candidate_out_and_choice_in_across_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Demonstrate the async-consumer shape: a worker thread parks an album and
    an async consumer (via anyio.to_thread) drains it and replies. This is the
    seam the future async API layer plugs into.
    """
    import anyio

    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)

    worker = threading.Thread(target=lambda: task.choose_match(session), daemon=True)
    worker.start()

    parked = await anyio.to_thread.run_sync(lambda: bridge.get_parked(2.0))
    assert parked is not None
    assert parked.candidate.data_source == "MusicBrainz"
    assert parked.candidate.options  # ranked alternatives mapped

    await anyio.to_thread.run_sync(
        lambda: bridge.push_choice(parked.album_index, ImportChoice(action=ImportAction.apply))
    )
    await anyio.to_thread.run_sync(lambda: worker.join(2.0))
    assert task.choice_flag is Action.APPLY


def test_run_import_worker_forces_single_threaded_and_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_import_worker must set config['threaded']=False and invoke session.run().

    We stub session.run to record the threaded flag at call time, proving the
    worker enforces serial execution regardless of the ambient config.
    """
    from app.beets.import_session import run_import_worker

    config["threaded"] = True  # ambient default; the worker must override it.
    seen: dict[str, Any] = {}

    class FakeSession:
        # The post-run trash pass reads these off the session; trash_dir=None
        # makes it return early before touching lib/get_album.
        lib = None
        # The in-library guard reads session.paths; empty -> guard no-ops.
        paths: ClassVar[list[bytes]] = []
        _replace_album_ids: ClassVar[set[int]] = set()
        _trash_dir = None

        def run(self) -> None:
            seen["threaded"] = bool(config["threaded"])

    run_import_worker(FakeSession())  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised
    assert seen["threaded"] is False


@pytest.mark.parametrize(
    ("action", "expected_flag"),
    [
        (ImportAction.asis, Action.ASIS),
        (ImportAction.astracks, Action.TRACKS),
    ],
)
def test_uncertain_rec_translates_asis_and_astracks(
    monkeypatch: pytest.MonkeyPatch, action: ImportAction, expected_flag: Action
) -> None:
    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)

    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    parked = bridge.get_parked(timeout=2.0)
    assert parked is not None
    bridge.push_choice(parked.album_index, ImportChoice(action=action))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)
    assert task.choice_flag is expected_flag


def test_no_candidates_skips_without_parking(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_tag_album(items: Any, search_ids: Any = None) -> tuple[str, str, Proposal]:
        return ("Artist", "Album", Proposal([], BeetsRec.none))

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = ImportTask(
        toppath=None,
        paths=[b"/music/album"],
        items=[Item(artist="Artist", album="Album", title="X", track=1, length=10.0)],
    )
    task.lookup_candidates([])

    task.choose_match(session)

    assert task.choice_flag is Action.SKIP
    assert bridge.pending_count() == 0


def test_apply_with_out_of_range_index_falls_back_to_top(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)

    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    parked = bridge.get_parked(timeout=2.0)
    assert parked is not None
    bridge.push_choice(
        parked.album_index, ImportChoice(action=ImportAction.apply, candidate_index=99)
    )
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)
    # Out-of-range index defensively falls back to the top candidate.
    assert task.choice_flag is Action.APPLY
    assert task.match is match


def test_bridge_outcome_channel_round_trips() -> None:
    bridge = ImportBridge()
    assert bridge.drain_outcomes() == []  # empty, non-blocking
    outcome = AlbumOutcome(
        album_index=0,
        folder="/music/album",
        artist="Radiohead",
        album="OK Computer",
        recommendation=Recommendation.strong,
        confidence=99.0,
        status=AlbumOutcomeStatus.applied,
    )
    bridge.note_outcome(outcome)
    drained = bridge.drain_outcomes()
    assert drained == [outcome]
    # Draining again yields nothing (the queue was consumed).
    assert bridge.drain_outcomes() == []


def test_strong_rec_emits_applied_outcome_without_parking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    match = _build_match(BeetsRec.strong)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.strong)

    task.choose_match(session)

    assert bridge.pending_count() == 0  # never parked (unchanged chunk-1 behavior)
    outcomes = bridge.drain_outcomes()
    assert len(outcomes) == 1
    assert outcomes[0].status is AlbumOutcomeStatus.applied
    assert outcomes[0].recommendation is Recommendation.strong
    assert outcomes[0].album == "OK Computer"


def test_uncertain_rec_emits_needs_review_outcome_at_park(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)

    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    parked = bridge.get_parked(timeout=2.0)
    assert parked is not None
    # The needs_review outcome is emitted BEFORE park, so it is already drainable
    # while the worker blocks on the reply.
    outcomes = bridge.drain_outcomes()
    assert len(outcomes) == 1
    assert outcomes[0].album_index == parked.album_index
    assert outcomes[0].status is AlbumOutcomeStatus.needs_review
    assert outcomes[0].recommendation is Recommendation.medium

    bridge.push_choice(parked.album_index, ImportChoice(action=ImportAction.apply))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)


def test_embedded_art_none_for_missing_or_artless(tmp_path: Any) -> None:
    # A path that does not exist -> None (no crash).
    assert embedded_art(str(tmp_path / "nope.mp3")) is None


def test_park_records_art_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    import os

    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)
    # The harness items carry no real .path; set one so choose_match can capture
    # the parked album's art source (presence isn't required - just the path).
    art_path = os.fsencode(str(tmp_path / "a.flac"))
    task.items[0].path = art_path

    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    parked = bridge.get_parked(timeout=2.0)
    assert parked is not None
    assert session.bridge.art_source(parked.album_index) == os.fsdecode(art_path)

    bridge.push_choice(parked.album_index, ImportChoice(action=ImportAction.skip))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)


def test_no_candidates_emits_skipped_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_tag_album(items: Any, search_ids: Any = None) -> tuple[str, str, Proposal]:
        return ("Artist", "Album", Proposal([], BeetsRec.none))

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = ImportTask(
        toppath=None,
        paths=[b"/music/album"],
        items=[Item(artist="Artist", album="Album", title="X", track=1, length=10.0)],
    )
    task.lookup_candidates([])

    task.choose_match(session)

    assert bridge.pending_count() == 0
    outcomes = bridge.drain_outcomes()
    assert len(outcomes) == 1
    assert outcomes[0].status is AlbumOutcomeStatus.skipped


def test_bridge_duplicate_channel_round_trips() -> None:
    import threading

    from app.models.import_models import (
        DuplicateAction,
        DuplicateDecision,
        DuplicatePrompt,
        ExistingAlbum,
        IncomingAlbum,
    )

    bridge = ImportBridge()
    assert bridge.get_parked_duplicate(timeout=0) is None  # empty, non-blocking

    prompt = DuplicatePrompt(
        album_index=5,
        incoming=IncomingAlbum(
            album_artist="Radiohead",
            album="In Rainbows",
            year=2007,
            track_count=10,
            format="FLAC",
            bitrate_kbps=900,
            folder="/incoming",
            has_current_art=False,
        ),
        existing=[
            ExistingAlbum(
                album_id=1,
                album_artist="Radiohead",
                album="In Rainbows",
                year=2007,
                track_count=9,
                format="MP3",
                bitrate_kbps=320,
                folder="/music",
            )
        ],
    )

    got: dict[str, DuplicateDecision] = {}
    done = threading.Event()

    def worker() -> None:
        got["decision"] = bridge.park_duplicate(prompt)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    parked = bridge.get_parked_duplicate(timeout=2.0)
    assert parked is not None
    assert parked.album_index == 5
    assert parked.incoming.album == "In Rainbows"

    bridge.push_duplicate_decision(5, DuplicateDecision(action=DuplicateAction.replace))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)
    assert got["decision"].action is DuplicateAction.replace


def test_push_duplicate_decision_unknown_index_raises_keyerror() -> None:
    import pytest

    from app.models.import_models import DuplicateAction, DuplicateDecision

    bridge = ImportBridge()
    with pytest.raises(KeyError):
        bridge.push_duplicate_decision(99, DuplicateDecision(action=DuplicateAction.skip_new))


def test_run_import_worker_forces_duplicate_action_ask() -> None:
    """The worker must force import.duplicate_action=ask so the hook always fires."""
    from app.beets.import_session import run_import_worker

    config["import"]["duplicate_action"] = "keep"  # user config says keep-both
    seen: dict[str, Any] = {}

    class FakeSession:
        lib = None
        paths: ClassVar[list[bytes]] = []
        _replace_album_ids: ClassVar[set[int]] = set()
        _trash_dir = None

        def run(self) -> None:
            seen["dup_action"] = config["import"]["duplicate_action"].get()

    run_import_worker(FakeSession())  # type: ignore[arg-type]  # minimal stand-in
    assert seen["dup_action"] == "ask"


def test_run_import_worker_trashes_replace_ids_after_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recorded Replace ids are moved to Trash AFTER run() returns, by id."""
    from pathlib import Path

    import app.beets.import_session as session_mod
    from app.beets.import_session import run_import_worker

    trashed: list[int] = []

    def fake_trash(lib: Any, album: Any, *, trash_dir: Path) -> str:
        trashed.append(int(album.id))
        return str(trash_dir)

    monkeypatch.setattr(session_mod, "trash_album", fake_trash)

    class _Album:
        def __init__(self, album_id: int) -> None:
            self.id = album_id

    class _Lib:
        def get_album(self, album_id: int) -> Any:
            return _Album(album_id)

        def transaction(self) -> Any:
            import contextlib

            return contextlib.nullcontext()

        def music_dir_context(self) -> Any:
            # Real Library binds beets' music-dir ContextVar here; a no-op is fine
            # since this fake never expands paths off the main thread.
            import contextlib

            return contextlib.nullcontext()

    class FakeSession:
        lib = _Lib()
        paths: ClassVar[list[bytes]] = []
        _replace_album_ids: ClassVar[set[int]] = {11, 22}
        _trash_dir = Path("/tmp/trash")

        def run(self) -> None:
            # The new albums are imported during run(); trashing happens after.
            assert trashed == []

    run_import_worker(FakeSession())  # type: ignore[arg-type]
    assert sorted(trashed) == [11, 22]


class _ScopedMoveSession:
    """Minimal session that records config['import']['move'] seen during run()."""

    lib = None
    paths: ClassVar[list[bytes]] = []
    _replace_album_ids: ClassVar[set[int]] = set()
    _trash_dir = None

    def __init__(self) -> None:
        self.seen: bool | None = None

    def run(self) -> None:
        self.seen = config["import"]["move"].get(bool)


def test_scoped_move_sets_then_restores() -> None:
    from app.beets.import_session import run_import_worker

    config["import"]["move"] = False
    config["import"]["copy"] = True
    s = _ScopedMoveSession()
    run_import_worker(s, move=True)  # type: ignore[arg-type]  # minimal stand-in; only .run() is exercised
    assert s.seen is True  # honored during run
    assert config["import"]["move"].get(bool) is False  # restored after
    assert config["import"]["copy"].get(bool) is True


def test_scoped_move_restores_on_raise() -> None:
    from app.beets.import_session import run_import_worker

    config["import"]["move"] = False
    config["import"]["copy"] = True

    class _Boom(_ScopedMoveSession):
        def run(self) -> None:
            raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        run_import_worker(_Boom(), move=True)  # type: ignore[arg-type]  # minimal stand-in
    assert config["import"]["move"].get(bool) is False  # finally restored
    assert config["import"]["copy"].get(bool) is True


def test_default_move_none_touches_nothing() -> None:
    from app.beets.import_session import run_import_worker

    config["import"]["move"] = False
    config["import"]["copy"] = True
    run_import_worker(_ScopedMoveSession(), move=None)  # type: ignore[arg-type]  # minimal stand-in
    assert config["import"]["move"].get(bool) is False
    assert config["import"]["copy"].get(bool) is True


class _AddedAlbum:
    """Stand-in for the beets Album that task.add() attaches as task.album."""

    def __init__(self, album_id: int) -> None:
        self.id = album_id


def test_next_choose_match_flushes_previous_applied_album_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Task 1: a strong match auto-applies; its outcome carries album_id=None
    # (beets has not run task.add() yet at choose_match time).
    match = _build_match(BeetsRec.strong)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task1 = _make_task(match, monkeypatch, BeetsRec.strong)
    task1.choose_match(session)
    first = bridge.drain_outcomes()
    assert len(first) == 1
    assert first[0].status is AlbumOutcomeStatus.applied
    assert first[0].album_id is None

    # beets' user_query stage then runs _apply_choice -> task.add(lib), which
    # sets task.album; simulate that before the next task arrives (the
    # sequential pipeline guarantees this ordering).
    task1.album = _AddedAlbum(42)

    # Task 2 entering choose_match flushes task 1's follow-up outcome.
    task2 = _make_task(match, monkeypatch, BeetsRec.strong)
    task2.choose_match(session)
    outcomes = bridge.drain_outcomes()
    follow_ups = [o for o in outcomes if o.album_id is not None]
    assert len(follow_ups) == 1
    assert follow_ups[0].album_id == 42
    assert follow_ups[0].album_index == first[0].album_index
    assert follow_ups[0].status is AlbumOutcomeStatus.applied


def test_run_flushes_the_final_album_id_after_super_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The LAST task of an import has no "next choose_match" to flush it; the
    # run() override flushes once beets' run returns.
    from beets.importer.session import ImportSession

    match = _build_match(BeetsRec.strong)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.strong)

    def fake_super_run(self: ImportSession) -> None:
        # The "pipeline": choose the task, then beets adds it to the library.
        task.choose_match(self)
        task.album = _AddedAlbum(7)

    # Patch the PARENT run; WebImportSession.run's super().run() resolves to it.
    monkeypatch.setattr(ImportSession, "run", fake_super_run)
    session.run()

    outcomes = bridge.drain_outcomes()
    assert [o.album_id for o in outcomes] == [None, 7]
    assert outcomes[1].status is AlbumOutcomeStatus.applied


def test_decided_apply_also_gains_album_id_via_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A parked album the user resolves with apply is added by beets too; its
    # follow-up forces status=applied so the registry can never regress the
    # row's decided status (the needs_review upgrade branch matches by status).
    from beets.importer.session import ImportSession

    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)

    def fake_super_run(self: ImportSession) -> None:
        task.choose_match(self)  # parks; unblocked by push_choice below
        task.album = _AddedAlbum(9)

    monkeypatch.setattr(ImportSession, "run", fake_super_run)

    done = threading.Event()

    def worker() -> None:
        session.run()
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    parked = bridge.get_parked(timeout=2.0)
    assert parked is not None
    bridge.push_choice(parked.album_index, ImportChoice(action=ImportAction.apply))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)

    outcomes = bridge.drain_outcomes()
    assert outcomes[0].status is AlbumOutcomeStatus.needs_review
    assert outcomes[0].album_id is None
    assert outcomes[1].album_id == 9
    assert outcomes[1].status is AlbumOutcomeStatus.applied
    assert outcomes[1].album_index == outcomes[0].album_index


def test_flush_skips_tasks_beets_never_added(monkeypatch: pytest.MonkeyPatch) -> None:
    from beets.importer.session import ImportSession

    match = _build_match(BeetsRec.strong)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.strong)

    def fake_super_run(self: ImportSession) -> None:
        task.choose_match(self)  # applied at choose time, but never task.add()ed

    monkeypatch.setattr(ImportSession, "run", fake_super_run)
    session.run()

    outcomes = bridge.drain_outcomes()
    assert len(outcomes) == 1  # no follow-up without a library id
    assert outcomes[0].album_id is None


def test_is_in_library_source_inside(tmp_path: Path) -> None:
    lib_dir = os.fsencode(str(tmp_path / "library"))
    (tmp_path / "library" / "Artist" / "Album").mkdir(parents=True)
    assert is_in_library_source(lib_dir, str(tmp_path / "library" / "Artist" / "Album")) is True
    # The library root itself counts as inside.
    assert is_in_library_source(lib_dir, str(tmp_path / "library")) is True


def test_is_in_library_source_outside(tmp_path: Path) -> None:
    lib_dir = os.fsencode(str(tmp_path / "library"))
    assert is_in_library_source(lib_dir, str(tmp_path / "downloads" / "Artist")) is False
    # Prefix sibling: /library-other is NOT inside /library (string-prefix bug guard).
    assert is_in_library_source(lib_dir, str(tmp_path / "library-other")) is False


def test_is_in_library_source_relative_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A relative source resolves against the cwd, mirroring what beets does.
    monkeypatch.chdir(tmp_path)
    lib_dir = os.fsencode(str(tmp_path / "library"))
    (tmp_path / "library").mkdir()
    assert is_in_library_source(lib_dir, "library") is True
    assert is_in_library_source(lib_dir, "elsewhere") is False


def test_is_in_library_source_symlink_alias(tmp_path: Path) -> None:
    # TrueNAS analogue: lib.directory is a symlink onto the real dataset, so a
    # source under the real dir reaches the same files through a different
    # string. realpath collapses the alias, so the guard still recognizes it.
    real_music = tmp_path / "real_music"
    (real_music / "Artist" / "Album").mkdir(parents=True)
    alias = tmp_path / "library"
    alias.symlink_to(real_music)
    lib_dir = os.fsencode(str(alias))
    # Source reached through the REAL path (what beets walks); lib via the alias.
    assert is_in_library_source(lib_dir, str(real_music / "Artist" / "Album")) is True
    # And the symmetric case: lib via the real path, source through the alias.
    lib_dir_real = os.fsencode(str(real_music))
    assert is_in_library_source(lib_dir_real, str(alias / "Artist" / "Album")) is True


def test_is_in_library_source_samefile_bind_analogue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bind mounts keep DISTINCT realpaths for two paths onto one dir, so the
    # realpath prefix check alone would miss them. We can't create a real bind
    # mount hermetically (needs root), so neutralize realpath (identity) to force
    # the lexical prefix check to fail, leaving the samefile fallback as the sole
    # decider. A symlink supplies the shared st_dev/st_ino the fallback detects.
    monkeypatch.setattr(os.path, "realpath", os.path.abspath)
    real_music = tmp_path / "real_music"
    (real_music / "Artist" / "Album").mkdir(parents=True)
    alias = tmp_path / "library"
    alias.symlink_to(real_music)
    lib_dir = os.fsencode(str(alias))
    # realpath is now identity: lexically /tmp/.../real_music/... is NOT under
    # /tmp/.../library, so only samefile (real_music IS library's target) catches it.
    assert is_in_library_source(lib_dir, str(real_music / "Artist" / "Album")) is True
    # A genuinely outside source still resolves to False through the fallback.
    outside = tmp_path / "downloads" / "Artist"
    outside.mkdir(parents=True)
    assert is_in_library_source(lib_dir, str(outside)) is False


def _guard_session(tmp_path: Path, source: Path) -> WebImportSession:
    """A real session over a real empty library, for guard tests."""
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    source.mkdir(parents=True, exist_ok=True)
    return WebImportSession(
        lib,
        None,
        [os.fsencode(str(source))],
        None,
        ImportBridge(),
        None,
    )


def test_worker_forces_move_for_in_library_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, bool] = {}

    def fake_run(self: WebImportSession) -> None:
        seen["move"] = config["import"]["move"].get(bool)
        seen["copy"] = config["import"]["copy"].get(bool)

    monkeypatch.setattr(WebImportSession, "run", fake_run)
    # Source INSIDE the library; caller passes move=None (user config default,
    # which for the starter config is copy: yes) -> worker must force move.
    session = _guard_session(tmp_path, tmp_path / "music" / "incoming")
    config["import"]["move"] = False
    config["import"]["copy"] = True
    run_import_worker(session, move=None)
    assert seen == {"move": True, "copy": False}
    # Snapshot/restore still holds: globals are back to the pre-run values.
    assert config["import"]["move"].get(bool) is False
    assert config["import"]["copy"].get(bool) is True


def test_worker_refuses_explicit_copy_for_in_library_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(WebImportSession, "run", lambda self: None)
    session = _guard_session(tmp_path, tmp_path / "music" / "incoming")
    with pytest.raises(InLibraryCopyError):
        run_import_worker(session, move=False)


def test_worker_leaves_outside_source_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, bool] = {}

    def fake_run(self: WebImportSession) -> None:
        seen["move"] = config["import"]["move"].get(bool)

    monkeypatch.setattr(WebImportSession, "run", fake_run)
    session = _guard_session(tmp_path, tmp_path / "downloads" / "incoming")
    config["import"]["move"] = False
    config["import"]["copy"] = True
    run_import_worker(session, move=None)
    # Outside the library: move=None falls through to the user's config.
    assert seen == {"move": False}


def test_bridge_pause_event_round_trips() -> None:
    bridge = ImportBridge()
    assert bridge.pause_requested() is False
    bridge.request_pause()
    assert bridge.pause_requested() is True
    bridge.request_pause()  # idempotent
    assert bridge.pause_requested() is True


def test_bridge_known_skip_counter() -> None:
    bridge = ImportBridge()
    assert bridge.known_skips() == 0
    bridge.note_known_skip()
    bridge.note_known_skip()
    assert bridge.known_skips() == 2


def test_sweep_session_construction_implies_unattended(tmp_path: Path) -> None:
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    session = WebImportSession(
        lib,
        None,
        [os.fsencode(str(tmp_path / "in"))],
        None,
        ImportBridge(),
        None,
        unattended=False,
        sweep=True,
        bank_dir=tmp_path / "bank",
    )
    # A sweep is unattended by definition - the constructor ORs the flag in.
    assert session.unattended is True
    assert session.sweep is True
    assert session._bank_dir == tmp_path / "bank"


def test_default_construction_is_not_sweep(tmp_path: Path) -> None:
    session = _guard_session(tmp_path, tmp_path / "downloads" / "incoming")
    assert session.sweep is False
    assert session._bank_dir is None
    assert session.unattended is False


def test_pause_aborts_at_top_of_each_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    from beets.importer.session import ImportAbortError

    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)
    bridge.request_pause()
    # The pause is checked FIRST in every decision hook, so each raises beets'
    # native clean abort without parking, banking, or emitting anything.
    with pytest.raises(ImportAbortError):
        session.choose_match(task)
    with pytest.raises(ImportAbortError):
        session.choose_item(task)
    with pytest.raises(ImportAbortError):
        session.get_duplicate_action(task, [])
    assert bridge.pending_count() == 0
    assert bridge.drain_outcomes() == []


def test_already_imported_counts_known_skips() -> None:
    bridge = ImportBridge()
    session = _make_session(bridge)
    # __init__ is skipped: provide what beets' already_imported reads. A plain
    # dict stands in for the iconfig view (truthiness is all it uses); 2.12's
    # stricter ImportSession.config annotation needs the scoped ignore.
    session.config = {"incremental": True}  # type: ignore[assignment]  # fake config view
    session._is_resuming = {}
    session._history_dirs = {(b"/music/done",)}
    assert session.already_imported(b"/top", [b"/music/done"]) is True
    assert session.already_imported(b"/top", [b"/music/new"]) is False
    assert bridge.known_skips() == 1


class _SweepConfigSession:
    """Minimal session recording the sweep-relevant config seen during run()."""

    lib = None
    paths: ClassVar[list[bytes]] = []
    _replace_album_ids: ClassVar[set[int]] = set()
    _trash_dir = None

    def __init__(self) -> None:
        self.seen: dict[str, Any] = {}

    def run(self) -> None:
        self.seen = {
            "incremental": config["import"]["incremental"].get(bool),
            "resume": config["import"]["resume"].get(),
            "singletons": config["import"]["singletons"].get(bool),
        }


def test_sweep_forces_incremental_no_resume_no_singletons() -> None:
    config["import"]["incremental"] = False
    config["import"]["resume"] = "ask"  # beets' default
    config["import"]["singletons"] = True  # hostile user config
    s = _SweepConfigSession()
    run_import_worker(s, sweep=True)  # type: ignore[arg-type]  # minimal stand-in
    assert s.seen == {"incremental": True, "resume": False, "singletons": False}
    # Snapshot/restore: the globals are back to their pre-run values.
    assert config["import"]["incremental"].get(bool) is False
    assert config["import"]["resume"].get() == "ask"
    assert config["import"]["singletons"].get(bool) is True


def test_non_sweep_leaves_incremental_config_alone() -> None:
    config["import"]["incremental"] = True  # the user's own incremental setup
    config["import"]["resume"] = "ask"
    s = _SweepConfigSession()
    run_import_worker(s)  # type: ignore[arg-type]  # minimal stand-in
    assert s.seen["incremental"] is True  # untouched
    assert s.seen["resume"] == "ask"  # untouched


def test_sweep_config_restores_on_raise() -> None:
    config["import"]["incremental"] = False
    config["import"]["resume"] = "ask"
    config["import"]["singletons"] = False

    class _Boom(_SweepConfigSession):
        def run(self) -> None:
            raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        run_import_worker(_Boom(), sweep=True)  # type: ignore[arg-type]  # minimal stand-in
    assert config["import"]["incremental"].get(bool) is False
    assert config["import"]["resume"].get() == "ask"
    assert config["import"]["singletons"].get(bool) is False


def test_directive_session_construction_implies_unattended(tmp_path: Path) -> None:
    from app.models.bank import BankApplyDirective

    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    session = WebImportSession(
        lib,
        None,
        [os.fsencode(str(tmp_path / "in"))],
        None,
        ImportBridge(),
        None,
        directive=BankApplyDirective(action="asis"),
    )
    # An apply run is unattended by definition: a directive session can never
    # park (block on a human) even if a code path missed the directive branch.
    assert session.unattended is True
    assert session.sweep is False
    assert session._directive is not None


def test_directive_apply_selects_top_candidate_without_parking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.bank import BankApplyDirective

    # rec is MEDIUM: the as-built policy would park (attended) or SKIP
    # (unattended); the directive must override both and apply.
    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    session.unattended = True
    session._directive = BankApplyDirective(action="apply", search_id="rel-1")
    task = _make_task(match, monkeypatch, BeetsRec.medium)

    task.choose_match(session)

    assert task.choice_flag is Action.APPLY
    assert task.match is match
    assert bridge.pending_count() == 0  # never parked
    outcomes = bridge.drain_outcomes()
    assert [o.status for o in outcomes] == [AlbumOutcomeStatus.applied]


def test_directive_apply_gains_album_id_follow_up(monkeypatch: pytest.MonkeyPatch) -> None:
    # The applied outcome is stashed for the album-id follow-up exactly like a
    # strong auto-apply, so the bank row can learn its library album id.
    from app.models.bank import BankApplyDirective

    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    session.unattended = True
    session._directive = BankApplyDirective(action="apply", search_id="rel-1")
    task = _make_task(match, monkeypatch, BeetsRec.medium)
    task.choose_match(session)
    task.album = _AddedAlbum(13)  # beets' task.add ran (sequential pipeline)
    session._flush_album_ids()
    follow_ups = [o for o in bridge.drain_outcomes() if o.album_id is not None]
    assert [o.album_id for o in follow_ups] == [13]


def test_directive_asis_and_astracks(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.models.bank import BankApplyDirective

    match = _build_match(BeetsRec.medium)
    session = _make_session(ImportBridge())
    session.unattended = True
    session._directive = BankApplyDirective(action="asis")
    task = _make_task(match, monkeypatch, BeetsRec.medium)
    assert session.choose_match(task) is Action.ASIS

    session2 = _make_session(ImportBridge())
    session2.unattended = True
    session2._directive = BankApplyDirective(action="astracks")
    task2 = _make_task(match, monkeypatch, BeetsRec.medium)
    assert session2.choose_match(task2) is Action.TRACKS
    # TRACKS re-pipelines singletons through choose_item (beets routes
    # SingletonImportTask.choose_match -> session.choose_item): each imports
    # as-is. The chunk-1 SKIP would silently import nothing.
    assert session2.choose_item(task2) is Action.ASIS
    # Any other directive keeps the chunk-1 posture.
    assert session.choose_item(task) is Action.SKIP


def test_directive_apply_with_no_candidates_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pinned id no plugin resolves yields zero candidates (tag_album with
    # search_ids does NO text fallback): emit skipped + SKIP; the apply runner
    # turns that into a retryable failed row.
    from app.models.bank import BankApplyDirective

    def fake_tag_album(items: Any, search_ids: Any = None) -> tuple[str, str, Proposal]:
        return ("Artist", "Album", Proposal([], BeetsRec.none))

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)
    bridge = ImportBridge()
    session = _make_session(bridge)
    session.unattended = True
    session._directive = BankApplyDirective(action="apply", search_id="bad-id")
    task = ImportTask(
        toppath=None,
        paths=[b"/music/album"],
        items=[Item(artist="Artist", album="Album", title="X", track=1, length=10.0)],
    )
    task.lookup_candidates([])
    assert session.choose_match(task) is Action.SKIP
    outcomes = bridge.drain_outcomes()
    assert [o.status for o in outcomes] == [AlbumOutcomeStatus.skipped]


def _directive_dup_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[WebImportSession, ImportTask, Any]:
    """A directive-mode session + APPLY-chosen task + one real library duplicate."""
    from beets.library import Library as BeetsLibrary

    match = _build_match(BeetsRec.strong)
    session = _make_session(ImportBridge())
    session.unattended = True
    session._replace_album_ids = set()
    lib = BeetsLibrary(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
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
    task = _make_task(match, monkeypatch, BeetsRec.strong)
    return session, task, existing_album


def test_directive_duplicate_actions_map_like_attended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models.bank import BankApplyDirective
    from app.models.import_models import DuplicateAction

    # skip_new
    session, task, existing = _directive_dup_setup(tmp_path, monkeypatch)
    session._directive = BankApplyDirective(
        action="duplicate", duplicate_action=DuplicateAction.skip_new
    )
    assert session.get_duplicate_action(task, [existing]) is BeetsDuplicateAction.SKIP

    # merge
    session2, task2, existing2 = _directive_dup_setup(tmp_path, monkeypatch)
    session2._directive = BankApplyDirective(
        action="duplicate", duplicate_action=DuplicateAction.merge
    )
    assert session2.get_duplicate_action(task2, [existing2]) is BeetsDuplicateAction.MERGE

    # replace records the ids for the post-run Trash pass (KEEP, never hard-delete)
    session3, task3, existing3 = _directive_dup_setup(tmp_path, monkeypatch)
    session3._directive = BankApplyDirective(
        action="duplicate", duplicate_action=DuplicateAction.replace
    )
    assert session3.get_duplicate_action(task3, [existing3]) is BeetsDuplicateAction.KEEP
    assert session3._replace_album_ids == {int(existing3.id)}

    # keep_both imports alongside the existing copy
    session4, task4, existing4 = _directive_dup_setup(tmp_path, monkeypatch)
    session4._directive = BankApplyDirective(
        action="duplicate", duplicate_action=DuplicateAction.keep_both
    )
    assert session4.get_duplicate_action(task4, [existing4]) is BeetsDuplicateAction.KEEP


def test_directive_without_dup_action_skips_unanticipated_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An apply/asis/astracks row that turns out to duplicate a library album:
    # never auto-resolve - SKIP, emit the dup outcome (the runner reads it off
    # the feed and fails the row with re-decide guidance).
    from app.models.bank import BankApplyDirective

    session, task, existing = _directive_dup_setup(tmp_path, monkeypatch)
    session._directive = BankApplyDirective(action="apply", search_id="rel-1")
    assert session.get_duplicate_action(task, [existing]) is BeetsDuplicateAction.SKIP
    outcomes = session.bridge.drain_outcomes()
    assert any(o.status is AlbumOutcomeStatus.needs_dup_resolution for o in outcomes)


class _ApplyConfigSession(_SweepConfigSession):
    """Records the apply-relevant config (sweep trio + search_ids) during run()."""

    def run(self) -> None:
        self.seen = {
            "incremental": config["import"]["incremental"].get(bool),
            "resume": config["import"]["resume"].get(),
            "singletons": config["import"]["singletons"].get(bool),
            "search_ids": config["import"]["search_ids"].get(),
        }


def test_apply_directive_forces_nonincremental_and_pins_search_ids() -> None:
    from app.models.bank import BankApplyDirective

    # Hostile ambient config: the user's own incremental sweep setup. Banked
    # folders are in taghistory (the sweep SKIP-recorded them), so without an
    # explicit incremental=False the apply would silently skip its own folder.
    config["import"]["incremental"] = True
    config["import"]["resume"] = "ask"
    config["import"]["singletons"] = True
    config["import"]["search_ids"] = []
    s = _ApplyConfigSession()
    run_import_worker(
        s,  # type: ignore[arg-type]  # minimal stand-in
        directive=BankApplyDirective(action="apply", search_id="rel-123"),
    )
    assert s.seen == {
        "incremental": False,
        "resume": False,
        "singletons": False,
        "search_ids": ["rel-123"],
    }
    # Snapshot/restore: the globals are back to their pre-run values.
    assert config["import"]["incremental"].get(bool) is True
    assert config["import"]["resume"].get() == "ask"
    assert config["import"]["singletons"].get(bool) is True
    assert config["import"]["search_ids"].get() == []


def test_apply_directive_without_release_id_leaves_lookup_unpinned() -> None:
    from app.models.bank import BankApplyDirective

    config["import"]["search_ids"] = []
    s = _ApplyConfigSession()
    run_import_worker(s, directive=BankApplyDirective(action="asis"))  # type: ignore[arg-type]
    assert s.seen["search_ids"] == []  # unpinned: asis/dup/legacy rows
    assert s.seen["incremental"] is False  # the non-incremental forcing still applies


def test_apply_directive_restores_search_ids_on_raise() -> None:
    from app.models.bank import BankApplyDirective

    config["import"]["search_ids"] = ["user-pin"]

    class _Boom(_ApplyConfigSession):
        def run(self) -> None:
            raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        run_import_worker(
            _Boom(),  # type: ignore[arg-type]  # minimal stand-in
            directive=BankApplyDirective(action="apply", search_id="rel-1"),
        )
    assert config["import"]["search_ids"].get() == ["user-pin"]


def test_non_directive_run_never_touches_search_ids() -> None:
    config["import"]["search_ids"] = ["user-pin"]
    s = _ApplyConfigSession()
    run_import_worker(s)  # type: ignore[arg-type]  # minimal stand-in
    assert s.seen["search_ids"] == ["user-pin"]  # manual/inbox/sweep: untouched
    assert config["import"]["search_ids"].get() == ["user-pin"]


# ----- _task_folder: multi-disc bank-folder fix -----


def test_task_folder_is_the_common_parent_of_multidisc_paths() -> None:
    # A deemix multi-disc layout collapses to paths=[CD1, CD2, CD3] (the album
    # parent is excluded because its loose files defeat the nested collapse). The
    # bank folder must be the album dir, not CD1, or the apply re-imports CD1 only.
    session = _make_session(ImportBridge())
    session.paths = [b"/dl"]
    task = ImportTask(
        toppath=None,
        paths=[b"/dl/Album/CD1", b"/dl/Album/CD2", b"/dl/Album/CD3"],
        items=[],
    )
    assert session._task_folder(task) == "/dl/Album"


def test_task_folder_single_path_is_unchanged() -> None:
    # A normal one-folder album: common-parent of a single path is that path.
    session = _make_session(ImportBridge())
    session.paths = [b"/dl"]
    task = ImportTask(toppath=None, paths=[b"/dl/Album"], items=[])
    assert session._task_folder(task) == "/dl/Album"


def test_task_folder_empty_paths_is_blank() -> None:
    session = _make_session(ImportBridge())
    task = ImportTask(toppath=None, paths=[], items=[])
    assert session._task_folder(task) == ""


def test_task_folder_no_toppaths_falls_back_to_full_commonpath() -> None:
    # Degenerate session (no toppaths recorded): with nothing to scope by, the
    # folder is the plain common-parent of every path — the pre-fix behavior.
    session = _make_session(ImportBridge())
    session.paths = []
    task = ImportTask(toppath=None, paths=[b"/dl/Album/CD1", b"/dl/Album/CD2"], items=[])
    assert session._task_folder(task) == "/dl/Album"


def test_task_folder_merged_task_uses_source_folder_not_library_ancestor() -> None:
    # A MERGE decision makes beets build ImportTask(None, source_paths +
    # duplicate LIBRARY file paths). The naive common-parent of an inbox folder
    # and a library file is a bogus ancestor ("/"), which the feed then shows and
    # a Rescan would os.walk across the whole library mount. Scoping to the paths
    # under a session toppath recovers the real incoming source folder.
    session = _make_session(ImportBridge())
    session.paths = [b"/inbox"]
    task = ImportTask(
        toppath=None,
        paths=[b"/inbox/Album", b"/music/Artist/Album/01.flac", b"/music/Artist/Album/02.flac"],
        items=[],
    )
    assert session._task_folder(task) == "/inbox/Album"


def test_task_folder_merged_multidisc_source_stays_scoped() -> None:
    session = _make_session(ImportBridge())
    session.paths = [b"/inbox"]
    task = ImportTask(
        toppath=None,
        paths=[b"/inbox/Album/CD1", b"/inbox/Album/CD2", b"/music/Artist/Album/01.flac"],
        items=[],
    )
    assert session._task_folder(task) == "/inbox/Album"


def test_albums_in_dir_collapses_deemix_multidisc(tmp_path: Path) -> None:
    # Guards the beets behaviour the fix relies on: re-importing the album PARENT
    # (loose files + CD1/CD2/CD3) collapses the three discs into ONE album, so a
    # common-parent bank folder re-imports every disc.
    root = tmp_path / "Album"
    for cd in ("CD1", "CD2", "CD3"):
        leaf = root / cd
        leaf.mkdir(parents=True)
        for i in range(1, 3):
            (leaf / f"{i:02d} t.flac").write_bytes(b"\0")
    for extra in ("cover.jpg", "playlist.m3u8", "Thumbs.db"):  # deemix leftovers
        (root / extra).write_bytes(b"\0")

    albums = list(beets_tasks.albums_in_dir(os.fsencode(str(root))))
    collapsed = [
        paths
        for paths, _items in albums
        if {os.path.basename(os.fsdecode(p)) for p in paths} == {"CD1", "CD2", "CD3"}
    ]
    assert len(collapsed) == 1  # the three discs are ONE album


# ----- attended "as tracks": the singletons must land, not silently skip -----


def test_choose_item_skips_by_default_but_asis_when_astracks_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A singleton reaching choose_item (no directive) is set aside by default...
    match = _build_match(BeetsRec.medium)
    session = _make_session(ImportBridge())
    task = _make_task(match, monkeypatch, BeetsRec.medium)
    assert session.choose_item(task) is Action.SKIP
    # ...but once choose_match has armed the in-flight flag for an astracks'd
    # album, its re-pipelined singletons import ASIS (previously silent no-op).
    session._astracks_in_flight = True
    assert session.choose_item(task) is Action.ASIS


def test_attended_astracks_arms_the_in_flight_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)

    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    parked = bridge.get_parked(timeout=2.0)
    assert parked is not None
    bridge.push_choice(parked.album_index, ImportChoice(action=ImportAction.astracks))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)
    # The album's TRACKS choice re-pipelines singletons through choose_item; the
    # flag is what makes that hook answer ASIS instead of the default SKIP.
    assert task.choice_flag is Action.TRACKS
    assert session._astracks_in_flight is True


def test_next_choose_match_clears_the_in_flight_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # A prior album armed the flag; the next album task's choose_match closes the
    # singleton window (its files already re-pipelined) by resetting it to False.
    match = _build_match(BeetsRec.strong)
    session = _make_session(ImportBridge())
    session._astracks_in_flight = True
    task = _make_task(match, monkeypatch, BeetsRec.strong)

    result = session.choose_match(task)  # strong rec: auto-applies, never parks

    assert result is match
    assert session._astracks_in_flight is False


def test_attended_astracks_lands_the_singletons_full_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """E2E regression: an attended run decided "as tracks" must import the files.

    The album parks (medium rec), the user chooses astracks, and beets
    re-pipelines each file as a SingletonImportTask whose choose_match routes to
    ``choose_item``. Before the fix that hook answered SKIP for the attended
    path, so the run completed "cleanly" while the library gained ZERO items.
    The in-flight flag makes those singletons import ASIS instead.
    """
    from mediafile import MediaFile

    from tests.conftest import build_library

    sample = Path(__file__).parent / "fixtures" / "silent.flac"
    source = tmp_path / "downloads" / "okc"
    source.mkdir(parents=True)
    for i in range(1, 3):
        dst = source / f"{i:02d} Track {i}.flac"
        shutil.copyfile(sample, dst)
        mf = MediaFile(str(dst))
        mf.artist = "Radiohead"
        mf.albumartist = "Radiohead"
        mf.album = "OK Computer"
        mf.title = f"Airbag {i}"
        mf.track = i
        mf.save()

    music = tmp_path / "music"
    music.mkdir()
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def fake_tag_album(items: Any, search_ids: Any = None) -> tuple[str, str, Proposal]:
        # A canned MEDIUM-rec match built FROM the passed items so the album parks.
        item_list = list(items)
        tracks = [
            TrackInfo(title=f"Airbag {i}", track_id=f"t{i}", index=i, length=1.0)
            for i in range(1, len(item_list) + 1)
        ]
        info = AlbumInfo(
            tracks=tracks,
            album="OK Computer",
            artist="Radiohead",
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
        return ("Radiohead", "OK Computer", Proposal([match], BeetsRec.medium))

    def fake_tag_item(item: Any, search_ids: Any = None) -> Proposal:
        return Proposal([], BeetsRec.none)

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)
    monkeypatch.setattr(beets_tasks, "tag_item", fake_tag_item)

    bridge = ImportBridge()
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

    def worker() -> None:
        try:
            run_import_worker(session, move=None, sweep=False, directive=None)
        except Exception as exc:
            errors.append(f"{exc.__class__.__name__}: {exc}")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    parked = bridge.get_parked(timeout=10.0)
    assert parked is not None
    bridge.push_choice(parked.album_index, ImportChoice(action=ImportAction.astracks))
    t.join(timeout=20.0)
    assert not t.is_alive(), "import worker hung"
    assert errors == [], f"import worker errored: {errors}"

    # As-tracks lands the two files as singletons (no album row).
    assert len(list(lib.items())) == 2
    assert len(list(lib.albums())) == 0


def test_rescan_choice_rereads_swaps_items_and_reparks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.beets.import_session as session_mod

    match = _build_match(BeetsRec.medium)
    other = _build_other_match()
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)
    session.paths = [b"/music"]  # a real session always has toppaths; /music/album is under it
    original_items = task.items
    new_items = list(other.mapping.keys())  # the fresh read (one file deleted)
    monkeypatch.setattr(session_mod, "_read_items", lambda p: new_items)
    monkeypatch.setattr(
        session_mod,
        "lookup_items",
        lambda items, s: ("Radiohead", "Amnesiac", [other], BeetsRec.strong),
    )

    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    first = bridge.get_parked(timeout=2.0)
    assert first is not None
    bridge.push_choice(first.album_index, ImportChoice(action=ImportAction.rescan))

    second = bridge.get_parked(timeout=2.0)
    assert second is not None
    assert second.candidate.search_revision == 1
    assert second.candidate.search_feedback is None
    assert second.candidate.album_after.album == "Amnesiac"
    assert task.items is new_items  # the swap: asis/astracks now see the fresh read
    assert task.cur_album == "Amnesiac"

    bridge.push_choice(second.album_index, ImportChoice(action=ImportAction.apply))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)
    assert task.match is other  # apply selects from the NEW candidate list
    assert original_items is not task.items


def test_rescan_with_no_audio_left_keeps_state_and_sets_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.beets.import_session as session_mod

    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)
    session.paths = [b"/music"]  # a real session always has toppaths; /music/album is under it
    original_items = task.items
    monkeypatch.setattr(session_mod, "_read_items", lambda p: [])

    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    first = bridge.get_parked(timeout=2.0)
    assert first is not None
    bridge.push_choice(first.album_index, ImportChoice(action=ImportAction.rescan))

    second = bridge.get_parked(timeout=2.0)
    assert second is not None
    assert second.candidate.search_revision == 1
    assert second.candidate.search_feedback == (
        "No audio files remain in the folder. Skip or Abort."
    )
    assert task.items is original_items
    assert second.candidate.album_after.album == "OK Computer"

    bridge.push_choice(second.album_index, ImportChoice(action=ImportAction.skip))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)


def test_rescan_with_no_candidates_never_half_swaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.beets.import_session as session_mod

    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)
    session.paths = [b"/music"]  # a real session always has toppaths; /music/album is under it
    original_items = task.items
    monkeypatch.setattr(session_mod, "_read_items", lambda p: [Item(title="x")])
    monkeypatch.setattr(
        session_mod, "lookup_items", lambda items, s: (None, None, [], BeetsRec.none)
    )

    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    first = bridge.get_parked(timeout=2.0)
    assert first is not None
    bridge.push_choice(first.album_index, ImportChoice(action=ImportAction.rescan))

    second = bridge.get_parked(timeout=2.0)
    assert second is not None
    assert second.candidate.search_feedback == (
        "No release matched the rescanned folder; showing the album as originally scanned."
    )
    # NO half-swap: items untouched, previous candidates still apply-able.
    assert task.items is original_items
    assert second.candidate.album_after.album == "OK Computer"

    bridge.push_choice(second.album_index, ImportChoice(action=ImportAction.apply))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)
    assert task.match is match  # the original candidate applied


def test_rescan_refused_when_folder_escapes_the_session_toppaths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Defense-in-depth for the merge shape: if the parked folder is not under a
    # session toppath (a task whose paths are all library files), a Rescan must
    # NOT os.walk it — that ancestor can be the whole library mount. The guard
    # refuses with feedback and never reaches _read_items.
    import app.beets.import_session as session_mod

    match = _build_match(BeetsRec.medium)
    bridge = ImportBridge()
    session = _make_session(bridge)
    session.paths = [b"/inbox"]  # the real import source
    task = _make_task(match, monkeypatch, BeetsRec.medium)
    # A merged-away shape: every path is a LIBRARY file, none under /inbox, so
    # _task_folder falls back to "/music/Artist/Album" — outside the toppath.
    task.paths = [b"/music/Artist/Album/01.flac", b"/music/Artist/Album/02.flac"]
    original_items = task.items

    def _boom(_p: object) -> list[Item]:
        raise AssertionError("rescan must not read a folder outside the session toppaths")

    monkeypatch.setattr(session_mod, "_read_items", _boom)

    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    first = bridge.get_parked(timeout=2.0)
    assert first is not None
    bridge.push_choice(first.album_index, ImportChoice(action=ImportAction.rescan))

    second = bridge.get_parked(timeout=2.0)
    assert second is not None
    assert second.candidate.search_feedback == "Rescan isn't available for this album."
    assert task.items is original_items  # nothing swapped
    assert second.candidate.search_revision == 1

    bridge.push_choice(second.album_index, ImportChoice(action=ImportAction.skip))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)
