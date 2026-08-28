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
from app.models.reorganize import ReorganizeConflict, ReorganizeMove
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
    assert plan.total == 1
    assert plan.will_move == 1


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
        assert "01 Song other.mp3" in error  # BOTH tracks named
        assert "'Song'" in error  # BOTH tracks named
        # The real emitted form (collisions_by_dest) names the two colliding tracks;
        # "track number and title" was unproducible by any message, so it pins nothing.
        assert "tracks resolve to this same name" in error
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
        assert plan.conflicts_total == 1
        assert plan.will_move == 0
        # Classified by the ALL-items duplicate check, NOT as a foreign occupant:
        # the settled twin is a unit-mate, so the on-disk arm deliberately exempts
        # it (that exemption is what lets a genuine swap self-heal) and only the
        # duplicate-destination arm is left to catch this.
        assert [c.kind for c in plan.conflicts[0].collisions] == ["intra_unit"]
        assert "2 tracks resolve to this same name" in (outcome.error or "")


def test_a_destination_tripping_both_arms_is_reported_once_as_intra_unit(
    tmp_path: Path,
) -> None:
    """One destination can trip both arms at once: two tracks render onto it AND a
    stranger already holds the name. It must yield ONE row, the intra-unit one —
    naming both tracks is what the user can act on, and a second row for the same
    destination would double every message that quotes it."""
    lib, music, _base = _collide_pair_lib(tmp_path)
    squat = music / "X" / "Collide"
    squat.mkdir(parents=True)
    (squat / "01 Song.mp3").write_bytes(b"\x00SQUAT")  # unknown to the library

    plan = reorg.plan_reorganize(lib, scope="library", artist=None, album_id=None)

    assert plan.conflicts_total == 1
    collisions = plan.conflicts[0].collisions
    assert [c.kind for c in collisions] == ["intra_unit"]
    assert "2 tracks resolve to this same name" in collisions[0].detail


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


def test_reorganize_album_refuses_when_a_stray_file_holds_the_art_name(
    reorganize_lib: Library,
) -> None:
    """A stray file already holds the name the album art would be moved to.

    Pre-fix (2026-08-28): outcome was status "moved" and the real art was silently
    diverted to ``cover.1.jpg`` beside the untouched stray ``cover.jpg``.
    """
    lib = reorganize_lib
    music = Path(os.fsdecode(lib.directory))
    with lib.music_dir_context():
        album = _album(lib, "In Rainbows")
        art = music / "junk" / "ir" / "cover.jpg"
        art.write_bytes(b"real art")
        album.artpath = os.fsencode(str(art))
        album.store()
    dest_dir = music / "Radiohead" / "In Rainbows"
    dest_dir.mkdir(parents=True)
    (dest_dir / "cover.jpg").write_bytes(b"stray")
    outcome = reorg.reorganize_album(lib, _album(lib, "In Rainbows"))
    assert outcome.status == "failed"
    assert "cover.jpg" in (outcome.error or "")
    # nothing moved: items, art and the stray all untouched
    assert (music / "junk" / "ir" / "a.mp3").exists()
    assert art.exists()
    assert (dest_dir / "cover.jpg").read_bytes() == b"stray"
    with lib.music_dir_context():
        assert bytes(_album(lib, "In Rainbows").artpath or b"") == os.fsencode(str(art))


def test_preview_counts_an_art_collision_as_a_conflict(reorganize_lib: Library) -> None:
    """The same setup as the refusal test, seen through the PREVIEW: the unit is a
    conflict (with an ``art`` row), not an ordinary move, and the plan's counts say
    so — ``will_move`` drops by one, ``conflicts_total`` gains one."""
    lib = reorganize_lib
    music = Path(os.fsdecode(lib.directory))
    with lib.music_dir_context():
        album = _album(lib, "In Rainbows")
        art = music / "junk" / "ir" / "cover.jpg"
        art.write_bytes(b"real art")
        album.artpath = os.fsencode(str(art))
        album.store()
    dest_dir = music / "Radiohead" / "In Rainbows"
    dest_dir.mkdir(parents=True)
    (dest_dir / "cover.jpg").write_bytes(b"stray")

    with lib.music_dir_context():
        row = _describe_album(lib, _album(lib, "In Rainbows"))
        assert isinstance(row, ReorganizeConflict)
        assert any(c.kind == "art" for c in row.collisions)

    plan = plan_reorganize(lib, scope="library", artist=None, album_id=None)
    # In Rainbows under conflicts, not moves (cf. test_plan_library_counts).
    assert plan.will_move == 2
    assert plan.conflicts_total == 1
    assert "Radiohead - In Rainbows" in [c.label for c in plan.conflicts]


def test_reorganize_album_refuses_an_alias_of_its_own_art_at_the_destination(
    reorganize_lib: Library,
) -> None:
    """A symlink to the album's own art squatting the destination still makes
    beets divert (move_art has no samefile guard), so it must refuse."""
    lib = reorganize_lib
    music = Path(os.fsdecode(lib.directory))
    with lib.music_dir_context():
        album = _album(lib, "In Rainbows")
        art = music / "junk" / "ir" / "cover.jpg"
        art.write_bytes(b"real art")
        album.artpath = os.fsencode(str(art))
        album.store()
    dest_dir = music / "Radiohead" / "In Rainbows"
    dest_dir.mkdir(parents=True)
    (dest_dir / "cover.jpg").symlink_to(art)
    outcome = reorg.reorganize_album(lib, _album(lib, "In Rainbows"))
    assert outcome.status == "failed"
    assert "cover.jpg" in (outcome.error or "")


def test_art_destination_alias_of_a_moving_item_is_exempted(reorganize_lib: Library) -> None:
    """The samefile half of the vacating exemption: the stray at the predicted
    art name is a SYMLINK to a file that this unit itself relocates (a.mp3 ->
    01 15 Step.mp3) — byte-unequal to every moving path, samefile-equal to one.
    The alias's target vacates during the item moves, so by the time the art
    moves the destination is a dangling symlink (os.path.exists: False), beets
    takes the name and no divert happens."""
    lib = reorganize_lib
    music = Path(os.fsdecode(lib.directory))
    with lib.music_dir_context():
        album = _album(lib, "In Rainbows")
        art = music / "junk" / "ir" / "cover.jpg"
        art.write_bytes(b"real art")
        album.artpath = os.fsencode(str(art))
        album.store()
        first = next(i for i in album.items())
        assert os.path.basename(os.fsdecode(first.path)) == "a.mp3"  # the alias target
    dest_dir = music / "Radiohead" / "In Rainbows"
    dest_dir.mkdir(parents=True)
    (dest_dir / "cover.jpg").symlink_to(art.parent / "a.mp3")
    outcome = reorg.reorganize_album(lib, _album(lib, "In Rainbows"))
    assert outcome.status == "moved"
    with lib.music_dir_context():
        final_art = _album(lib, "In Rainbows").artpath
        assert final_art is not None
        assert os.path.basename(final_art) == b"cover.jpg"


def test_art_divert_that_slips_past_the_preflight_is_reported(
    monkeypatch: pytest.MonkeyPatch, reorganize_lib: Library
) -> None:
    """A divert that lands between pre-flight and move is caught by the backstop:
    the items DO move (mutation happens), then the failure reports honestly.
    Simulated by silencing the pre-flight (monkeypatch, as in
    test_reorganize_album_failure_is_caught)."""
    lib = reorganize_lib
    music = Path(os.fsdecode(lib.directory))
    with lib.music_dir_context():
        album = _album(lib, "In Rainbows")
        art = music / "junk" / "ir" / "cover.jpg"
        art.write_bytes(b"real art")
        album.artpath = os.fsencode(str(art))
        album.store()
    dest_dir = music / "Radiohead" / "In Rainbows"
    dest_dir.mkdir(parents=True)
    (dest_dir / "cover.jpg").write_bytes(b"stray")
    expected = os.path.normpath(os.fsencode(str(dest_dir / "cover.jpg")))
    monkeypatch.setattr(reorg, "art_preflight", lambda *a, **k: reorg.ArtPreflight(None, expected))
    outcome = reorg.reorganize_album(lib, _album(lib, "In Rainbows"))
    assert outcome.status == "failed"
    assert "cover.1.jpg" in (outcome.error or "")


def test_reorganize_album_without_art_moves_despite_stray_occupant(
    reorganize_lib: Library,
) -> None:
    """No artpath: a stray file holding the destination art name is irrelevant —
    there is no art to divert, so the move goes through."""
    lib = reorganize_lib
    music = Path(os.fsdecode(lib.directory))
    dest_dir = music / "Radiohead" / "In Rainbows"
    dest_dir.mkdir(parents=True)
    (dest_dir / "cover.jpg").write_bytes(b"stray")
    outcome = reorg.reorganize_album(lib, _album(lib, "In Rainbows"))
    assert outcome.status == "moved"


def test_reorganize_album_with_missing_art_moves_and_beets_clears_the_ref(
    reorganize_lib: Library,
) -> None:
    """artpath points at a file that does not exist: beets clears the dangling
    ref itself during the move — that is not a divert, so the move goes through
    and the re-fetched artpath is falsy."""
    lib = reorganize_lib
    music = Path(os.fsdecode(lib.directory))
    with lib.music_dir_context():
        album = _album(lib, "In Rainbows")
        album.artpath = os.fsencode(str(music / "junk" / "ir" / "missing.jpg"))
        album.store()
    outcome = reorg.reorganize_album(lib, _album(lib, "In Rainbows"))
    assert outcome.status == "moved"
    with lib.music_dir_context():
        assert not _album(lib, "In Rainbows").artpath


def test_missing_art_with_a_stray_at_the_computed_name_still_moves(
    reorganize_lib: Library,
) -> None:
    """The guard's real job: a DANGLING artpath plus a stray at the computed art
    name. beets would move cleanly and just drop the dead ref; without the
    missing-file early return the stray reads as an art collision and the album
    is refused on every sweep forever."""
    lib = reorganize_lib
    music = Path(os.fsdecode(lib.directory))
    with lib.music_dir_context():
        album = _album(lib, "In Rainbows")
        # File NOT created — the ref dangles. The extension is what
        # art_destination takes from it, so the computed name is cover.jpg.
        album.artpath = os.fsencode(str(music / "junk" / "ir" / "missing.jpg"))
        album.store()
    dest_dir = music / "Radiohead" / "In Rainbows"
    dest_dir.mkdir(parents=True)
    (dest_dir / "cover.jpg").write_bytes(b"stray")
    outcome = reorg.reorganize_album(lib, _album(lib, "In Rainbows"))
    assert outcome.status == "moved"
    with lib.music_dir_context():
        assert not _album(lib, "In Rainbows").artpath


def test_reorganize_album_in_place_rename_keeps_its_art(reorganize_lib: Library) -> None:
    """Geogaddi renames in place (right dir, wrong filenames): its art sits at
    the SAME path before and after, so there is no divert — move goes through,
    artpath unchanged, no cover.1.jpg anywhere in the dir."""
    lib = reorganize_lib
    music = Path(os.fsdecode(lib.directory))
    dest_dir = music / "Boards of Canada" / "Geogaddi"
    art = dest_dir / "cover.jpg"
    art.write_bytes(b"real art")
    with lib.music_dir_context():
        album = _album(lib, "Geogaddi")
        album.artpath = os.fsencode(str(art))
        album.store()
    outcome = reorg.reorganize_album(lib, _album(lib, "Geogaddi"))
    assert outcome.status == "moved"
    with lib.music_dir_context():
        assert bytes(_album(lib, "Geogaddi").artpath or b"") == os.fsencode(str(art))
    assert not (dest_dir / "cover.1.jpg").exists()


def test_reorganize_album_vacating_item_vacates_the_art_name(reorganize_lib: Library) -> None:
    """A fourth In Rainbows item currently SITS AT the destination art name
    (Radiohead/In Rainbows/cover.jpg) and moves to '04 Hidden.jpg', so it
    vacates the name before the art moves — the occupant is this unit's own
    file, not a stranger, so the move goes through and the art lands at
    cover.jpg (no .1 divert)."""
    lib = reorganize_lib
    music = Path(os.fsdecode(lib.directory))
    with lib.music_dir_context():
        album = _album(lib, "In Rainbows")
        art = music / "junk" / "ir" / "cover.jpg"
        art.write_bytes(b"real art")
        album.artpath = os.fsencode(str(art))
        album.store()
    dest_dir = music / "Radiohead" / "In Rainbows"
    dest_dir.mkdir(parents=True)
    occupant = dest_dir / "cover.jpg"
    occupant.write_bytes(b"\x00")
    it = Item(
        album="In Rainbows",
        albumartist="Radiohead",
        artist="Radiohead",
        title="Hidden",
        track=4,
        disc=1,
    )
    it.path = os.fsencode(str(occupant))
    it.album_id = album.id
    lib.add(it)
    it.store()
    outcome = reorg.reorganize_album(lib, _album(lib, "In Rainbows"))
    assert outcome.status == "moved"
    with lib.music_dir_context():
        assert bytes(_album(lib, "In Rainbows").artpath or b"") == os.fsencode(str(occupant))
    assert not (dest_dir / "cover.1.jpg").exists()


def test_reorganize_heals_a_previously_diverted_art_name(reorganize_lib: Library) -> None:
    """Pins the upstream fact the whole fix leans on: art_destination rebuilds the
    name from the template and DISCARDS a diverted `.1` base, so move_art's
    byte-equal early return never fires on a diverted name and a clean move
    renames cover.1.jpg back to cover.jpg."""
    lib = reorganize_lib
    music = Path(os.fsdecode(lib.directory))
    with lib.music_dir_context():
        album = _album(lib, "In Rainbows")
        art = music / "junk" / "ir" / "cover.1.jpg"
        art.write_bytes(b"real art")
        album.artpath = os.fsencode(str(art))
        album.store()
    # destination dir does not exist yet: a fully clean move
    outcome = reorg.reorganize_album(lib, _album(lib, "In Rainbows"))
    assert outcome.status == "moved"
    with lib.music_dir_context():
        artpath = _album(lib, "In Rainbows").artpath
        assert artpath is not None
        assert os.path.basename(bytes(artpath)) == b"cover.jpg"
    assert not (music / "Radiohead" / "In Rainbows" / "cover.1.jpg").exists()


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
    assert plan.conflicts_total == 0
    assert plan.will_move == 1

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
    # _verify_moves has exactly three forms (not-found / did-not-take-effect /
    # diverted); this case emits the divert one, and "two tracks share the same
    # track number and title" was unproducible by any of them — so pin the exact
    # emitted text instead of a dead absence needle.
    assert problems[0] == (
        "raw.mp3: the computed file name was already taken on disk, so the file"
        " landed at '01 Song.1.mp3'; check this album's folder for what is holding that name"
    )


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


def test_art_preflight_reports_the_expected_destination_on_a_free_name(
    reorganize_lib: Library,
) -> None:
    """The free-name branch must still return expected_dest — it is what arms
    the post-move backstops; (None, None) here would disarm them silently."""
    lib = reorganize_lib
    music = Path(os.fsdecode(lib.directory))
    with lib.music_dir_context():
        album = _album(lib, "In Rainbows")
        art = music / "junk" / "ir" / "cover.jpg"
        art.parent.mkdir(parents=True, exist_ok=True)
        art.write_bytes(b"real art")
        album.artpath = os.fsencode(str(art))
        album.store()
        dests = reorg._unit_dests(lib, list(album.items()))
        result = reorg.art_preflight(lib, album, dests)
    assert result.collision is None
    assert result.expected_dest is not None
    assert os.path.basename(result.expected_dest) == b"cover.jpg"
    assert os.fsdecode(result.expected_dest).startswith(str(music / "Radiohead" / "In Rainbows"))


def test_reorganize_art_prediction_skips_a_missing_first_mover(tmp_path: Path) -> None:
    """A two-disc album whose FIRST mover's source file is missing: beets'
    Item.move silently skips it, so Album.move's "first item whose path changed"
    is the disc-2 track and the art follows disc 2.

    The prediction must skip the missing-source mover too — predicting disc 1's
    dir (a dir the art never visits) would make the post-move backstop report a
    false "the album art's computed name was already taken" for a CLEAN landing.
    The missing file itself is still reported (truthfully); the art message is
    not."""
    import os

    from beets.library import Item

    music = tmp_path / "music"
    lib = build_library(
        str(tmp_path / "library.db"),
        str(music),
        path_format="$albumartist/$album/Disc $disc/$track $title",
    )
    base = music / "old"
    base.mkdir(parents=True)
    items = []
    for disc in (1, 2):
        f = base / f"{disc} T.mp3"
        f.write_bytes(b"\x00")
        it = Item(album="Boxset", albumartist="Ann", artist="Ann", title="T", track=1, disc=disc)
        it.path = os.fsencode(str(f))
        items.append(it)
    album = lib.add_album(items)
    art = base / "cover.jpg"
    art.write_bytes(b"art")
    album.artpath = os.fsencode(str(art))
    album.store()
    # The FIRST mover's source is gone — beets skips it at move time.
    os.remove(os.fsdecode(bytes(items[0].path)))

    with lib.music_dir_context():
        outcome = reorg.reorganize_album(lib, album)

    assert outcome.status == "failed"  # the missing file is reported
    assert "file not found" in (outcome.error or "")
    # THE point: no false art divert — the landing was clean at the real target.
    assert "album art" not in (outcome.error or "")
    refetched = lib.get_album(_require_id(album.id))
    assert refetched is not None
    assert refetched.artpath is not None
    assert os.path.dirname(os.fsdecode(refetched.artpath)) == str(
        music / "Ann" / "Boxset" / "Disc 02"
    )


def test_reorganize_refuses_when_the_real_art_target_is_taken_and_first_mover_missing(
    tmp_path: Path,
) -> None:
    """First mover's source missing AND a stray holding the art name at the
    REAL target (disc 2, where the surviving mover lands the art): the
    prediction must skip the missing-source mover and find the collision,
    REFUSING the unit before anything moves — not predict disc 1 (free), let
    the move through, and only report the divert after the art has been
    silently renamed."""
    import os

    from beets.library import Item

    music = tmp_path / "music"
    lib = build_library(
        str(tmp_path / "library.db"),
        str(music),
        path_format="$albumartist/$album/Disc $disc/$track $title",
    )
    base = music / "old"
    base.mkdir(parents=True)
    items = []
    for disc in (1, 2):
        f = base / f"{disc} T.mp3"
        f.write_bytes(b"\x00")
        it = Item(album="Boxset", albumartist="Ann", artist="Ann", title="T", track=1, disc=disc)
        it.path = os.fsencode(str(f))
        items.append(it)
    album = lib.add_album(items)
    art = base / "cover.jpg"
    art.write_bytes(b"art")
    album.artpath = os.fsencode(str(art))
    album.store()
    os.remove(os.fsdecode(bytes(items[0].path)))  # first mover gone
    disc2 = music / "Ann" / "Boxset" / "Disc 02"
    disc2.mkdir(parents=True)
    (disc2 / "cover.jpg").write_bytes(b"stray")

    with lib.music_dir_context():
        outcome = reorg.reorganize_album(lib, album)

    assert outcome.status == "failed"  # refused — not "moved with a divert"
    assert "album art" in (outcome.error or "")
    # THE point: the refusal happened BEFORE the move — the stray still holds
    # the name, no `.1` sibling was minted, and disc 2 never moved.
    assert (disc2 / "cover.jpg").read_bytes() == b"stray"
    assert not (disc2 / "cover.1.jpg").exists()
    refetched = lib.get_album(_require_id(album.id))
    assert refetched is not None
    assert refetched.artpath is not None
    assert os.path.dirname(os.fsdecode(refetched.artpath)) == str(base)


def test_reorganize_backstop_ignores_a_dir_only_art_misprediction(
    monkeypatch: pytest.MonkeyPatch, reorganize_lib: Library
) -> None:
    """If a residual misprediction ever leaves the art in a DIFFERENT DIR under
    the SAME name (basename unchanged — a unique_path divert always renames),
    that is a misprediction, not a taken name: the backstop must not report
    'the computed name was already taken' for a clean landing. Simulated by
    monkeypatching the pre-flight to a same-basename sibling dir (the same
    lever as test_art_divert_that_slips_past_the_preflight_is_reported, whose
    basename DOES differ)."""
    lib = reorganize_lib
    music = Path(os.fsdecode(lib.directory))
    with lib.music_dir_context():
        album = _album(lib, "In Rainbows")
        art = music / "junk" / "ir" / "cover.jpg"
        art.write_bytes(b"real art")
        album.artpath = os.fsencode(str(art))
        album.store()
    # Same basename (cover.jpg), different dir than the real landing.
    mispredicted = os.path.normpath(
        os.fsencode(str(music / "Radiohead" / "In Rainbows (old)" / "cover.jpg"))
    )
    monkeypatch.setattr(
        reorg, "art_preflight", lambda *a, **k: reorg.ArtPreflight(None, mispredicted)
    )
    outcome = reorg.reorganize_album(lib, _album(lib, "In Rainbows"))
    assert outcome.status == "moved"
    assert "album art" not in (outcome.error or "")


def _two_disc_lib(tmp_path: Path) -> tuple[Library, int, list[Item], Path]:
    """The D1 two-disc library (disc 1 = 'One', disc 2 = 'Two', art in
    music/old/). The callers settle disc 1 themselves, under
    ``lib.music_dir_context()`` — destination rendering needs it."""
    music = tmp_path / "music"
    lib = build_library(
        str(tmp_path / "library.db"),
        str(music),
        path_format="$albumartist/$album/Disc $disc/$track $title",
    )
    base = music / "old"
    base.mkdir(parents=True)
    items = []
    for disc, title in ((1, "One"), (2, "Two")):
        f = base / f"{disc} {title}.mp3"
        f.write_bytes(b"\x00")
        it = Item(
            album="Boxset",
            albumartist="Ann",
            artist="Ann",
            title=title,
            track=1,
            disc=disc,
        )
        it.path = os.fsencode(str(f))
        items.append(it)
    album = lib.add_album(items)
    art = base / "cover.jpg"
    art.write_bytes(b"art")
    album.artpath = os.fsencode(str(art))
    album.store()
    return lib, _require_id(album.id), items, music


def _settle_disc1(lib: Library, items: list[Item]) -> None:
    """Physically settle disc 1 at its destination: rename the file, update
    and store ``item.path``. Caller is under ``lib.music_dir_context()``."""
    settled = bytes(items[0].destination(basedir=lib.directory))
    os.makedirs(os.path.dirname(settled), exist_ok=True)
    os.rename(os.fsdecode(bytes(items[0].path)), os.fsdecode(settled))
    items[0].path = settled
    items[0].store()


def test_reorganize_art_follows_the_settled_albums_actual_mover(tmp_path: Path) -> None:
    """Disc 1 already sits at its destination; only disc 2 moves, so the art
    targets DISC 2's dir. A stray cover.jpg there must refuse the unit BEFORE
    anything moves — and a prediction read from the raw first dest (disc 1)
    would miss it, move the files, and divert the art."""
    lib, aid, items, music = _two_disc_lib(tmp_path)
    with lib.music_dir_context():
        _settle_disc1(lib, items)  # disc 1 is already at its destination
        disc2 = music / "Ann" / "Boxset" / "Disc 02"
        disc2.mkdir(parents=True)
        (disc2 / "cover.jpg").write_bytes(b"stray")  # holds the art's real target
        before = _tree(music)
        album = lib.get_album(aid)
        assert album is not None
        outcome = reorg.reorganize_album(lib, album)

    assert outcome.status == "failed"  # refused — not "moved with a divert"
    assert "cover.jpg" in (outcome.error or "")
    # THE point: the refusal happened BEFORE the move — the whole tree, the
    # stray and the settled disc-1 file included, is byte-identical.
    assert _tree(music) == before
    assert not (disc2 / "cover.1.jpg").exists()  # no divert was minted


def test_reorganize_ignores_a_stray_in_a_dir_the_art_never_visits(tmp_path: Path) -> None:
    """Mirror: the stray sits in DISC 1's dir — settled, not receiving the art.
    The unit must move; the art lands beside disc 2. A prediction read from
    the raw first dest (disc 1) would find the stray and REFUSE a clean move."""
    lib, aid, items, music = _two_disc_lib(tmp_path)
    with lib.music_dir_context():
        _settle_disc1(lib, items)  # disc 1 is already at its destination
        disc1 = music / "Ann" / "Boxset" / "Disc 01"
        (disc1 / "cover.jpg").write_bytes(b"stray")  # dir the art never visits
        album = lib.get_album(aid)
        assert album is not None
        outcome = reorg.reorganize_album(lib, album)

    assert outcome.status == "moved"
    assert "album art" not in (outcome.error or "")  # no false art detail
    refetched = lib.get_album(aid)
    assert refetched is not None
    assert refetched.artpath is not None
    assert os.path.dirname(os.fsdecode(refetched.artpath)) == str(
        music / "Ann" / "Boxset" / "Disc 02"
    )
