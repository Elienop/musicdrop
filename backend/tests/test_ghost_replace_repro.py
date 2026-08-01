"""Repro: attended Replace of a GHOST album (DB rows, files deleted off disk).

Reported live bug: a "ghost" album survives in the beets DB (rows only; the
folder was deleted outside MusicDrop). The user re-imports the same album from a
real source folder. beets flags it a duplicate of the ghost -> the duplicate
prompt appears -> the user chooses "Replace old". EXPECTED: the new album lands
on disk under the library and the ghost's DB rows are dropped. REPORTED ACTUAL:
nothing imports, no new folder, the ghost remains.

These tests drive the REAL ``BeetsImportRunner`` (attended, ``options=None`` ->
manual default, no directive) end to end: a real hermetic Library, a real source
folder of tagged FLACs, ``tag_album`` patched to a canned match (so the album
PARKS, mirroring the user's review), and a consumer that answers park->apply (or
asis) then duplicate->replace exactly like the UI. Every variant PASSES: the new
album lands and the ghost drops, so the Replace mechanism itself is correct.
"""

from __future__ import annotations

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
from beets.library import Item, Library

from app.beets.import_session import ImportBridge
from app.beets.library import _require_id
from app.import_jobs.runner import BeetsImportRunner
from app.models.import_models import (
    DuplicateAction,
    DuplicateDecision,
    ImportAction,
    ImportChoice,
    ImportOptions,
)
from tests.conftest import build_library

SAMPLE = Path(__file__).parent / "fixtures" / "silent.flac"


@pytest.fixture(autouse=True)
def _serial_copy_mode() -> None:
    """Manual default (starter beets config): copy: yes, single-threaded."""
    config["threaded"] = False
    config["import"]["copy"] = True
    config["import"]["move"] = False


def _make_tagged_flac(dst: Path, *, album: str, artist: str, title: str, track: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SAMPLE, dst)
    item = Item(album=album, albumartist=artist, artist=artist, title=title, track=track, disc=1)
    item.path = os.fsencode(str(dst))
    item.write()


def _seed_ghost(lib: Library, music: Path, *, artist: str, album: str, folder: str) -> int:
    """Add a 1-track album with a real file, then DELETE the folder off disk.

    Returns the ghost album id. DB rows survive; the file/folder do not - the
    exact state the bug describes.
    """
    base = music / folder
    base.mkdir(parents=True, exist_ok=True)
    f = base / "01 old.flac"
    shutil.copyfile(SAMPLE, f)
    item = Item(album=album, albumartist=artist, artist=artist, title="old", track=1, disc=1)
    item.path = os.fsencode(str(f))
    al = lib.add_album([item])
    al.store()
    ghost_id = _require_id(al.id)
    shutil.rmtree(base)  # the folder is deleted OUTSIDE the app -> ghost
    return ghost_id


def _patch_match(
    monkeypatch: pytest.MonkeyPatch, *, artist: str, album: str, rec: BeetsRec
) -> None:
    """Patch beets' tag_album seam to a canned match built from the REAL items."""

    def fake_tag_album(items: Any, search_ids: Any = None) -> tuple[str, str, Proposal]:
        tracks = [
            TrackInfo(title=it.title or "t", track_id=f"t{i}", index=i, length=it.length or 1.0)
            for i, it in enumerate(items, start=1)
        ]
        info = AlbumInfo(
            tracks=tracks,
            album=album,
            artist=artist,
            album_id="rel-new",
            data_source="MusicBrainz",
            data_url="https://mb/rel-new",
            year=2007,
            va=False,
        )
        pairs, extra_i, extra_t = assign_items(items, info.tracks)
        match = AlbumMatch(distance(items, info, pairs), info, dict(pairs), extra_i, extra_t)
        return (artist, album, Proposal([match], rec))

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)


def _patch_match_per_item(monkeypatch: pytest.MonkeyPatch) -> None:
    """tag_album keyed off each task's own tags: Enema=medium, self-titled=none.

    Mirrors the reported run (row 1 blink-182 self-titled 43% no-match, row 2
    Enema of the State 94% medium) so both albums park for attended review.
    """

    def fake_tag_album(items: Any, search_ids: Any = None) -> tuple[str, str, Proposal]:
        artist = items[0].artist or "blink-182"
        album = items[0].album or "blink-182"
        rec = BeetsRec.medium if album == "Enema of the State" else BeetsRec.none
        tracks = [
            TrackInfo(title=it.title or "t", track_id=f"t{i}", index=i, length=it.length or 1.0)
            for i, it in enumerate(items, start=1)
        ]
        info = AlbumInfo(
            tracks=tracks,
            album=album,
            artist=artist,
            album_id=f"rel-{album}",
            data_source="MusicBrainz",
            data_url="https://mb/x",
            year=1999,
            va=False,
        )
        pairs, extra_i, extra_t = assign_items(items, info.tracks)
        match = AlbumMatch(distance(items, info, pairs), info, dict(pairs), extra_i, extra_t)
        return (artist, album, Proposal([match], rec))

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)


def _run_attended(runner: BeetsImportRunner, source: Path, bridge: ImportBridge) -> _RunHandle:
    """Start an attended manual import (options=default -> move=None, no directive)."""
    done = threading.Event()
    errors: list[str] = []

    def _on_error(message: str) -> None:
        errors.append(message)
        done.set()

    runner.run(
        [str(source)],
        bridge,
        on_finish=done.set,
        on_error=_on_error,
        options=ImportOptions(operation="default"),
        directive=None,
    )
    return _RunHandle(done, errors)


class _RunHandle:
    def __init__(self, done: threading.Event, errors: list[str]) -> None:
        self.done = done
        self.errors = errors

    def wait(self) -> None:
        assert self.done.wait(timeout=30.0), "import worker did not finish"
        assert not self.errors, f"worker errored: {self.errors}"


def _answer(bridge: ImportBridge, review: ImportChoice, *, timeout: float = 20.0) -> None:
    """UI consumer for ONE album: answer the review park, then replace the dup."""
    parked = bridge.get_parked(timeout=timeout)
    assert parked is not None, "album never parked for review"
    bridge.push_choice(parked.album_index, review)
    prompt = bridge.get_parked_duplicate(timeout=timeout)
    assert prompt is not None, "duplicate prompt never appeared"
    bridge.push_duplicate_decision(
        prompt.album_index, DuplicateDecision(action=DuplicateAction.replace)
    )


_APPLY = ImportChoice(action=ImportAction.apply, candidate_index=0)
_ASIS = ImportChoice(action=ImportAction.asis)


def test_attended_replace_of_ghost_imports_new_and_drops_ghost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    music = tmp_path / "music"
    music.mkdir()
    lib = build_library(str(tmp_path / "library.db"), str(music))

    ghost_id = _seed_ghost(
        lib, music, artist="Radiohead", album="In Rainbows", folder="Radiohead/In Rainbows"
    )
    source = tmp_path / "incoming" / "Radiohead - In Rainbows"
    _make_tagged_flac(
        source / "01 15 Step.flac",
        album="In Rainbows",
        artist="Radiohead",
        title="15 Step",
        track=1,
    )
    _patch_match(monkeypatch, artist="Radiohead", album="In Rainbows", rec=BeetsRec.medium)

    bridge = ImportBridge()
    handle = _run_attended(BeetsImportRunner(lib, trash_dir=tmp_path / "trash"), source, bridge)
    _answer(bridge, _APPLY)
    handle.wait()

    assert lib.get_album(ghost_id) is None, "ghost album still in the DB after Replace"
    albums = list(lib.albums())
    assert len(albums) == 1, f"expected exactly the new album, got {[a.album for a in albums]}"
    new_items = list(albums[0].items())
    assert new_items, "new album has no items"
    landed = os.fsdecode(new_items[0].path)
    assert os.path.isfile(landed), f"new album file not on disk: {landed}"
    assert str(music) in landed, f"new file not under the library root: {landed}"


def test_attended_replace_two_ghost_albums_one_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported run: ONE 'Add from folder' over an artist dir with TWO albums,
    both ghosts, user applies + replaces each. Both must land; both ghosts drop."""
    music = tmp_path / "music"
    music.mkdir()
    lib = build_library(str(tmp_path / "library.db"), str(music))

    ghost_self = _seed_ghost(
        lib, music, artist="blink-182", album="blink-182", folder="blink-182/blink-182"
    )
    ghost_enema = _seed_ghost(
        lib,
        music,
        artist="blink-182",
        album="Enema of the State",
        folder="blink-182/Enema of the State",
    )
    src_artist = tmp_path / "incoming" / "blink-182"
    _make_tagged_flac(
        src_artist / "blink-182" / "01 Feeling This.flac",
        album="blink-182",
        artist="blink-182",
        title="Feeling This",
        track=1,
    )
    _make_tagged_flac(
        src_artist / "Enema of the State" / "01 Dumpweed.flac",
        album="Enema of the State",
        artist="blink-182",
        title="Dumpweed",
        track=1,
    )
    _patch_match_per_item(monkeypatch)

    bridge = ImportBridge()
    handle = _run_attended(BeetsImportRunner(lib, trash_dir=tmp_path / "trash"), src_artist, bridge)
    for _ in range(2):
        _answer(bridge, _APPLY)
    handle.wait()

    assert lib.get_album(ghost_self) is None, "self-titled ghost still present"
    assert lib.get_album(ghost_enema) is None, "Enema ghost still present"
    albums = sorted(a.album for a in lib.albums())
    assert albums == ["Enema of the State", "blink-182"], f"unexpected albums: {albums}"
    for al in lib.albums():
        for it in al.items():
            assert os.path.isfile(os.fsdecode(it.path)), f"missing file for {al.album}"


def test_attended_asis_replace_of_ghost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The 'no match' row route: user imports AS-IS (keep file tags) then replaces."""
    music = tmp_path / "music"
    music.mkdir()
    lib = build_library(str(tmp_path / "library.db"), str(music))
    ghost_id = _seed_ghost(
        lib, music, artist="blink-182", album="blink-182", folder="blink-182/blink-182"
    )
    source = tmp_path / "incoming" / "blink-182"
    _make_tagged_flac(
        source / "01 Feeling This.flac",
        album="blink-182",
        artist="blink-182",
        title="Feeling This",
        track=1,
    )
    # rec=none so the album parks (attended) rather than auto-applying.
    _patch_match(monkeypatch, artist="blink-182", album="blink-182", rec=BeetsRec.none)

    bridge = ImportBridge()
    handle = _run_attended(BeetsImportRunner(lib, trash_dir=tmp_path / "trash"), source, bridge)
    _answer(bridge, _ASIS)
    handle.wait()

    assert lib.get_album(ghost_id) is None, "ghost still present (asis replace)"
    albums = list(lib.albums())
    assert len(albums) == 1, f"expected the new album only, got {[a.album for a in albums]}"
    for it in albums[0].items():
        assert os.path.isfile(os.fsdecode(it.path)), "new asis album file missing on disk"


def test_attended_replace_of_ghost_inlibrary_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source folder sits INSIDE the library (guard forces move) + ghost.

    Reproduces the plausible real state: the artist folder was deleted (ghost),
    then re-downloaded back INTO the library. is_in_library_source -> move=True.
    """
    music = tmp_path / "music"
    music.mkdir()
    lib = build_library(str(tmp_path / "library.db"), str(music))
    ghost_id = _seed_ghost(
        lib, music, artist="Radiohead", album="In Rainbows", folder="Radiohead/In Rainbows"
    )
    # A DIFFERENT folder but physically under the library root.
    source = music / "incoming" / "Radiohead - In Rainbows"
    _make_tagged_flac(
        source / "01 15 Step.flac",
        album="In Rainbows",
        artist="Radiohead",
        title="15 Step",
        track=1,
    )
    _patch_match(monkeypatch, artist="Radiohead", album="In Rainbows", rec=BeetsRec.medium)

    bridge = ImportBridge()
    handle = _run_attended(BeetsImportRunner(lib, trash_dir=tmp_path / "trash"), source, bridge)
    _answer(bridge, _APPLY)
    handle.wait()

    assert lib.get_album(ghost_id) is None, "ghost still present (in-library source)"
    albums = list(lib.albums())
    assert len(albums) == 1, f"expected the new album only, got {[a.album for a in albums]}"
    for it in albums[0].items():
        assert os.path.isfile(os.fsdecode(it.path)), "new album file missing on disk"


def test_attended_empty_source_after_prior_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry over a source whose files a prior attempt already MOVED away.

    beets finds no music, no tasks fire, the run finishes clean with an empty
    feed. Documents the 'nothing imported' confusion (ghost untouched)."""
    music = tmp_path / "music"
    music.mkdir()
    lib = build_library(str(tmp_path / "library.db"), str(music))
    ghost_id = _seed_ghost(
        lib, music, artist="Radiohead", album="In Rainbows", folder="Radiohead/In Rainbows"
    )
    empty_source = tmp_path / "incoming" / "Radiohead - In Rainbows"
    empty_source.mkdir(parents=True)  # exists but holds no audio
    _patch_match(monkeypatch, artist="Radiohead", album="In Rainbows", rec=BeetsRec.medium)

    bridge = ImportBridge()
    handle = _run_attended(
        BeetsImportRunner(lib, trash_dir=tmp_path / "trash"), empty_source, bridge
    )
    handle.wait()
    # Nothing parked, nothing landed, ghost untouched: the confusing no-op retry.
    assert bridge.get_parked(timeout=0.1) is None
    assert bridge.drain_outcomes() == []
    assert lib.get_album(ghost_id) is not None, "ghost unexpectedly dropped by an empty import"
