# backend/tests/test_reorganize_adapter.py
import logging
import os
import shutil
from pathlib import Path

import pytest
from beets.library import Album, Item, Library

from app.beets import reorganize as reorg
from app.beets.library import _require_id
from app.beets.reorganize import (
    _describe_album,
    _item_moves,
    album_label,
    collect_units,
    plan_reorganize,
)
from app.models.reorganize import ReorganizeMove
from tests.conftest import build_library

SAMPLE_FLAC = Path(__file__).parent / "fixtures" / "silent.flac"


def _album(lib: Library, name: str) -> Album:
    return next(a for a in lib.albums() if a.album == name)


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` as {relative path: content} — the churn detector.

    Paths AND bytes, so a rename shows up as a changed key and a swap as changed
    values; a refused unit must leave this dict byte-identical.
    """
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_album_label(reorganize_lib: Library) -> None:
    with reorganize_lib.music_dir_context():
        assert album_label(_album(reorganize_lib, "In Rainbows")) == "Radiohead - In Rainbows"


def test_item_moves_detects_misfiled(reorganize_lib: Library) -> None:
    with reorganize_lib.music_dir_context():
        ir = _album(reorganize_lib, "In Rainbows")
        assert all(_item_moves(reorganize_lib, i) for i in ir.items())
        disc = _album(reorganize_lib, "Discovery")
        assert not any(_item_moves(reorganize_lib, i) for i in disc.items())


def test_describe_album_misfiled_has_distinct_dirs(reorganize_lib: Library) -> None:
    with reorganize_lib.music_dir_context():
        m = _describe_album(reorganize_lib, _album(reorganize_lib, "In Rainbows"))
        assert isinstance(m, ReorganizeMove)
        assert m.kind == "album"
        assert m.track_count == 3
        assert m.from_path != m.to_path
        assert m.to_path.endswith("Radiohead/In Rainbows")


def test_describe_album_already_in_place_is_none(reorganize_lib: Library) -> None:
    with reorganize_lib.music_dir_context():
        assert _describe_album(reorganize_lib, _album(reorganize_lib, "Discovery")) is None


def test_describe_album_rename_in_place_same_dir(reorganize_lib: Library) -> None:
    with reorganize_lib.music_dir_context():
        m = _describe_album(reorganize_lib, _album(reorganize_lib, "Geogaddi"))
        assert isinstance(m, ReorganizeMove)
        assert m.from_path == m.to_path  # only filenames change


def test_collect_units_library_includes_singleton(reorganize_lib: Library) -> None:
    albums, singletons = collect_units(reorganize_lib, scope="library", artist=None, album_id=None)
    assert len(albums) == 3
    assert len(singletons) == 1


def test_plan_library_counts(reorganize_lib: Library) -> None:
    plan = plan_reorganize(reorganize_lib, scope="library", artist=None, album_id=None)
    # 3 albums + 1 singleton = 4 units; Discovery already in place -> 3 will move.
    assert plan.total == 4
    assert plan.will_move == 3
    assert plan.already_in_place == 1
    assert plan.truncated is False
    assert {m.kind for m in plan.moves} == {"album", "singleton"}


def test_plan_artist_scope(reorganize_lib: Library) -> None:
    plan = plan_reorganize(reorganize_lib, scope="artist", artist="Radiohead", album_id=None)
    assert plan.scope_label == "Radiohead"
    assert plan.total == 1 and plan.will_move == 1


def test_multidisc_to_path_is_album_root(tmp_path: Path) -> None:
    music = tmp_path / "music"
    lib = build_library(
        str(tmp_path / "library.db"),
        str(music),
        path_format="$albumartist/$album/Disc $disc/$track $title",
    )
    base = music / "junk"
    base.mkdir(parents=True, exist_ok=True)
    items = []
    for disc in (1, 2):
        f = base / f"d{disc}.mp3"
        f.write_bytes(b"\x00")
        it = Item(album="Wall", albumartist="PF", artist="PF", title=f"T{disc}", track=1, disc=disc)
        it.path = os.fsencode(str(f))
        items.append(it)
    lib.add_album(items).store()
    with lib.music_dir_context():
        album = next(iter(lib.albums()))
        m = reorg._describe_album(lib, album)
    assert isinstance(m, ReorganizeMove)
    # New layout splits across Disc 1/ Disc 2/, but the reported root is the album dir.
    assert m.to_path.endswith("PF/Wall")
    assert m.track_count == 2


def test_preview_row_cap(monkeypatch: pytest.MonkeyPatch, reorganize_lib: Library) -> None:
    monkeypatch.setattr(reorg, "PREVIEW_ROW_CAP", 1)
    plan = reorg.plan_reorganize(reorganize_lib, scope="library", artist=None, album_id=None)
    assert plan.will_move == 3  # exact count unaffected by the cap
    assert len(plan.moves) == 1  # rows capped
    assert plan.truncated is True


def test_reorganize_album_moves_and_prunes(reorganize_lib: Library) -> None:
    with reorganize_lib.music_dir_context():
        album = _album(reorganize_lib, "In Rainbows")
        old_dir = os.path.dirname(os.fsdecode(next(iter(album.items())).path))
        outcome = reorg.reorganize_album(reorganize_lib, album)
    assert outcome.status == "moved"
    assert not os.path.exists(old_dir)  # vacated source pruned
    music = os.fsdecode(reorganize_lib.directory)
    moved = os.path.join(music, "Radiohead", "In Rainbows")
    assert os.path.isdir(moved)
    # DB paths now point at the new location.
    with reorganize_lib.music_dir_context():
        again = _album(reorganize_lib, "In Rainbows")
        assert all(not reorg._item_moves(reorganize_lib, i) for i in again.items())


def test_reorganize_album_already_in_place_skips(reorganize_lib: Library) -> None:
    with reorganize_lib.music_dir_context():
        outcome = reorg.reorganize_album(reorganize_lib, _album(reorganize_lib, "Discovery"))
    assert outcome.status == "skipped"


def test_reorganize_singleton_moves(reorganize_lib: Library) -> None:
    with reorganize_lib.music_dir_context():
        item = next(iter(reorganize_lib.items("singleton:true")))
        outcome = reorg.reorganize_singleton(reorganize_lib, item)
    assert outcome.status == "moved"


def test_reorganize_album_failure_is_caught(
    monkeypatch: pytest.MonkeyPatch, reorganize_lib: Library
) -> None:
    with reorganize_lib.music_dir_context():
        album = _album(reorganize_lib, "In Rainbows")

        def boom(*a: object, **k: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(type(album), "move", boom)
        outcome = reorg.reorganize_album(reorganize_lib, album)
    assert outcome.status == "failed"
    assert "disk full" in (outcome.error or "")


def test_reorganize_album_isolates_beets_filesystem_error(
    monkeypatch: pytest.MonkeyPatch, reorganize_lib: Library
) -> None:
    """beets raises FilesystemError (a HumanReadableError, NOT an OSError) for real
    move failures — permission denied, disk full, NAS I/O. The 'Never raises'
    adapter must catch it and record status='failed', or one bad album escapes and
    aborts the ENTIRE library-wide reorganize sweep."""
    from beets.util import FilesystemError

    with reorganize_lib.music_dir_context():
        album = _album(reorganize_lib, "In Rainbows")

        def boom(*a: object, **k: object) -> None:
            raise FilesystemError(OSError("permission denied"), "move", (b"/a", b"/b"))

        monkeypatch.setattr(type(album), "move", boom)
        outcome = reorg.reorganize_album(reorganize_lib, album)  # must not raise
    assert outcome.status == "failed"
    assert outcome.error


def test_reorganize_singleton_isolates_beets_filesystem_error(
    monkeypatch: pytest.MonkeyPatch, reorganize_lib: Library
) -> None:
    """Same FilesystemError isolation for the singleton path."""
    from beets.util import FilesystemError

    with reorganize_lib.music_dir_context():
        item = next(iter(reorganize_lib.items("singleton:true")))

        def boom(*a: object, **k: object) -> None:
            raise FilesystemError(OSError("disk full"), "move", (b"/a", b"/b"))

        monkeypatch.setattr(type(item), "move", boom)
        outcome = reorg.reorganize_singleton(reorganize_lib, item)  # must not raise
    assert outcome.status == "failed"
    assert outcome.error


def test_reorganize_album_missing_source_fails(reorganize_lib: Library) -> None:
    """A moving item whose file is gone at the DB path: beets 2.12 silently skips
    the move (models.py:1142 — no store, no exception). Verification must turn the
    unit into `failed` and name the file + cause. Other tracks may still move; we
    assert only status/error."""
    with reorganize_lib.music_dir_context():
        album = _album(reorganize_lib, "In Rainbows")
        victim = os.fsdecode(next(iter(album.items())).path)
        os.remove(victim)  # DB row untouched; on-disk file gone
        outcome = reorg.reorganize_album(reorganize_lib, album)
    assert outcome.status == "failed"
    assert "not found on disk" in (outcome.error or "")


# --- collision pre-flight: refuse BEFORE any file is touched -------------------
#
# beets' Item.move_file diverts a move whose destination is occupied to a
# `.N` sibling (util.unique_path) without raising. Detecting that AFTER the fact
# is useless: the loser vacates the slot it held, unique_path always restarts its
# scan at `.1`, so every sweep renames a real file forever (.1 <-> .2). The unit
# must therefore be refused BEFORE album.move/item.move runs.


def _collide_pair_lib(tmp_path: Path) -> tuple[Library, Path, Path]:
    """(lib, music, album dir) for ONE album whose two tracks render to one name."""
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))
    base = music / "junk" / "col"
    base.mkdir(parents=True, exist_ok=True)
    items = []
    for fname, content in (("01 Song.mp3", b"\x00A"), ("01 Song other.mp3", b"\x00B")):
        f = base / fname
        f.write_bytes(content)
        it = Item(album="Collide", albumartist="X", artist="X", title="Song", track=1, disc=1)
        it.path = os.fsencode(str(f))
        items.append(it)
    lib.add_album(items).store()
    return lib, music, base


def test_reorganize_album_intra_unit_collision_refused_every_run(tmp_path: Path) -> None:
    """Two tracks of one album compute the SAME destination. Every run must refuse
    the unit untouched — no rename, no `.1`, no churn — and say what collided."""
    lib, music, _base = _collide_pair_lib(tmp_path)
    before = _tree(music)

    for _run in range(3):
        plan = reorg.plan_reorganize(lib, scope="library", artist=None, album_id=None)
        with lib.music_dir_context():
            outcome = reorg.reorganize_album(lib, next(iter(lib.albums())))

        assert outcome.status == "failed"
        error = outcome.error or ""
        assert "01 Song other.mp3" in error and "'Song'" in error  # BOTH tracks named
        assert "track number and title" not in error  # no hardcoded guess
        assert _tree(music) == before  # zero renames, zero new files
        assert not (music / "X").exists()  # the destination folder was never created

        # ...and the preview calls it a conflict, never an ordinary move.
        assert plan.will_move == 0
        assert plan.conflicts_total == 1
        assert [c.label for c in plan.conflicts] == ["X - Collide"]
        assert [c.kind for c in plan.conflicts[0].collisions] == ["intra_unit"]
        assert plan.conflicts[0].collisions[0].path == os.path.join("X", "Collide", "01 Song.mp3")
        assert plan.total == plan.will_move + plan.already_in_place + plan.conflicts_total


def test_reorganize_album_refuses_when_a_settled_track_owns_the_destination(
    tmp_path: Path,
) -> None:
    """The ping-pong steady state, and the reason the check reads ALL of the unit's
    items: one twin already SITS at the shared destination (so it is not moving and a
    moving-only view never sees it), the other sits at the `.1` name. Treating the
    occupied slot as a unit-mate relocating this run would rename the loser again."""
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))
    base = music / "X" / "Collide"
    base.mkdir(parents=True)
    items = []
    for fname, content in (("01 Song.mp3", b"\x00A"), ("01 Song.1.mp3", b"\x00B")):
        f = base / fname
        f.write_bytes(content)
        it = Item(album="Collide", albumartist="X", artist="X", title="Song", track=1, disc=1)
        it.path = os.fsencode(str(f))
        items.append(it)
    lib.add_album(items).store()
    before = _tree(music)

    for _run in range(3):
        plan = reorg.plan_reorganize(lib, scope="library", artist=None, album_id=None)
        with lib.music_dir_context():
            outcome = reorg.reorganize_album(lib, next(iter(lib.albums())))
        assert outcome.status == "failed"
        assert _tree(music) == before  # no .2, no .3 — the churn is over
        assert plan.conflicts_total == 1 and plan.will_move == 0
        # Classified by the ALL-items duplicate check, NOT as a foreign occupant:
        # the settled twin is a unit-mate, so the on-disk arm deliberately exempts
        # it (that exemption is what lets a genuine swap self-heal) and only the
        # duplicate-destination arm is left to catch this.
        assert [c.kind for c in plan.conflicts[0].collisions] == ["intra_unit"]
        assert "2 tracks resolve to this same name" in (outcome.error or "")


def _two_album_rows_lib(tmp_path: Path) -> tuple[Library, Path, int, int]:
    """(lib, music, settled album id, colliding album id) — the real-world incident:
    two ALBUM ROWS whose tracks render to the same folder AND filename."""
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))
    settled_dir = music / "X" / "Collide"
    settled_dir.mkdir(parents=True)
    (settled_dir / "01 Song.mp3").write_bytes(b"\x00SETTLED")
    a = Item(album="Collide", albumartist="X", artist="X", title="Song", track=1, disc=1)
    a.path = os.fsencode(str(settled_dir / "01 Song.mp3"))
    album_a = lib.add_album([a])
    other_dir = music / "junk" / "dup"
    other_dir.mkdir(parents=True)
    (other_dir / "raw.mp3").write_bytes(b"\x00OTHER")
    b = Item(album="Collide", albumartist="X", artist="X", title="Song", track=1, disc=1)
    b.path = os.fsencode(str(other_dir / "raw.mp3"))
    album_b = lib.add_album([b])
    return lib, music, _require_id(album_a.id), _require_id(album_b.id)


def test_reorganize_cross_unit_collision_refused_every_run(tmp_path: Path) -> None:
    """Unit A is settled at the destination; unit B renders onto it. B is refused
    every run, A is never touched, and no file is ever renamed."""
    lib, music, a_id, b_id = _two_album_rows_lib(tmp_path)
    before = _tree(music)

    for _run in range(3):
        plan = reorg.plan_reorganize(lib, scope="library", artist=None, album_id=None)
        with lib.music_dir_context():
            albums = {_require_id(al.id): al for al in lib.albums()}
            out_a = reorg.reorganize_album(lib, albums[a_id])
            out_b = reorg.reorganize_album(lib, albums[b_id])

        assert out_a.status == "skipped"  # already in place, untouched
        assert out_b.status == "failed"
        error = out_b.error or ""
        assert "already exists" in error
        assert f"album {a_id}" in error  # names the OTHER album row, not a guess
        assert _tree(music) == before

        assert plan.total == 2
        assert plan.will_move == 0
        assert plan.already_in_place == 1
        assert plan.conflicts_total == 1
        assert [c.kind for c in plan.conflicts[0].collisions] == ["cross_unit"]


def test_reorganize_singleton_refuses_a_destination_held_by_a_stranger(tmp_path: Path) -> None:
    """Singletons get the same pre-flight. Here the occupant is not in the library
    at all, so the message must say exactly that instead of naming a track."""
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))
    loose = music / "loose"
    loose.mkdir(parents=True)
    audio = loose / "z.mp3"
    audio.write_bytes(b"\x00LOOSE")
    item = Item(artist="Aphex Twin", albumartist="Aphex Twin", title="Xtal", track=1)
    item.path = os.fsencode(str(audio))
    lib.add(item)
    squatter = music / "Non-Album" / "Aphex Twin"  # beets' built-in singleton: format
    squatter.mkdir(parents=True)
    (squatter / "Xtal.mp3").write_bytes(b"\x00STRANGER")
    before = _tree(music)

    for _run in range(2):
        plan = reorg.plan_reorganize(lib, scope="library", artist=None, album_id=None)
        with lib.music_dir_context():
            fresh = next(iter(lib.items("singleton:true")))
            outcome = reorg.reorganize_singleton(lib, fresh)
        assert outcome.status == "failed"
        assert "not a file in the library" in (outcome.error or "")
        assert _tree(music) == before
        assert plan.conflicts_total == 1
        assert [c.kind for c in plan.conflicts] == ["singleton"]


def test_reorganize_album_allows_a_swap_between_its_own_tracks(tmp_path: Path) -> None:
    """The self-healing case the pre-flight must NOT refuse: each track's
    destination is held by a unit-mate that is itself relocating this run. beets
    diverts once and the next run settles it; refusing would freeze the album in the
    swapped state forever."""
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))
    base = music / "X" / "Swap"
    base.mkdir(parents=True)
    # track 1 "A" currently lives at "02 B.mp3" and vice versa.
    items = []
    for fname, content, track, title in (
        ("02 B.mp3", b"\x00ONE", 1, "A"),
        ("01 A.mp3", b"\x00TWO", 2, "B"),
    ):
        f = base / fname
        f.write_bytes(content)
        it = Item(album="Swap", albumartist="X", artist="X", title=title, track=track, disc=1)
        it.path = os.fsencode(str(f))
        items.append(it)
    lib.add_album(items).store()

    # Not refused: the preview offers it as a move, not a conflict.
    plan = reorg.plan_reorganize(lib, scope="library", artist=None, album_id=None)
    assert plan.conflicts_total == 0 and plan.will_move == 1

    for _run in range(3):
        with lib.music_dir_context():
            reorg.reorganize_album(lib, next(iter(lib.albums())))

    # Settled: each track under its own name, one divert absorbed on the way.
    assert _tree(music) == {
        os.path.join("X", "Swap", "01 A.mp3"): b"\x00ONE",
        os.path.join("X", "Swap", "02 B.mp3"): b"\x00TWO",
    }
    with lib.music_dir_context():
        assert not any(_item_moves(lib, i) for i in next(iter(lib.albums())).items())


def test_reorganize_album_allows_a_destination_that_is_the_same_file(tmp_path: Path) -> None:
    """beets does not divert when the destination IS the file being moved
    (``move_file`` guards with ``util.samefile``), so the pre-flight must not refuse
    it. Stand-in for the real trigger — a case-only rename on a case-insensitive
    filesystem, where path and destination differ as strings but name one file."""
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))
    base = music / "X" / "Alias"
    base.mkdir(parents=True)
    real = base / "01 Song.mp3"  # the item's own destination
    real.write_bytes(b"\x00A")
    alias = base / "alias.mp3"
    alias.symlink_to(real)
    it = Item(album="Alias", albumartist="X", artist="X", title="Song", track=1, disc=1)
    it.path = os.fsencode(str(alias))  # DB points at the symlink
    lib.add_album([it]).store()

    plan = reorg.plan_reorganize(lib, scope="library", artist=None, album_id=None)

    assert plan.conflicts_total == 0
    assert plan.will_move == 1


def test_reorganize_album_refuses_a_destination_aliasing_a_staying_mates_file(
    tmp_path: Path,
) -> None:
    """The samefile exemption must be as narrow as the beets guard it mirrors:
    ``move_file`` skips ``unique_path`` only when the destination IS the moving
    item's OWN file. A destination that aliases a unit-mate's file (symlink here;
    a case-insensitive NAS mount in the wild) is only safe when that mate itself
    vacates this run — a mate that is STAYING holds the name forever, beets
    diverts to `.N` on every sweep, and the churn this wave kills survives."""
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))
    base = music / "X" / "Alias2"
    base.mkdir(parents=True)
    keeper = base / "02 Keeper.mp3"  # settled: its own computed destination
    keeper.write_bytes(b"\x00KEEP")
    # An on-disk alias of the keeper occupies the mover's destination name.
    (base / "01 Song.mp3").symlink_to(keeper)
    src = music / "junk"
    src.mkdir()
    mover = src / "b.mp3"
    mover.write_bytes(b"\x00MOVE")
    items = []
    for path, track, title in ((keeper, 2, "Keeper"), (mover, 1, "Song")):
        it = Item(album="Alias2", albumartist="X", artist="X", title=title, track=track, disc=1)
        it.path = os.fsencode(str(path))
        items.append(it)
    lib.add_album(items).store()

    plan = reorg.plan_reorganize(lib, scope="library", artist=None, album_id=None)
    assert plan.conflicts_total == 1
    assert plan.conflicts[0].collisions[0].kind == "cross_unit"
    assert plan.will_move == 0

    before = _tree(music)
    for _run in range(2):
        with lib.music_dir_context():
            outcome = reorg.reorganize_album(lib, next(iter(lib.albums())))
        assert outcome.status == "failed"
        assert _tree(music) == before  # never a .1/.2 rename, run after run


def test_carry_follows_a_diverted_landing_when_preflight_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A race the pre-flight cannot see (the occupant appears between the check and
    the move) still diverts inside beets; the carry keys off the ACTUAL landing, so
    the diverted file keeps ITS lyrics. Simulated by blinding the pre-flight."""
    monkeypatch.setattr(reorg, "_collisions", lambda lib, dests: [])
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))
    base = music / "junk" / "col"
    base.mkdir(parents=True)
    audio_to_lyrics = {b"\x00A": "lyrics-A\n", b"\x00B": "lyrics-B\n"}
    items = []
    for fname, (audio_bytes, lyrics) in zip(
        ("01 Song.mp3", "01 Song other.mp3"), audio_to_lyrics.items(), strict=True
    ):
        f = base / fname
        f.write_bytes(audio_bytes)
        f.with_suffix(".lrc").write_text(lyrics, encoding="utf-8")
        it = Item(album="Collide", albumartist="X", artist="X", title="Song", track=1, disc=1)
        it.path = os.fsencode(str(f))
        items.append(it)
    lib.add_album(items).store()

    with lib.music_dir_context():
        outcome = reorg.reorganize_album(lib, next(iter(lib.albums())))

    assert outcome.status == "failed"  # the backstop still reports the divert
    dest = music / "X" / "Collide"
    # One item won the plain name, the diverted one landed at .1 — and each audio
    # file kept its OWN lyrics, paired by content.
    for audio_name, lrc_name in (
        ("01 Song.mp3", "01 Song.lrc"),
        ("01 Song.1.mp3", "01 Song.1.lrc"),
    ):
        audio_bytes = (dest / audio_name).read_bytes()
        assert (dest / lrc_name).read_text(encoding="utf-8") == audio_to_lyrics[audio_bytes]
    assert list(base.glob("*.lrc")) == []  # nothing stranded behind


def test_reorganize_album_collision_error_caps_its_detail_rows(tmp_path: Path) -> None:
    """A whole-folder collision produces one detail per track; the failure message
    stays bounded while the preview keeps every collision."""
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))
    base = music / "junk" / "many"
    base.mkdir(parents=True)
    items = []
    for n in range(4):  # 4 duplicated titles -> 4 intra-unit collisions
        for side in ("a", "b"):
            f = base / f"{side}{n}.mp3"
            f.write_bytes(f"\x00{side}{n}".encode())
            it = Item(album="Many", albumartist="X", artist="X", title=f"S{n}", track=n + 1)
            it.path = os.fsencode(str(f))
            items.append(it)
    lib.add_album(items).store()

    plan = reorg.plan_reorganize(lib, scope="library", artist=None, album_id=None)
    with lib.music_dir_context():
        outcome = reorg.reorganize_album(lib, next(iter(lib.albums())))

    assert outcome.status == "failed"
    error = outcome.error or ""
    assert error.count("resolve to this same name") == reorg.COLLISION_ERROR_CAP
    assert "1 more" in error
    assert len(plan.conflicts[0].collisions) == 4  # the preview keeps them all


def test_verify_moves_divert_message_states_only_what_it_knows() -> None:
    """The backstop still fires on a divert the pre-flight cannot see (a race), but
    it only knows the name was taken and where the file landed — NOT why."""
    problems = reorg._verify_moves(
        [(1, b"/m/junk/raw.mp3", b"/m/X/A/01 Song.mp3")], {1: b"/m/X/A/01 Song.1.mp3"}
    )
    assert len(problems) == 1
    assert "01 Song.1.mp3" in problems[0]
    assert "already taken" in problems[0]
    assert "two tracks share the same track number and title" not in problems[0]


# --- lyric sidecars follow the audio ------------------------------------------
#
# beets moves audio + album art only. MusicDrop's own .lrc/.txt sidecars (the
# files Plex actually reads) are named after the AUDIO STEM, so every move and
# every in-place rename orphans them: they stay in the vacated folder, the
# post-run orphan sweep classifies it as an audio-empty husk, and the lyrics go
# to Trash while the job reports success.


def _seed_flac_album(
    lib: Library,
    music: Path,
    *,
    folder: str,
    artist: str,
    album: str,
    titles: list[str],
) -> Album:
    """One album of REAL flac files under ``folder``, filenames a.flac/b.flac/...

    Real audio (copies of the silent.flac fixture) and a real ``lib.add_album`` so
    the move under test is beets' own, unmocked. The raw stems guarantee every
    track's destination filename differs from its current one.
    """
    base = music / folder
    base.mkdir(parents=True, exist_ok=True)
    items = []
    for i, title in enumerate(titles, start=1):
        f = base / f"{chr(ord('a') + i - 1)}.flac"
        shutil.copyfile(SAMPLE_FLAC, f)
        it = Item(album=album, albumartist=artist, artist=artist, title=title, track=i, disc=1)
        it.path = os.fsencode(str(f))
        items.append(it)
    return lib.add_album(items)


def _ir_lib(tmp_path: Path) -> tuple[Library, Path, Album]:
    """(lib, music_dir, album) with a mis-filed 3-track In Rainbows in junk/ir."""
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))
    album = _seed_flac_album(
        lib,
        music,
        folder="junk/ir",
        artist="Radiohead",
        album="In Rainbows",
        titles=["15 Step", "Bodysnatchers", "Nude"],
    )
    return lib, music, album


def test_reorganize_album_carries_lyric_sidecars(tmp_path: Path) -> None:
    """Folder move: each track's .lrc/.txt lands beside it under its NEW name."""
    lib, music, album = _ir_lib(tmp_path)
    src = music / "junk" / "ir"
    (src / "a.lrc").write_text("[00:01.00] step\n", encoding="utf-8")
    (src / "b.txt").write_text("bodysnatchers\n", encoding="utf-8")

    with lib.music_dir_context():
        outcome = reorg.reorganize_album(lib, album)

    assert outcome.status == "moved"
    dest = music / "Radiohead" / "In Rainbows"
    assert (dest / "01 15 Step.lrc").read_text(encoding="utf-8") == "[00:01.00] step\n"
    assert (dest / "02 Bodysnatchers.txt").read_text(encoding="utf-8") == "bodysnatchers\n"
    # Track 3 had no sidecar: none is invented for it.
    assert not (dest / "03 Nude.lrc").exists()
    assert not (dest / "03 Nude.txt").exists()
    # Vacated folder is gone, so the orphan sweep has no husk to trash.
    assert not src.exists()


def test_reorganize_album_rename_in_place_carries_sidecars(tmp_path: Path) -> None:
    """Same directory, filenames only: the sidecar must be renamed alongside."""
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))
    album = _seed_flac_album(
        lib,
        music,
        folder="Boards of Canada/Geogaddi",
        artist="Boards of Canada",
        album="Geogaddi",
        titles=["Ready Lets Go", "Music Is Math"],
    )
    base = music / "Boards of Canada" / "Geogaddi"
    (base / "a.lrc").write_text("[00:02.00] ready\n", encoding="utf-8")

    with lib.music_dir_context():
        outcome = reorg.reorganize_album(lib, album)

    assert outcome.status == "moved"
    assert (base / "01 Ready Lets Go.lrc").read_text(encoding="utf-8") == "[00:02.00] ready\n"
    assert not (base / "a.lrc").exists()
    # The album's own folder still holds its audio — the re-prune never eats a live dir.
    assert (base / "01 Ready Lets Go.flac").exists()


def test_reorganize_singleton_carries_lyric_sidecars(tmp_path: Path) -> None:
    """The singleton path moves sidecars too — both extensions when both exist."""
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))
    loose = music / "loose"
    loose.mkdir(parents=True)
    audio = loose / "z.flac"
    shutil.copyfile(SAMPLE_FLAC, audio)
    item = Item(artist="Aphex Twin", albumartist="Aphex Twin", title="Xtal", track=1)
    item.path = os.fsencode(str(audio))
    lib.add(item)
    (loose / "z.lrc").write_text("[00:03.00] xtal\n", encoding="utf-8")
    (loose / "z.txt").write_text("xtal plain\n", encoding="utf-8")

    with lib.music_dir_context():
        outcome = reorg.reorganize_singleton(lib, item)

    assert outcome.status == "moved"
    dest = music / "Non-Album" / "Aphex Twin"  # beets' built-in singleton: format
    assert (dest / "Xtal.lrc").read_text(encoding="utf-8") == "[00:03.00] xtal\n"
    assert (dest / "Xtal.txt").read_text(encoding="utf-8") == "xtal plain\n"
    assert not loose.exists()


def test_reorganize_album_sidecar_move_never_clobbers(tmp_path: Path) -> None:
    """A sidecar already at the destination wins; the source one is left in place
    (two different lyrics for what is now the same track name — losing either
    silently is worse than leaving one behind where the user can see it)."""
    lib, music, album = _ir_lib(tmp_path)
    src = music / "junk" / "ir"
    (src / "a.lrc").write_text("SOURCE\n", encoding="utf-8")
    dest = music / "Radiohead" / "In Rainbows"
    dest.mkdir(parents=True)
    (dest / "01 15 Step.lrc").write_text("KEEP\n", encoding="utf-8")

    with lib.music_dir_context():
        outcome = reorg.reorganize_album(lib, album)

    assert outcome.status == "moved"  # the audio move is unaffected
    assert (dest / "01 15 Step.lrc").read_text(encoding="utf-8") == "KEEP\n"
    assert (src / "a.lrc").read_text(encoding="utf-8") == "SOURCE\n"


def test_reorganize_album_sidecar_failure_does_not_fail_the_audio_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An OSError moving a sidecar is logged, never raised: the audio has already
    moved, so the unit's outcome must stay truthful about the audio."""
    lib, music, album = _ir_lib(tmp_path)
    src = music / "junk" / "ir"
    (src / "a.lrc").write_text("[00:01.00] step\n", encoding="utf-8")

    def boom(*a: object, **k: object) -> None:
        raise OSError("read-only file system")

    # beets' own util.move uses os.replace (+ copyfileobj), never shutil.move, so
    # this breaks ONLY the sidecar move.
    monkeypatch.setattr(shutil, "move", boom)

    with caplog.at_level(logging.WARNING, logger="app.beets.sidecars"):
        with lib.music_dir_context():
            outcome = reorg.reorganize_album(lib, album)

    assert outcome.status == "moved"
    assert (music / "Radiohead" / "In Rainbows" / "01 15 Step.flac").exists()
    assert (src / "a.lrc").exists()  # left behind, not destroyed
    assert "a.lrc" in caplog.text
    assert any(r.exc_info for r in caplog.records)  # the traceback reaches the logs


def test_reorganize_album_leaves_non_sidecar_files_alone(tmp_path: Path) -> None:
    """Only stem-matched .lrc/.txt travel: art, an unrelated sidecar and any other
    file stay exactly where they are."""
    lib, music, album = _ir_lib(tmp_path)
    src = music / "junk" / "ir"
    (src / "a.lrc").write_text("[00:01.00] step\n", encoding="utf-8")
    (src / "scan.jpg").write_bytes(b"\xff\xd8")
    (src / "unrelated.lrc").write_text("not a track here\n", encoding="utf-8")

    with lib.music_dir_context():
        outcome = reorg.reorganize_album(lib, album)

    assert outcome.status == "moved"
    dest = music / "Radiohead" / "In Rainbows"
    assert (dest / "01 15 Step.lrc").exists()
    assert (src / "scan.jpg").exists()
    assert (src / "unrelated.lrc").exists()
    assert not (dest / "scan.jpg").exists()
    assert not (dest / "unrelated.lrc").exists()


def test_reorganize_album_leaves_a_non_moving_items_sidecar_alone(tmp_path: Path) -> None:
    """A track beets silently skipped (source file gone) keeps its sidecar where it
    is, while its album-mates' sidecars still travel — the carry runs even when
    verification is about to report the unit failed."""
    lib, music, album = _ir_lib(tmp_path)
    src = music / "junk" / "ir"
    (src / "a.lrc").write_text("stranded\n", encoding="utf-8")
    (src / "b.lrc").write_text("travels\n", encoding="utf-8")
    os.remove(src / "a.flac")  # beets 2.13 skips this item's move, no exception

    with lib.music_dir_context():
        outcome = reorg.reorganize_album(lib, album)

    assert outcome.status == "failed"  # the missing file is reported, not hidden
    assert (src / "a.lrc").read_text(encoding="utf-8") == "stranded\n"
    dest = music / "Radiohead" / "In Rainbows"
    assert (dest / "02 Bodysnatchers.lrc").read_text(encoding="utf-8") == "travels\n"


def test_move_sidecars_same_path_is_a_noop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An item whose path did not change must not even look like a collision —
    a naive implementation would see the sidecar at its own destination and log a
    bogus 'destination exists' warning on every run."""
    from app.beets.sidecars import move_sidecars

    audio = tmp_path / "01 Song.flac"
    audio.write_bytes(b"\x00")
    (tmp_path / "01 Song.lrc").write_text("[00:01.00] la\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="app.beets.sidecars"):
        assert move_sidecars(str(audio), str(audio)) == []

    assert (tmp_path / "01 Song.lrc").exists()
    assert caplog.records == []


def test_reorganize_album_crash_midway_still_carries_moved_items_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Album.move moves+stores one item at a time, and beets commits the
    transaction even while an exception propagates — so items moved before a
    mid-album crash are permanently re-pathed and never re-enter ``pending`` on a
    later run. Their sidecars must be carried from the except path, or they are
    stranded for good and the orphan sweep eventually trashes them."""
    from beets import util as beets_util

    lib, music, album = _ir_lib(tmp_path)
    src = music / "junk" / "ir"
    for stem in ("a", "b", "c"):
        (src / f"{stem}.lrc").write_text(f"{stem}-lyrics\n", encoding="utf-8")

    real_move = beets_util.move
    calls = {"n": 0}

    def move_then_die(sour: bytes, dest: bytes, replace: bool = False) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise beets_util.FilesystemError(
                OSError(28, "No space left on device"), "move", (sour, dest)
            )
        real_move(sour, dest, replace)

    # Breaks the SECOND audio move only; sidecars go through shutil.move.
    monkeypatch.setattr(beets_util, "move", move_then_die)

    with lib.music_dir_context():
        outcome = reorg.reorganize_album(lib, album)

    assert outcome.status == "failed"  # the crash is reported, not hidden
    dest = music / "Radiohead" / "In Rainbows"
    moved_audio = [p for p in dest.glob("*.flac")] if dest.exists() else []
    assert len(moved_audio) == 1  # exactly one item moved before the crash
    # THE point: the moved item's sidecar followed it despite the crash...
    lrc = moved_audio[0].with_suffix(".lrc")
    assert lrc.exists()
    # ...and the unmoved items keep audio + sidecars intact at the old location.
    assert len(list(src.glob("*.flac"))) == 2
    assert len(list(src.glob("*.lrc"))) == 2


def test_reorganize_album_collision_refusal_leaves_sidecars_in_place(tmp_path: Path) -> None:
    """The refusal happens before anything moves, so the sidecar carry must not run
    at all: every .lrc stays beside its own (unmoved) audio file."""
    lib, music, base = _collide_pair_lib(tmp_path)
    for fname, lyrics in (("01 Song.mp3", "lyrics-A\n"), ("01 Song other.mp3", "lyrics-B\n")):
        (base / fname).with_suffix(".lrc").write_text(lyrics, encoding="utf-8")
    before = _tree(music)

    with lib.music_dir_context():
        outcome = reorg.reorganize_album(lib, next(iter(lib.albums())))

    assert outcome.status == "failed"
    assert _tree(music) == before  # audio AND lyrics untouched
    assert not (music / "X").exists()
