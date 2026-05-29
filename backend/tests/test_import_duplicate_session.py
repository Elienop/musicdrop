"""resolve_duplicate hook behavior (hermetic, no network, no real files).

Mirrors test_import_session.py: a canned AlbumMatch via the tag_album seam, a
session built with __new__ so we never call run(), and the hook driven on a
worker thread that blocks on the bridge until a decision is pushed.
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
from beets.autotag.distance import distance
from beets.autotag.hooks import AlbumInfo, AlbumMatch, TrackInfo
from beets.autotag.match import Proposal, assign_items
from beets.autotag.match import Recommendation as BeetsRec
from beets.importer.tasks import Action, ImportTask
from beets.library import Item

from app.beets.import_session import ImportBridge, WebImportSession
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
    session._replace_album_ids = set()
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

    def __init__(self, album_id: int) -> None:
        self.id = album_id
        self.albumartist = "Radiohead"
        self.album = "In Rainbows"
        self.year = 2007

    def items(self) -> list[Any]:
        return []


def _run_hook(session: WebImportSession, task: ImportTask, dups: list[Any]) -> threading.Thread:
    t = threading.Thread(target=lambda: session.resolve_duplicate(task, dups), daemon=True)
    t.start()
    return t


def test_resolve_duplicate_parks_and_emits_needs_dup_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    match = _match()
    bridge = ImportBridge()
    session = _session(bridge)
    task = _task(match, monkeypatch)
    # dynamic attr beets' ImportTask doesn't declare (mirrors choose_match's stash)
    task.md_album_index = 7  # type: ignore[attr-defined]  # the index choose_match assigned

    t = _run_hook(session, task, [_FakeAlbum(1)])
    prompt = bridge.get_parked_duplicate(timeout=2.0)
    assert prompt is not None
    assert prompt.album_index == 7  # reuses the album's existing feed index
    assert prompt.incoming.album == "In Rainbows"
    assert prompt.existing[0].album_id == 1

    outcomes = bridge.drain_outcomes()
    assert any(o.status is AlbumOutcomeStatus.needs_dup_resolution for o in outcomes)

    bridge.push_duplicate_decision(7, DuplicateDecision(action=DuplicateAction.keep_both))
    t.join(timeout=2.0)
    assert task.choice_flag is Action.APPLY  # keep_both leaves the choice intact


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

    t = _run_hook(session, task, [_FakeAlbum(1)])
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

    t = _run_hook(session, task, [_FakeAlbum(1)])
    prompt = bridge.get_parked_duplicate(timeout=2.0)
    assert prompt is not None
    bridge.push_duplicate_decision(0, DuplicateDecision(action=DuplicateAction.skip_new))
    t.join(timeout=2.0)
    assert task.choice_flag is Action.SKIP


def test_merge_sets_should_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    match = _match()
    bridge = ImportBridge()
    session = _session(bridge)
    task = _task(match, monkeypatch)
    task.md_album_index = 0  # type: ignore[attr-defined]  # dynamic attr (see above)

    t = _run_hook(session, task, [_FakeAlbum(1)])
    assert bridge.get_parked_duplicate(timeout=2.0) is not None
    bridge.push_duplicate_decision(0, DuplicateDecision(action=DuplicateAction.merge))
    t.join(timeout=2.0)
    assert task.should_merge_duplicates is True
    assert task.should_remove_duplicates is False  # never beets' hard-delete


def test_replace_records_ids_without_hard_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    match = _match()
    bridge = ImportBridge()
    session = _session(bridge, trash_dir=Path("/tmp/trash"))
    task = _task(match, monkeypatch)
    task.md_album_index = 0  # type: ignore[attr-defined]  # dynamic attr (see above)

    t = _run_hook(session, task, [_FakeAlbum(11), _FakeAlbum(22)])
    assert bridge.get_parked_duplicate(timeout=2.0) is not None
    bridge.push_duplicate_decision(0, DuplicateDecision(action=DuplicateAction.replace))
    t.join(timeout=2.0)
    # New album imports normally; old copies are recorded for post-run trashing.
    assert task.choice_flag is Action.APPLY
    assert task.should_remove_duplicates is False
    assert session._replace_album_ids == {11, 22}
