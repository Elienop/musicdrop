import logging
import threading
from collections.abc import Iterator
from typing import Any

import beets.importer.tasks as beets_tasks
import pytest
from beets import config
from beets.autotag.distance import distance
from beets.autotag.hooks import AlbumInfo, AlbumMatch, TrackInfo
from beets.autotag.match import Proposal, assign_items
from beets.autotag.match import Recommendation as BeetsRec
from beets.importer.tasks import Action, ImportTask
from beets.library import Item

from app.beets.import_session import ImportBridge, WebImportSession
from app.models.import_models import (
    AlbumOutcome,
    AlbumOutcomeStatus,
    ImportAction,
    ImportChoice,
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
