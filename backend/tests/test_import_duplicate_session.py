"""resolve_duplicate hook behavior (hermetic, no network, no real files).

Mirrors test_import_session.py: a canned AlbumMatch via the tag_album seam, a
session built with __new__ so we never call run(), and the hook driven on a
worker thread that blocks on the bridge until a decision is pushed.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
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
from beets.importer.tasks import ImportTask, SingletonImportTask
from beets.library import Item

from app.beets.import_session import (
    ImportBridge,
    WebImportSession,
    _trash_replaced_albums,
)
from app.models.bank import BankApplyDirective
from app.models.import_models import (
    AlbumOutcomeStatus,
    DuplicateAction,
    DuplicateDecision,
)


@pytest.fixture(autouse=True)
def _force_serial() -> Any:
    config["threaded"] = False
    yield
    config["threaded"] = False


def _match() -> AlbumMatch:
    items = [Item(artist="Radiohead", album="In Rainbows", title="15 Step", track=1, length=234.0)]
    tracks = [TrackInfo(title="15 Step", track_id="t1", index=1, length=234.0)]
    info = AlbumInfo(
        tracks=tracks,
        album="In Rainbows",
        artist="Radiohead",
        album_id="a1",
        data_source="MusicBrainz",
        data_url="https://mb/a1",
        year=2007,
        va=False,
    )
    pairs, extra_items, extra_tracks = assign_items(items, info.tracks)
    return AlbumMatch(distance(items, info, pairs), info, dict(pairs), extra_items, extra_tracks)


def _session(bridge: ImportBridge, *, trash_dir: Path | None = None) -> WebImportSession:
    session = WebImportSession.__new__(WebImportSession)
    session.logger = logging.getLogger("test.dup")
    session.bridge = bridge
    session._album_index = 0
    session._trash_dir = trash_dir
    # Wired as a PAIR with _trash_dir (see WebImportSession): the post-run
    # Replace pass skips entirely unless both are set.
    session._trash_origins_dir = None if trash_dir is None else trash_dir.parent / "trash-origins"
    session._replace_album_ids = set()
    # __init__ is skipped, so default the two id sets the Replace machinery
    # reads: what the duplicate hook already trashed, and what this run landed
    # (the post-run pass compares its paths before it moves anything).
    session._hook_replaced_album_ids = set()
    session._landed_album_ids = set()
    # __init__ is skipped, so default the library the shared ExistingAlbum mapper
    # reads. None is safe: these fakes carry no items, so folder resolves to "".
    session.lib = None  # type: ignore[assignment]  # fake session never dereferences lib
    # __init__ is skipped, so default the attended flag resolve_duplicate reads.
    session.unattended = False
    # __init__ is skipped, so default the sweep flag + bank dir the unattended
    # branch reads (sweep banking lives in chunk 3; these tests stay non-sweep).
    session.sweep = False
    session._bank_dir = None
    # __init__ is skipped, so default the playlists dir the post-run Replace
    # trash pass reads for its `.m3u8` re-export. None = the pass is skipped.
    session._playlists_dir = None
    # __init__ is skipped, so default the apply directive the hooks now read.
    session._directive = None
    # __init__ is skipped, so set the toppaths _task_folder scopes by (beets sets
    # these in ImportSession.__init__). The tasks import from /incoming.
    session.paths = [b"/incoming"]
    return session


def _task(match: AlbumMatch, monkeypatch: pytest.MonkeyPatch) -> ImportTask:
    def fake_tag_album(items: Any, search_ids: Any = None) -> tuple[str, str, Proposal]:
        return ("Radiohead", "In Rainbows", Proposal([match], BeetsRec.strong))

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)
    task = ImportTask(toppath=None, paths=[b"/incoming/album"], items=list(match.mapping.keys()))
    task.lookup_candidates([])
    task.set_choice(match)  # APPLY: the album matched (mirrors the auto-apply path)
    return task


class _FakeAlbum:
    """A minimal stand-in for a beets library Album in found_duplicates."""

    def __init__(
        self,
        album_id: int,
        *,
        data_source: str | None = None,
        mb_albumid: str | None = None,
        label: str | None = None,
    ) -> None:
        self.id = album_id
        self.albumartist = "Radiohead"
        self.album = "In Rainbows"
        self.year = 2007
        self.data_source = data_source
        self.mb_albumid = mb_albumid
        self.label = label

    def get(self, key: str, default: Any = None) -> Any:
        # Mirror beets Album.get so the shared ExistingAlbum mapper's
        # ``album.get("year")`` works against the fake.
        return getattr(self, key, default)

    def items(self) -> list[Any]:
        return []


def _run_hook(
    session: WebImportSession, task: ImportTask, dups: list[Any]
) -> tuple[threading.Thread, dict[str, Any]]:
    """Run the (blocking) get_duplicate_action hook on a worker thread, capturing
    its returned beets DuplicateAction into ``result['action']`` once it unblocks."""
    result: dict[str, Any] = {}

    def target() -> None:
        result["action"] = session.get_duplicate_action(task, dups)

    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, result


def test_resolve_duplicate_parks_and_emits_needs_dup_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    match = _match()
    bridge = ImportBridge()
    session = _session(bridge)
    task = _task(match, monkeypatch)
    # dynamic attr beets' ImportTask doesn't declare (mirrors choose_match's stash)
    task.md_album_index = 7  # type: ignore[attr-defined]  # the index choose_match assigned

    t, result = _run_hook(session, task, [_FakeAlbum(1)])
    prompt = bridge.get_parked_duplicate(timeout=2.0)
    assert prompt is not None
    assert prompt.album_index == 7  # reuses the album's existing feed index
    assert prompt.incoming.album == "In Rainbows"
    assert prompt.existing[0].album_id == 1

    outcomes = bridge.drain_outcomes()
    assert any(o.status is AlbumOutcomeStatus.needs_dup_resolution for o in outcomes)

    bridge.push_duplicate_decision(7, DuplicateDecision(action=DuplicateAction.keep_both))
    t.join(timeout=2.0)
    assert result["action"] is BeetsDuplicateAction.KEEP  # keep_both -> import alongside


def test_duplicate_prompt_carries_release_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    match = _match()  # AlbumInfo: data_source MusicBrainz, album_id "a1"
    bridge = ImportBridge()
    session = _session(bridge)
    task = _task(match, monkeypatch)
    task.md_album_index = 0  # type: ignore[attr-defined]  # choose_match's stash
    existing = _FakeAlbum(1, data_source="MusicBrainz", mb_albumid="e1", label="XL Recordings")

    t, _ = _run_hook(session, task, [existing])
    prompt = bridge.get_parked_duplicate(timeout=2.0)
    assert prompt is not None
    # incoming = the matched release (what the import will become)
    assert prompt.incoming.release is not None
    assert prompt.incoming.release.data_source == "MusicBrainz"
    assert prompt.incoming.release.release_url == "https://musicbrainz.org/release/a1"
    # existing = the library copy's own release
    assert prompt.existing[0].release is not None
    assert prompt.existing[0].release.release_url == "https://musicbrainz.org/release/e1"
    assert prompt.existing[0].release.label == "XL Recordings"

    bridge.push_duplicate_decision(0, DuplicateDecision(action=DuplicateAction.keep_both))
    t.join(timeout=2.0)


def test_unattended_resolve_duplicate_skips_without_parking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unattended: a library duplicate emits the needs_dup_resolution outcome (so
    # the feed records the set-aside) but sets SKIP without parking + blocking.
    match = _match()
    bridge = ImportBridge()
    session = _session(bridge)
    session.unattended = True
    task = _task(match, monkeypatch)
    task.md_album_index = 0  # type: ignore[attr-defined]  # dynamic attr (see above)

    action = session.get_duplicate_action(task, [_FakeAlbum(1)])

    assert bridge.pending_count() == 0  # did NOT park
    assert action is BeetsDuplicateAction.SKIP  # new album skipped, library copy kept
    assert any(o.status is AlbumOutcomeStatus.needs_dup_resolution for o in bridge.drain_outcomes())


def test_resolve_duplicate_records_art_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # Mirrors test_park_records_art_source: resolve_duplicate must record the
    # incoming files' art source on the bridge so the /cover endpoint can serve
    # the "Importing (new)" panel for a duplicate row (the file need not exist —
    # art_source records the PATH; has_current_art is a separate concern).
    match = _match()
    bridge = ImportBridge()
    session = _session(bridge)
    task = _task(match, monkeypatch)
    task.md_album_index = 3  # type: ignore[attr-defined]  # dynamic attr (see above)
    art_path = os.fsencode(str(tmp_path / "a.flac"))
    task.items[0].path = art_path

    t, _ = _run_hook(session, task, [_FakeAlbum(1)])
    prompt = bridge.get_parked_duplicate(timeout=2.0)
    assert prompt is not None
    assert session.bridge.art_source(prompt.album_index) == os.fsdecode(art_path)

    bridge.push_duplicate_decision(3, DuplicateDecision(action=DuplicateAction.keep_both))
    t.join(timeout=2.0)


def test_skip_new_sets_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    match = _match()
    bridge = ImportBridge()
    session = _session(bridge)
    task = _task(match, monkeypatch)
    task.md_album_index = 0  # type: ignore[attr-defined]  # dynamic attr (see above)

    t, result = _run_hook(session, task, [_FakeAlbum(1)])
    prompt = bridge.get_parked_duplicate(timeout=2.0)
    assert prompt is not None
    bridge.push_duplicate_decision(0, DuplicateDecision(action=DuplicateAction.skip_new))
    t.join(timeout=2.0)
    assert result["action"] is BeetsDuplicateAction.SKIP


def test_merge_returns_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    match = _match()
    bridge = ImportBridge()
    session = _session(bridge)
    task = _task(match, monkeypatch)
    task.md_album_index = 0  # type: ignore[attr-defined]  # dynamic attr (see above)

    t, result = _run_hook(session, task, [_FakeAlbum(1)])
    assert bridge.get_parked_duplicate(timeout=2.0) is not None
    bridge.push_duplicate_decision(0, DuplicateDecision(action=DuplicateAction.merge))
    t.join(timeout=2.0)
    # beets 2.12 merges when task.duplicate_action is MERGE (never the hard-delete
    # REMOVE); our hook returns MERGE for the merge decision.
    assert result["action"] is BeetsDuplicateAction.MERGE


class _RootOnlyLib:
    """The smallest library ``require_library_root`` accepts: where the music is.

    The replace arm asks the root question before it reads "this album has no
    file" as a ghost, so a fake session that answers Replace needs a real music
    directory even when its albums carry no items.
    """

    def __init__(self, music_dir: Path) -> None:
        self.directory = os.fsencode(str(music_dir))


def test_replace_of_file_less_duplicates_answers_remove_and_trashes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both fakes carry no items, so both are ghosts: rows and no files.

    There is nothing to move, so the hook answers beets' own REMOVE — which runs
    at ``importer/stages.py:276``, before any file is placed at ``:293``, and
    whose ``util.remove`` is soft on a missing file. Nothing is recorded for the
    post-run pass: that pass is the BANKED route's only (see
    ``_seed_replace_from_directive``).

    The music root holds an unrelated folder: present and non-empty, so the root
    check passes, while the duplicates themselves still have no file anywhere.
    """
    music = tmp_path / "music"
    (music / "Someone Else").mkdir(parents=True)
    match = _match()
    bridge = ImportBridge()
    session = _session(bridge, trash_dir=Path("/tmp/trash"))
    session.lib = _RootOnlyLib(music)  # type: ignore[assignment]  # root-only stand-in, not a Library
    task = _task(match, monkeypatch)
    task.md_album_index = 0  # type: ignore[attr-defined]  # dynamic attr (see above)

    t, result = _run_hook(session, task, [_FakeAlbum(11), _FakeAlbum(22)])
    assert bridge.get_parked_duplicate(timeout=2.0) is not None
    bridge.push_duplicate_decision(0, DuplicateDecision(action=DuplicateAction.replace))
    t.join(timeout=2.0)
    assert result["action"] is BeetsDuplicateAction.REMOVE
    assert session._replace_album_ids == set()
    assert session._hook_replaced_album_ids == set()  # nothing was moved


def test_singleton_astracks_duplicate_skips_without_crashing() -> None:
    """An "as tracks" import re-pipelines each file as a SingletonImportTask
    (``is_album`` False). When such a track duplicates a library item, beets 2.12
    calls ``get_duplicate_action`` with Items (not Albums), so the album-shaped
    prompt / replace machinery must not run: it used to crash the WHOLE import job
    here (``to_existing_album`` does ``items[0].path`` on a ``Model.items()`` field
    tuple). The safe resolution is SKIP — keep the library track, drop the dup."""
    bridge = ImportBridge()
    session = _session(bridge)
    item = Item(artist="Radiohead", title="15 Step", path=b"/incoming/15 Step.flac")
    task = SingletonImportTask(toppath=None, item=item)
    task.set_choice(Action.ASIS)  # as-tracks imports each singleton ASIS
    dup = Item(artist="Radiohead", title="15 Step", path=b"/library/15 Step.flac")
    dup.id = 501  # a real library Item carries an id (find_duplicates returns rows)

    action = session.get_duplicate_action(task, [dup])

    assert action is BeetsDuplicateAction.SKIP  # keep the library track, drop the dup
    assert bridge.pending_count() == 0  # never parked (no album-shaped prompt)
    assert bridge.drain_outcomes() == []  # no album feed row flipped for a singleton
    assert session._replace_album_ids == set()  # no Item ids recorded as albums to trash


def test_singleton_astracks_duplicate_ignores_replace_directive() -> None:
    """A banked astracks apply carries an album-level ``duplicate_action``; it must
    NEVER be applied to an individual singleton duplicate. Doing so recorded the
    Item ids into ``_replace_album_ids`` and the post-run Trash pass would then
    delete the album that happens to share that id — a wrong-album data loss.
    Singletons SKIP regardless of the directive."""
    bridge = ImportBridge()
    session = _session(bridge)
    session._directive = BankApplyDirective(
        action="astracks", duplicate_action=DuplicateAction.replace
    )
    item = Item(artist="Radiohead", title="15 Step", path=b"/incoming/15 Step.flac")
    task = SingletonImportTask(toppath=None, item=item)
    task.set_choice(Action.ASIS)
    dup = Item(artist="Radiohead", title="15 Step", path=b"/library/15 Step.flac")
    dup.id = 501  # the album with id 501 must NOT be trashed by an item-id collision

    action = session.get_duplicate_action(task, [dup])

    assert action is BeetsDuplicateAction.SKIP
    assert session._replace_album_ids == set()  # NOT {501} — no wrong-album trash


def test_trash_replaced_albums_runs_from_a_worker_thread(
    duplicates_lib: Any, tmp_path: Path
) -> None:
    """Post-run Replace trashing runs on the import worker thread.

    Same root cause as the /duplicates resolve path: beets 2.11 expands DB-relative
    item paths via a ``ContextVar`` set when the ``Library`` is opened (main thread),
    which worker threads do not inherit, so ``Album.move`` got a relative source and
    raised ``FileNotFoundError``. ``_trash_replaced_albums`` binds the music dir so
    it works from any thread. Running it in a worker reproduces the import worker.
    """
    trash = tmp_path / "trash"
    target = next(iter(duplicates_lib.albums()))
    target_id = int(target.id)
    session = _session(ImportBridge(), trash_dir=trash)
    session.lib = duplicates_lib
    session._replace_album_ids = {target_id}

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_trash_replaced_albums, session).result()

    assert duplicates_lib.get_album(target_id) is None  # dropped from the library
    assert trash.is_dir()  # files relocated under Trash
    assert any(trash.iterdir())  # files relocated under Trash
