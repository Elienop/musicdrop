import contextlib
import logging
import os
import shutil
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, ClassVar, cast

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

from app.beets.import_mapping import embedded_art
from app.beets.import_session import (
    ImportBridge,
    InLibraryCopyError,
    WebImportSession,
    is_in_library_source,
    run_import_worker,
)
from app.beets.library import _require_id
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


def _build_third_match() -> AlbumMatch:
    """A third distinct release, used as a search re-lookup's single result."""
    items = [Item(artist="Radiohead", album="Kid A", title="Everything", track=1, length=180.0)]
    tracks = [TrackInfo(title="Everything", track_id="t5", index=1, length=180.0)]
    info = AlbumInfo(
        tracks=tracks,
        album="Kid A",
        artist="Radiohead",
        album_id="a5",
        data_source="MusicBrainz",
        data_url="https://mb/a5",
        year=2000,
        va=False,
    )
    pairs, extra_i, extra_t = assign_items(items, info.tracks)
    return AlbumMatch(distance(items, info, pairs), info, dict(pairs), extra_i, extra_t)


def _build_fourth_match() -> AlbumMatch:
    """A fourth distinct release, paired with the third so a search can REPLACE
    the candidate list with an equal-length one (the stale-revision case)."""
    items = [Item(artist="Radiohead", album="In Rainbows", title="Nude", track=1, length=260.0)]
    tracks = [TrackInfo(title="Nude", track_id="t7", index=1, length=260.0)]
    info = AlbumInfo(
        tracks=tracks,
        album="In Rainbows",
        artist="Radiohead",
        album_id="a7",
        data_source="MusicBrainz",
        data_url="https://mb/a7",
        year=2007,
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


class _BindOnlyLib:
    """The bare slice of ``Library`` that ``run_import_worker`` needs to bind.

    The worker wraps its whole body in ``session.lib.music_dir_context()`` so
    beets stores item paths music-dir-relative. The stand-in sessions below
    assert on beets *config* flags and never write a row, so a nullcontext is
    the honest fake: it satisfies the call without pretending to bind beets'
    ContextVar. The write representation itself is pinned against a REAL
    library in ``tests/test_import_path_representation.py``.
    """

    def music_dir_context(self) -> AbstractContextManager[None]:
        return contextlib.nullcontext()


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
    # __init__ is skipped, so seed the landed-id set _flush_album_ids fills and
    # the banked-replace seed gates on. A __new__ fake breaks the moment
    # production reads an attribute it never set, so this is not optional.
    session._landed_album_ids = set()
    # __init__ is skipped, so default the astracks-in-flight flag choose_item
    # now reads (armed by choose_match when a park is decided "as tracks").
    session._astracks_in_flight = False
    # __init__ is skipped, so the in-library guard's session.paths read has a
    # value; empty means the guard no-ops (these tests drive run() directly).
    session.paths = []
    # __init__ is skipped, so give run_import_worker something to bind its
    # music-dir context on (see _BindOnlyLib).
    session.lib = _BindOnlyLib()  # type: ignore[assignment]  # bind-only stand-in, not a Library
    return session


def _make_task(match: AlbumMatch, monkeypatch: pytest.MonkeyPatch, rec: BeetsRec) -> ImportTask:
    _patch_tag_album(monkeypatch, match, rec)
    task = ImportTask(toppath=None, paths=[b"/music/album"], items=list(match.mapping.keys()))
    task.lookup_candidates([])  # populates cur_artist/cur_album/candidates/rec
    return task


def _make_task_multi(
    matches: list[AlbumMatch], monkeypatch: pytest.MonkeyPatch, rec: BeetsRec
) -> ImportTask:
    """Like _make_task but the first scan offers MULTIPLE candidates, so the
    client can render a long list a later search can shrink out from under it."""

    def fake_tag_album(items: Any, search_ids: Any = None) -> tuple[str, str, Proposal]:
        return ("Radiohead", "OK Computer", Proposal(list(matches), rec))

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)
    task = ImportTask(toppath=None, paths=[b"/music/album"], items=list(matches[0].mapping.keys()))
    task.lookup_candidates([])
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


def test_search_success_swaps_task_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful search re-park must swap task.candidates itself.

    Pins the `task.candidates = candidates` assignment in `_park_search`: a
    mutation review proved nothing else in the suite forces the task object to
    hold the NEW candidate list after a successful relookup.
    """
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

    bridge.push_choice(
        first.album_index,
        ImportChoice(action=ImportAction.search, search=ImportSearch(release_id="a9")),
    )

    second = bridge.get_parked(timeout=2.0)
    assert second is not None
    assert second.candidate.search_revision == 1
    assert second.candidate.album_after.album == "Amnesiac"  # the re-looked-up release

    # The TASK object itself must now carry the relookup's NEW candidate list,
    # not the original lookup's one — that swap happens inside `_park_search`.
    assert task.candidates == [other]

    bridge.push_choice(second.album_index, ImportChoice(action=ImportAction.apply))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)


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
        lib = _BindOnlyLib()
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


def test_out_of_range_apply_reparks_instead_of_importing_top(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stale apply for an index past the current list must NOT silently import
    # candidates[0] (a different release than the user chose) — it re-parks with
    # feedback so the user re-confirms; a subsequent in-range apply then resolves.
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

    # It re-parks (does NOT resolve) with the stale-apply feedback + bumped revision.
    reparked = bridge.get_parked(timeout=2.0)
    assert reparked is not None
    assert reparked.album_index == parked.album_index
    assert reparked.candidate.search_revision == 1
    assert reparked.candidate.search_feedback is not None
    assert "no longer in the list" in reparked.candidate.search_feedback
    assert not done.is_set()  # the worker did not import candidates[0]

    # A valid apply now resolves normally to the (only) candidate.
    bridge.push_choice(reparked.album_index, ImportChoice(action=ImportAction.apply))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)
    assert task.choice_flag is Action.APPLY
    assert task.match is match


def test_stale_apply_after_search_shrinks_list_reparks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The finding's exact race: a search re-parks with a SHORTER list, then the
    # client (still showing the old, longer list) submits an apply for an index
    # that is out of range for the new list. It must re-park, not import blindly.
    import app.beets.import_session as session_mod

    match = _build_match(BeetsRec.medium)
    other = _build_other_match()
    single = _build_third_match()
    bridge = ImportBridge()
    session = _make_session(bridge)
    # First scan offers TWO candidates (index 1 is valid for now).
    task = _make_task_multi([match, other], monkeypatch, BeetsRec.medium)
    # A search shrinks the list to a SINGLE distinct release.
    monkeypatch.setattr(session_mod, "relookup", lambda t, s: ([single], BeetsRec.strong))

    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    first = bridge.get_parked(timeout=2.0)
    assert first is not None
    assert len(first.candidate.options) == 2  # client renders the long list

    bridge.push_choice(
        first.album_index,
        ImportChoice(action=ImportAction.search, search=ImportSearch(release_id="a5")),
    )
    second = bridge.get_parked(timeout=2.0)
    assert second is not None
    assert second.candidate.search_revision == 1
    assert second.candidate.album_after.album == "Kid A"  # shrank to the search result

    # Stale apply: index 1 was valid for the OLD 2-list, out of range for the new 1-list.
    bridge.push_choice(
        second.album_index, ImportChoice(action=ImportAction.apply, candidate_index=1)
    )
    third = bridge.get_parked(timeout=2.0)
    assert third is not None  # re-parked rather than resolving
    assert third.candidate.search_revision == 2
    assert third.candidate.search_feedback is not None
    assert "no longer in the list" in third.candidate.search_feedback
    assert not done.is_set()

    # An in-range apply resolves to the correct (search-result) release, not match/other.
    bridge.push_choice(
        third.album_index, ImportChoice(action=ImportAction.apply, candidate_index=0)
    )
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)
    assert task.match is single


def test_stale_revision_apply_after_equal_length_search_reparks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The M12b residual: a search REPLACES the list with an EQUAL-LENGTH one, so
    # the stale client's index is still in range — only the echoed
    # search_revision can reveal the submit predates the search. It must
    # re-park, not silently import a release the user never chose.
    import app.beets.import_session as session_mod

    match = _build_match(BeetsRec.medium)
    other = _build_other_match()
    x = _build_third_match()
    y = _build_fourth_match()
    bridge = ImportBridge()
    session = _make_session(bridge)
    # First scan offers TWO candidates; the search swaps in TWO DIFFERENT ones.
    task = _make_task_multi([match, other], monkeypatch, BeetsRec.medium)
    monkeypatch.setattr(session_mod, "relookup", lambda t, s: ([x, y], BeetsRec.medium))

    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    first = bridge.get_parked(timeout=2.0)
    assert first is not None
    assert first.candidate.search_revision == 0
    assert len(first.candidate.options) == 2

    bridge.push_choice(
        first.album_index,
        ImportChoice(action=ImportAction.search, search=ImportSearch(release_id="a5")),
    )
    second = bridge.get_parked(timeout=2.0)
    assert second is not None
    assert second.candidate.search_revision == 1
    assert second.candidate.album_after.album == "Kid A"  # the replaced list's top
    assert len(second.candidate.options) == 2  # SAME length — index 1 looks valid

    # Stale apply: index 1 is IN RANGE for the new list, but the echoed revision
    # (0) says the client was still rendering the OLD list. Re-park, don't
    # import Y (a release the user never saw, let alone chose).
    bridge.push_choice(
        second.album_index,
        ImportChoice(action=ImportAction.apply, candidate_index=1, search_revision=0),
    )
    third = bridge.get_parked(timeout=2.0)
    assert third is not None  # re-parked rather than resolving
    assert third.candidate.search_revision == 2
    assert third.candidate.search_feedback is not None
    assert "no longer in the list" in third.candidate.search_feedback
    assert not done.is_set()

    # An apply echoing the CURRENT revision resolves to the intended release.
    bridge.push_choice(
        third.album_index,
        ImportChoice(action=ImportAction.apply, candidate_index=0, search_revision=2),
    )
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)
    assert task.match is x


def test_matching_revision_apply_resolves_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Guard: an apply echoing the CURRENT revision resolves at once (no re-park).
    match = _build_match(BeetsRec.medium)
    other = _build_other_match()
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task_multi([match, other], monkeypatch, BeetsRec.medium)

    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    parked = bridge.get_parked(timeout=2.0)
    assert parked is not None
    assert parked.candidate.search_revision == 0
    bridge.push_choice(
        parked.album_index,
        ImportChoice(action=ImportAction.apply, candidate_index=1, search_revision=0),
    )
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)
    assert task.choice_flag is Action.APPLY
    candidates = task.candidates
    assert candidates is not None
    assert task.match is candidates[1]


def test_none_revision_apply_keeps_legacy_length_only_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A legacy/simple client that never echoes a revision (None) degrades to the
    # length-only guard: an in-range apply resolves even though a search bumped
    # the revision out from under it.
    import app.beets.import_session as session_mod

    match = _build_match(BeetsRec.medium)
    other = _build_other_match()
    x = _build_third_match()
    y = _build_fourth_match()
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task_multi([match, other], monkeypatch, BeetsRec.medium)
    monkeypatch.setattr(session_mod, "relookup", lambda t, s: ([x, y], BeetsRec.medium))

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
        ImportChoice(action=ImportAction.search, search=ImportSearch(release_id="a5")),
    )
    second = bridge.get_parked(timeout=2.0)
    assert second is not None
    assert second.candidate.search_revision == 1

    # No echoed revision: the in-range apply resolves against the CURRENT list.
    bridge.push_choice(
        second.album_index, ImportChoice(action=ImportAction.apply, candidate_index=0)
    )
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)
    assert task.choice_flag is Action.APPLY
    assert task.match is x


def test_stale_revision_on_skip_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Non-apply actions are list-independent decisions: a stale echoed revision
    # must never re-park them — skip resolves regardless.
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
    assert parked.candidate.search_revision == 0
    bridge.push_choice(
        parked.album_index, ImportChoice(action=ImportAction.skip, search_revision=99)
    )
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)
    assert task.choice_flag is Action.SKIP
    assert task.skip is True


def test_in_range_explicit_index_apply_resolves_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Guard: a NORMAL in-range explicit index still resolves at once (no re-park).
    match = _build_match(BeetsRec.medium)
    other = _build_other_match()
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task_multi([match, other], monkeypatch, BeetsRec.medium)

    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    parked = bridge.get_parked(timeout=2.0)
    assert parked is not None
    bridge.push_choice(
        parked.album_index, ImportChoice(action=ImportAction.apply, candidate_index=1)
    )
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)
    assert task.choice_flag is Action.APPLY
    candidates = task.candidates
    assert candidates is not None
    assert task.match is candidates[1]  # the explicitly chosen in-range candidate


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
    decision = DuplicateDecision(action=DuplicateAction.skip_new)
    with pytest.raises(KeyError):
        bridge.push_duplicate_decision(99, decision)


def test_run_import_worker_forces_duplicate_action_ask() -> None:
    """The worker must force import.duplicate_action=ask so the hook always fires."""
    from app.beets.import_session import run_import_worker

    config["import"]["duplicate_action"] = "keep"  # user config says keep-both
    seen: dict[str, Any] = {}

    class FakeSession:
        lib = _BindOnlyLib()
        paths: ClassVar[list[bytes]] = []
        _replace_album_ids: ClassVar[set[int]] = set()
        _trash_dir = None

        def run(self) -> None:
            seen["dup_action"] = config["import"]["duplicate_action"].get()

    run_import_worker(FakeSession())  # type: ignore[arg-type]  # minimal stand-in
    assert seen["dup_action"] == "ask"


def test_run_import_worker_forces_autotag_on_and_restores_it() -> None:
    """The worker must force import.autotag=yes so MusicDrop's hooks fire at all.

    With autotag off, beets replaces the lookup_candidates + user_query stages
    with import_asis (session.py run()), and user_query is the ONLY stage that
    calls choose_match — so no outcome is ever emitted, no bank row written and
    no album id reported, while beets imports the files for real. Same bug class
    as the singletons forcing next to it, but total.
    """
    from app.beets.import_session import run_import_worker

    config["import"]["autotag"] = False  # hostile user config
    seen: dict[str, Any] = {}

    class FakeSession:
        lib = _BindOnlyLib()
        paths: ClassVar[list[bytes]] = []
        _replace_album_ids: ClassVar[set[int]] = set()
        _trash_dir = None

        def run(self) -> None:
            seen["autotag"] = config["import"]["autotag"].get(bool)

    run_import_worker(FakeSession())  # type: ignore[arg-type]  # minimal stand-in
    assert seen["autotag"] is True
    # Snapshot/restore: the user's own value is back after the run.
    assert config["import"]["autotag"].get(bool) is False


def test_run_import_worker_trashes_replace_ids_after_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Recorded Replace ids are moved to Trash AFTER run() returns, by id.

    The fake library carries a ``directory`` and a ``path`` because the post-run
    pass re-checks the store layout before it moves anything: the Trash pair was
    resolved when the registry was handed the library, and an import can run
    hours later. The four paths here are siblings under ``tmp_path``, which is a
    layout the check accepts — ``settings.beets_dir`` is pinned alongside them so
    the fixture does not depend on where the session's BEETSDIR happens to be.
    """

    import app.beets.import_session as session_mod
    from app.beets.import_session import run_import_worker

    trashed: list[int] = []

    def fake_trash(lib: Any, album: Any, *, trash_dir: Path, origins_dir: Path) -> str:
        trashed.append(int(album.id))
        return str(trash_dir)

    monkeypatch.setattr(session_mod, "trash_album", fake_trash)
    monkeypatch.setattr("app.config.settings.beets_dir", str(tmp_path / "beets"))

    class _Album:
        def __init__(self, album_id: int) -> None:
            self.id = album_id

        def items(self) -> list[Any]:
            # Read by the post-run pass BEFORE trashing, to seed the `.m3u8`
            # re-export with the ids this Replace drops. Empty is honest for a
            # fake whose only claim is "trashed by id, after run()".
            return []

    class _Lib(_BindOnlyLib):
        directory = os.fsencode(str(tmp_path / "music"))
        path = os.fsencode(str(tmp_path / "beets" / "library.db"))

        def get_album(self, album_id: int) -> Any:
            return _Album(album_id)

        def transaction(self) -> Any:
            return contextlib.nullcontext()

    class FakeSession:
        lib = _Lib()
        paths: ClassVar[list[bytes]] = []
        _replace_album_ids: ClassVar[set[int]] = {11, 22}
        _trash_dir = tmp_path / "trash"
        # Wired as a PAIR with _trash_dir: the post-run pass skips unless both
        # are set, so a fake with only one silently stops trashing.
        _trash_origins_dir = tmp_path / "trash-origins"
        # Unwired playlist store -> the post-trash `.m3u8` re-export is skipped
        # (it is pinned in tests/test_playlist_reexport_movers.py instead).
        _playlists_dir = None

        def run(self) -> None:
            # The new albums are imported during run(); trashing happens after.
            assert trashed == []

    run_import_worker(FakeSession())  # type: ignore[arg-type]
    assert sorted(trashed) == [11, 22]


class _ScopedMoveSession:
    """Minimal session that records config['import']['move'] seen during run()."""

    lib = _BindOnlyLib()
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

    session = _Boom()  # minimal stand-in
    with pytest.raises(RuntimeError):
        run_import_worker(session, move=True)  # type: ignore[arg-type]
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
    task1.album = cast(Album, _AddedAlbum(42))

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
        task.album = cast(Album, _AddedAlbum(7))

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
        task.album = cast(Album, _AddedAlbum(9))

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

    lib = _BindOnlyLib()
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

    session = _Boom()  # minimal stand-in
    with pytest.raises(RuntimeError):
        run_import_worker(session, sweep=True)  # type: ignore[arg-type]
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
    task.album = cast(Album, _AddedAlbum(13))  # beets' task.add ran (sequential pipeline)
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


# ----- the banked-replace seed (enforcing a replace beets never re-detected) -----


def _existing_album_model(
    album_id: int, *, artist: str = "Radiohead", album: str = "OK Computer"
) -> Any:
    from app.models.import_models import ExistingAlbum

    return ExistingAlbum(
        album_id=album_id,
        album_artist=artist,
        album=album,
        year=None,
        track_count=1,
        format=None,
        bitrate_kbps=None,
        folder="/lib/x",
    )


def _replace_seed_setup(tmp_path: Path) -> tuple[WebImportSession, int]:
    """A directive-mode session over a REAL library holding one album.

    Deliberately no ImportTask and no hook call: these tests are about the case
    where beets' duplicate hook NEVER FIRES, so driving it would test the
    opposite thing.
    """
    session = _make_session(ImportBridge())
    session.unattended = True
    session._replace_album_ids = set()
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    session.lib = lib
    album = lib.add_album(
        [Item(albumartist="Radiohead", album="OK Computer", title="Airbag", track=1)]
    )
    album.store()
    return session, _require_id(album.id)


def _replace_directive(*existing: Any) -> Any:
    from app.models.bank import BankApplyDirective
    from app.models.import_models import DuplicateAction

    return BankApplyDirective(
        action="duplicate",
        duplicate_action=DuplicateAction.replace,
        replace_existing=list(existing),
    )


def test_banked_replace_seeds_the_trash_set_when_the_hook_never_fired(tmp_path: Path) -> None:
    """THE enforcement: beets' name-keyed re-detection missed, so
    ``_beets_dup_action`` never recorded anything — the stored id from the
    banked prompt is what puts the old copy in the post-run Trash set."""
    session, existing_id = _replace_seed_setup(tmp_path)
    session._directive = _replace_directive(_existing_album_model(existing_id))
    session._landed_album_ids = {existing_id + 500}  # the new album landed
    session._seed_replace_from_directive()
    assert session._replace_album_ids == {existing_id}


def test_banked_replace_seed_skips_an_identity_mismatch(tmp_path: Path) -> None:
    """The id-REUSE guard: beets ids are reused SQLite rowids, so the stored id
    can now name a different album. Trashing it would destroy something the
    user never decided about — it is skipped (and warned about) instead."""
    session, existing_id = _replace_seed_setup(tmp_path)
    session._directive = _replace_directive(
        _existing_album_model(existing_id, artist="Someone", album="Else")
    )
    session._landed_album_ids = {existing_id + 500}
    session._seed_replace_from_directive()
    assert session._replace_album_ids == set()


def test_banked_replace_seed_is_gated_on_something_landing(tmp_path: Path) -> None:
    """Nothing landed -> nothing is trashed. A pinned lookup that resolves
    nothing SKIPs while run() still returns normally and the trash pass still
    executes; an ungated seed would Trash the user's ONLY copy."""
    session, existing_id = _replace_seed_setup(tmp_path)
    session._directive = _replace_directive(_existing_album_model(existing_id))
    session._landed_album_ids = set()
    session._seed_replace_from_directive()
    assert session._replace_album_ids == set()


def test_banked_replace_seed_never_trashes_the_album_it_just_imported(tmp_path: Path) -> None:
    """If the old copy was deleted outside the app, the import can be handed
    its exact rowid — and being the same album, it passes the identity check
    too. Excluding this run's landed ids is what stops us trashing it."""
    session, existing_id = _replace_seed_setup(tmp_path)
    session._directive = _replace_directive(_existing_album_model(existing_id))
    session._landed_album_ids = {existing_id}
    session._seed_replace_from_directive()
    assert session._replace_album_ids == set()


def test_banked_replace_seed_ignores_a_non_replace_resolution(tmp_path: Path) -> None:
    """Only ``replace`` may touch a library album this run did not import. The
    directive never carries entries for the other three, and if one ever did,
    the seed must still refuse."""
    from app.models.bank import BankApplyDirective
    from app.models.import_models import DuplicateAction

    session, existing_id = _replace_seed_setup(tmp_path)
    session._directive = BankApplyDirective(
        action="duplicate",
        duplicate_action=DuplicateAction.skip_new,
        replace_existing=[_existing_album_model(existing_id)],
    )
    session._landed_album_ids = {existing_id + 500}
    session._seed_replace_from_directive()
    assert session._replace_album_ids == set()


def test_flush_album_ids_records_what_landed(tmp_path: Path) -> None:
    """``_landed_album_ids`` is the seed's gate, and this is the only thing
    that fills it: an id follow-up flushed for a task beets actually added."""
    session, _ = _replace_seed_setup(tmp_path)
    outcome = AlbumOutcome(
        album_index=0,
        folder="/x",
        artist="A",
        album="B",
        recommendation=Recommendation.strong,
        confidence=99.0,
        status=AlbumOutcomeStatus.applied,
    )

    class _AddedTask:
        album = type("_Added", (), {"id": 404})()

    session._await_album_id = [(outcome, cast(ImportTask, _AddedTask()))]
    session._flush_album_ids()
    assert session._landed_album_ids == {404}


def test_run_seeds_the_replace_set_after_the_flush(tmp_path: Path, monkeypatch: Any) -> None:
    """Order matters: the seed reads what the flush recorded, so run() must
    call it AFTER _flush_album_ids, not before."""
    from beets.importer.session import ImportSession

    session, existing_id = _replace_seed_setup(tmp_path)
    session._directive = _replace_directive(_existing_album_model(existing_id))

    class _AddedTask:
        album = type("_Added", (), {"id": existing_id + 500})()

    outcome = AlbumOutcome(
        album_index=0,
        folder="/x",
        artist="A",
        album="B",
        recommendation=Recommendation.strong,
        confidence=99.0,
        status=AlbumOutcomeStatus.applied,
    )
    session._await_album_id = [(outcome, cast(ImportTask, _AddedTask()))]
    monkeypatch.setattr(ImportSession, "run", lambda self: None)
    session.run()
    assert session._replace_album_ids == {existing_id}


def test_worker_trashes_a_seeded_copy_end_to_end(tmp_path: Path, monkeypatch: Any) -> None:
    """The whole post-run path: seed -> _trash_replaced_albums, reusing the
    SAME pass the hook-recorded ids already go through (it dedupes, skips
    missing albums and re-exports playlists) rather than forking a second."""
    from beets.importer.session import ImportSession

    import app.beets.import_session as session_mod

    trashed: list[int] = []

    def fake_trash(lib: Any, album: Any, *, trash_dir: Path, origins_dir: Path) -> str:
        trashed.append(int(album.id))
        return str(trash_dir)

    monkeypatch.setattr(session_mod, "trash_album", fake_trash)
    monkeypatch.setattr(ImportSession, "run", lambda self: None)

    session, existing_id = _replace_seed_setup(tmp_path)
    session._trash_dir = tmp_path / "trash"
    session._trash_origins_dir = tmp_path / "trash-origins"
    session._playlists_dir = None
    session._directive = _replace_directive(_existing_album_model(existing_id))
    session._landed_album_ids = {existing_id + 500}
    run_import_worker(session)
    assert trashed == [existing_id]


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
    # Unpinned: asis/astracks carry no release, and so do LEGACY rows (banked
    # before the release id was stored, or from a task that had no match).
    assert s.seen["search_ids"] == []
    assert s.seen["incremental"] is False  # the non-incremental forcing still applies


def test_apply_directive_restores_search_ids_on_raise() -> None:
    from app.models.bank import BankApplyDirective

    config["import"]["search_ids"] = ["user-pin"]

    class _Boom(_ApplyConfigSession):
        def run(self) -> None:
            raise RuntimeError("x")

    session = _Boom()  # minimal stand-in
    directive = BankApplyDirective(action="apply", search_id="rel-1")
    with pytest.raises(RuntimeError):
        run_import_worker(session, directive=directive)  # type: ignore[arg-type]
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


def test_successful_rescan_redetects_embedded_art(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pins the art re-detection tail of `_park_rescan`: on a successful rescan
    # art_source + has_current_art are recomputed from the FRESH items (the
    # rescan's whole point is that the user edited the folder, so the old file
    # may have carried the cover and the new one may not — or vice versa).
    # A mutation review proved this tail was unpinned. The re-parked
    # candidate's has_current_art AND the art_source the bridge records for the
    # re-park both have to reflect the new items.
    import app.beets.import_session as session_mod

    ORIG_PATH = "/music/album/orig.flac"  # original scan's first file: no embedded art
    NEW_PATH = "/music/album/new.flac"  # rescanned first file: HAS embedded art

    def fake_embedded_art(path: str) -> tuple[bytes, str] | None:
        return (b"jpeg-bytes", "image/jpeg") if path == NEW_PATH else None

    match = _build_match(BeetsRec.medium)
    other = _build_other_match()
    bridge = ImportBridge()
    session = _make_session(bridge)
    task = _make_task(match, monkeypatch, BeetsRec.medium)
    session.paths = [b"/music"]  # a real session always has toppaths; /music/album is under it
    # Original scan's item has a path (so art_source is set) but no embedded art.
    task.items[0].path = os.fsencode(ORIG_PATH)
    new_items: list[Item] = [
        Item(
            artist="Radiohead",
            album="Amnesiac",
            title="Pyramid Song",
            track=1,
            length=200.0,
            path=os.fsencode(NEW_PATH),
        )
    ]
    monkeypatch.setattr(session_mod, "_read_items", lambda p: new_items)
    monkeypatch.setattr(
        session_mod,
        "lookup_items",
        lambda items, s: ("Radiohead", "Amnesiac", [other], BeetsRec.strong),
    )
    monkeypatch.setattr(session_mod, "embedded_art", fake_embedded_art)

    done = threading.Event()

    def worker() -> None:
        task.choose_match(session)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    first = bridge.get_parked(timeout=2.0)
    assert first is not None
    # Baseline: the original park saw NO embedded art on the original file.
    assert first.candidate.has_current_art is False
    assert session.bridge.art_source(first.album_index) == ORIG_PATH
    bridge.push_choice(first.album_index, ImportChoice(action=ImportAction.rescan))

    second = bridge.get_parked(timeout=2.0)
    assert second is not None
    assert second.candidate.search_revision == 1
    assert second.candidate.search_feedback is None
    # The invariant under test: the re-park reflects the NEW items' art state.
    assert second.candidate.has_current_art is True
    assert session.bridge.art_source(second.album_index) == NEW_PATH

    bridge.push_choice(second.album_index, ImportChoice(action=ImportAction.apply))
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)
    assert task.match is other


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
