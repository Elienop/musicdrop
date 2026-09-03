from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from beets.library import Album, Item, Library

from app.beets.reorganize import plan_reorganize, reorganize_album, reorganize_singleton


def test_reorganize_album_sets_source_dir_on_move(reorganize_lib: Library) -> None:
    outcomes = [reorganize_album(reorganize_lib, a) for a in reorganize_lib.albums()]
    movers = [o for o in outcomes if o.status == "moved"]
    assert movers, "reorganize_lib should contain at least one misfiled album"
    assert all(m.source_dir for m in movers)  # moved outcomes carry their pre-move dir


def test_plan_lists_orphans(reorganize_lib: Library, tmp_path: Path) -> None:
    music_dir = Path(os.fsdecode(reorganize_lib.directory))
    husk = music_dir / "Ghost Artist"
    husk.mkdir(parents=True, exist_ok=True)
    (husk / "artist-poster.jpg").write_bytes(b"x")
    trash = tmp_path / "trash"
    plan = plan_reorganize(
        reorganize_lib, scope="library", artist=None, album_id=None, trash_dir=trash
    )
    assert any(o.name == "Ghost Artist" for o in plan.orphans)
    assert plan.orphans_total >= 1


def test_plan_without_trash_dir_has_no_orphans(reorganize_lib: Library) -> None:
    # Back-compat: existing callers pass no trash_dir -> orphan preview inactive.
    plan = plan_reorganize(reorganize_lib, scope="library", artist=None, album_id=None)
    assert plan.orphans == []
    assert plan.orphans_total == 0


def test_scoped_preview_has_no_orphans_premove(reorganize_lib: Library, tmp_path: Path) -> None:
    # A scoped (artist) preview cannot show husks: they form only during the run
    # (post-move). The actual sweep still cleans + reports them. Documents the design.
    music_dir = Path(os.fsdecode(reorganize_lib.directory))
    (music_dir / "Ghost").mkdir(parents=True, exist_ok=True)
    (music_dir / "Ghost" / "art.jpg").write_bytes(b"x")
    trash = tmp_path / "trash"
    plan = plan_reorganize(
        reorganize_lib, scope="artist", artist="Radiohead", album_id=None, trash_dir=trash
    )
    assert plan.orphans == []
    assert plan.orphans_total == 0


def test_reorganize_singleton_sets_source_dir(reorganize_lib: Library) -> None:
    outcomes = [
        reorganize_singleton(reorganize_lib, i)
        for i in reorganize_lib.items()
        if i.album_id is None
    ]
    movers = [o for o in outcomes if o.status == "moved"]
    # reorganize_lib includes a misfiled singleton; if present, its source_dir is set.
    for m in movers:
        assert m.source_dir


def test_registry_records_orphans() -> None:
    from app.reorganize_jobs.registry import ReorganizeRegistry

    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    reg.record_orphans(2)
    reg.record_orphans(1)
    reg.finish("done")
    assert reg.state().orphans_trashed == 3


def test_sweep_trashes_library_orphans(reorganize_lib: Library, tmp_path: Path) -> None:
    from app.reorganize_jobs.registry import ReorganizeRegistry
    from app.reorganize_jobs.runner import sweep
    from tests.conftest import make_test_handle, origins_for

    music_dir = Path(os.fsdecode(reorganize_lib.directory))
    husk = music_dir / "Ghost Artist"
    husk.mkdir(parents=True, exist_ok=True)
    (husk / "artist-poster.jpg").write_bytes(b"x")
    trash = tmp_path / "trash"

    handle = make_test_handle(reorganize_lib, tmp_path)
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    sweep(
        reg,
        handle,
        scope="library",
        trash_dir=trash,
        trash_origins_dir=origins_for(trash),
    )

    assert reg.state().orphans_trashed == 1
    assert not husk.exists()
    assert (trash / "Ghost Artist" / "artist-poster.jpg").exists()


# --- live_album_roots: the DB-side protected set ------------------------------
# Fixtures build their own library (never ``reorganize_lib``) because the shape
# under test is the PATH TEMPLATE: a discless template flattens a multi-disc album
# during the unit loop, so the album dir would gain direct audio and the sweep's
# has_own_audio guard would spare the art folder without any of this code.

_DISC_FORMAT = "$albumartist/$album/Disc $disc/$track $title"


def _library(tmp_path: Path, *, path_format: str) -> Library:
    from tests.conftest import build_library

    tmp_path.mkdir(parents=True, exist_ok=True)
    return build_library(
        str(tmp_path / "library.db"), str(tmp_path / "music"), path_format=path_format
    )


def _add_album(lib: Library, *, artist: str, album: str, files: list[tuple[str, int]]) -> Album:
    """Add an album whose items live at the given music-dir-relative paths.

    ``files`` is ``[(relative path, disc)]``; every file is a ``b"\\x00"`` stub so
    beets' move/destination machinery works without real audio.
    """
    music = Path(os.fsdecode(lib.directory))
    items = []
    for i, (rel, disc) in enumerate(files, start=1):
        f = music / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"\x00")
        it = Item(album=album, albumartist=artist, artist=artist, title=f"T{i}", track=i, disc=disc)
        it.path = os.fsencode(str(f))
        items.append(it)
    added = lib.add_album(items)
    added.store()
    return added


def test_live_album_roots_folds_a_multidisc_album_to_its_album_dir(tmp_path: Path) -> None:
    """The root is the commonpath of the item DIRS (``_album_root`` semantics), so a
    multi-disc album yields its ``$album`` folder — the dir whose art subfolders the
    sweep must spare. An album sitting AT the music root is dropped: its "root" is
    the whole library, which would protect everything."""
    from app.beets.reorganize import live_album_roots

    lib = _library(tmp_path, path_format=_DISC_FORMAT)
    music = Path(os.fsdecode(lib.directory))
    _add_album(
        lib,
        artist="Artist",
        album="Album",
        files=[("Artist/Album/Disc 01/01 T1.mp3", 1), ("Artist/Album/Disc 02/02 T2.mp3", 2)],
    )
    _add_album(lib, artist="Loose", album="Loose", files=[("loose.mp3", 1)])

    roots = live_album_roots(lib)

    assert str(music / "Artist" / "Album") in roots
    assert str(music) not in roots
    assert str(music / "Artist" / "Album" / "Disc 01") not in roots


def test_live_album_roots_skips_an_album_row_with_no_items(tmp_path: Path) -> None:
    """An album row carrying no items contributes nothing and raises nothing —
    ``os.path.commonpath([])`` is a ValueError, so the derivation must never reach it
    for such a row (grouping by the ITEMS is what keeps it out of reach)."""
    from app.beets.reorganize import live_album_roots

    lib = _library(tmp_path, path_format=_DISC_FORMAT)
    music = Path(os.fsdecode(lib.directory))
    _add_album(lib, artist="Artist", album="Album", files=[("Artist/Album/01 T1.mp3", 1)])
    empty = _add_album(lib, artist="Gone", album="Gone", files=[("Gone/Gone/01 T1.mp3", 1)])
    album_id = empty.id
    with lib.transaction():
        for item in list(empty.items()):
            # with_album=False keeps the row: the default prunes an album that just
            # lost its last item, which is why this state looks unreachable.
            item.remove(delete=False, with_album=False)
    assert album_id is not None
    assert lib.get_album(album_id) is not None  # the row is really there, itemless

    assert live_album_roots(lib) == frozenset({str(music / "Artist" / "Album")})


def test_live_album_roots_excludes_an_album_outside_the_music_dir(tmp_path: Path) -> None:
    """A row pointing outside the library (legacy import, moved share) must not
    protect anything: the set is only meaningful inside the swept tree."""
    from app.beets.reorganize import live_album_roots

    lib = _library(tmp_path, path_format=_DISC_FORMAT)
    outside = tmp_path / "elsewhere" / "Album"
    outside.mkdir(parents=True)
    (outside / "01.mp3").write_bytes(b"\x00")
    it = Item(album="Album", albumartist="Artist", artist="Artist", title="T", track=1, disc=1)
    it.path = os.fsencode(str(outside / "01.mp3"))
    lib.add_album([it]).store()

    assert live_album_roots(lib) == frozenset()


def test_live_album_roots_protects_the_album_dir_above_a_nested_disc_dir(tmp_path: Path) -> None:
    """Single-nested-disc gap: an album whose audio all sits in ONE subfolder folds
    to that subfolder, so the ``$album`` dir above it needs protecting too — it is
    where 'Scans (LP)' lives. Both templates are covered: the disc-less one puts the
    album's destination dir directly above the actual root, the disc-bearing one puts
    it one DISC level above the destination."""
    from app.beets.reorganize import live_album_roots

    lib = _library(tmp_path, path_format="$albumartist/$album/$track $title")
    music = Path(os.fsdecode(lib.directory))
    _add_album(lib, artist="Artist", album="Album", files=[("Artist/Album/CD1/01 T1.mp3", 1)])
    assert str(music / "Artist" / "Album") in live_album_roots(lib)

    disc_lib = _library(tmp_path / "d", path_format=_DISC_FORMAT)
    disc_music = Path(os.fsdecode(disc_lib.directory))
    _add_album(disc_lib, artist="Artist", album="Album", files=[("Artist/Album/CD1/01 T1.mp3", 1)])
    assert str(disc_music / "Artist" / "Album") in live_album_roots(disc_lib)


def test_live_album_roots_never_protects_a_dir_the_album_is_not_in(tmp_path: Path) -> None:
    """The control arm for the discriminator above. A misfiled album (its files are
    not under the ``$album`` folder the template names) extends protection to
    nothing: only dirs that strictly CONTAIN the album's actual root are taken.

    Two husks are the bait. The artist container must never enter the set or every
    husk beside a live album stops being swept; and the album's future destination
    dir — which here exists as a genuine art-only husk — must not be protected by a
    row whose files are somewhere else entirely."""
    from app.beets.reorganize import live_album_roots

    lib = _library(tmp_path, path_format=_DISC_FORMAT)
    music = Path(os.fsdecode(lib.directory))
    _add_album(lib, artist="Artist", album="Album", files=[("Artist/Wrong Name/CD1/01 T1.mp3", 1)])
    husk = music / "Artist" / "Album"  # where the template WANTS it: art only
    husk.mkdir(parents=True)
    (husk / "cover.jpg").write_bytes(b"x")

    roots = live_album_roots(lib)

    assert str(music / "Artist") not in roots
    assert str(husk) not in roots
    assert roots == frozenset({str(music / "Artist" / "Wrong Name" / "CD1")})


def test_live_album_roots_drops_a_root_that_contains_another_root(tmp_path: Path) -> None:
    """An album whose items are split across two folders (a half-finished move) folds
    to their common ancestor — here the ARTIST dir. Such a root swallows its
    siblings' albums, so it is dropped rather than allowed to shield every husk
    under that artist."""
    from app.beets.reorganize import live_album_roots

    lib = _library(tmp_path, path_format="$albumartist/$album/$track $title")
    music = Path(os.fsdecode(lib.directory))
    _add_album(
        lib,
        artist="Artist",
        album="Split",
        files=[("Artist/Half A/01 T1.mp3", 1), ("Artist/Half B/01 T2.mp3", 1)],
    )
    _add_album(lib, artist="Artist", album="Kept", files=[("Artist/Kept/01 T1.mp3", 1)])

    roots = live_album_roots(lib)

    assert str(music / "Artist") not in roots
    assert str(music / "Artist" / "Kept") in roots


def test_sweep_leaves_live_multidisc_scans_alone(tmp_path: Path) -> None:
    """End-to-end: the executor and the preview both spare a live multi-disc album's
    non-listed art folder while still trashing a genuine husk.

    The album is placed EXACTLY where the disc-bearing template wants it, so the unit
    loop moves nothing (asserted) — against a disc-less template beets would flatten
    the discs first, the album dir would gain direct audio, and ``has_own_audio``
    would spare 'Scans (LP)' with none of this code running."""
    from app.beets.reorganize import plan_reorganize
    from app.reorganize_jobs.registry import ReorganizeRegistry
    from app.reorganize_jobs.runner import sweep
    from tests.conftest import make_test_handle, origins_for

    lib = _library(tmp_path, path_format=_DISC_FORMAT)
    music = Path(os.fsdecode(lib.directory))
    _add_album(
        lib,
        artist="Artist",
        album="Album",
        files=[("Artist/Album/Disc 01/01 T1.mp3", 1), ("Artist/Album/Disc 02/02 T2.mp3", 2)],
    )
    scans = music / "Artist" / "Album" / "Scans (LP)"
    scans.mkdir(parents=True)
    (scans / "booklet.jpg").write_bytes(b"x")
    husk = music / "Ghost Artist"
    husk.mkdir(parents=True)
    (husk / "artist-poster.jpg").write_bytes(b"x")
    trash = tmp_path / "trash"

    plan = plan_reorganize(lib, scope="library", artist=None, album_id=None, trash_dir=trash)
    assert [o.name for o in plan.orphans] == ["Ghost Artist"]  # not 'Scans (LP)'

    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    sweep(
        reg,
        make_test_handle(lib, tmp_path),
        scope="library",
        trash_dir=trash,
        trash_origins_dir=origins_for(trash),
    )

    assert reg.state().moved == 0  # the fixture is already in place: nothing flattened
    assert reg.state().orphans_trashed == 1
    assert (scans / "booklet.jpg").exists()
    assert not husk.exists()


def test_live_album_roots_keeps_an_album_dir_that_nests_another_album(tmp_path: Path) -> None:
    """A bonus disc imported as its OWN album row sits inside the main album's folder,
    so the main album's root strictly contains another root. Dropping every such
    container let the sweep trash the main album's 'Sleeve Photos' (deep-review F3).

    The shape is ORDINARY, not a half-finished move: the main album's items are split
    across ``Disc 01``/``Disc 02`` exactly as a split album's are split across two
    sibling folders, so only the path template tells the two apart — ``A/Main`` is
    where the template puts this album, ``Artist`` (the split case) is not.
    """
    from app.beets.orphans import find_orphan_folders
    from app.beets.reorganize import live_album_roots

    lib = _library(tmp_path, path_format=_DISC_FORMAT)
    music = Path(os.fsdecode(lib.directory))
    _add_album(
        lib,
        artist="A",
        album="Main",
        files=[("A/Main/Disc 01/01 T1.mp3", 1), ("A/Main/Disc 02/02 T2.mp3", 2)],
    )
    _add_album(lib, artist="A", album="Bonus Disc", files=[("A/Main/Bonus Disc/01 T1.mp3", 1)])
    art = music / "A" / "Main" / "Sleeve Photos"
    art.mkdir(parents=True)
    (art / "back.jpg").write_bytes(b"x")
    husk = music / "Ghost"  # control: a genuine husk must still be swept
    husk.mkdir(parents=True)
    (husk / "poster.jpg").write_bytes(b"x")

    roots = live_album_roots(lib)

    assert str(music / "A" / "Main") in roots
    found = find_orphan_folders(
        music, seeds=None, trash_dir=tmp_path / "trash", protected_dirs=roots
    )
    assert found == [husk]


def test_the_orphan_pass_is_skipped_when_only_the_trash_dir_is_wired(
    reorganize_lib: Library, tmp_path: Path
) -> None:
    """Both dirs or neither — and the skip is LOUD rather than silent.

    A husk relocated without a recorded origin has no exit from Trash but
    permanent deletion, which is the exact gap the record exists to close. So a
    caller that wires the Trash dir and forgets the origin store must not collect
    husks it can never hand back; the pass is skipped, which fails visibly (no
    orphans collected) instead of quietly filling Trash with unrestorable rows.
    """
    from app.reorganize_jobs.registry import ReorganizeRegistry
    from app.reorganize_jobs.runner import sweep
    from tests.conftest import make_test_handle

    music_dir = Path(os.fsdecode(reorganize_lib.directory))
    husk = music_dir / "Ghost Artist"
    husk.mkdir(parents=True, exist_ok=True)
    (husk / "artist-poster.jpg").write_bytes(b"x")
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")

    sweep(
        reg,
        make_test_handle(reorganize_lib, tmp_path),
        scope="library",
        trash_dir=tmp_path / "trash",
        trash_origins_dir=None,
    )

    assert reg.state().orphans_trashed == 0
    assert husk.is_dir(), "the husk must be left in the library, not trashed unrestorably"
    # SKIPPED, not crashed: the reorganize itself still succeeded, and a run that
    # merely declined the orphan pass must not surface as a failed job. Without
    # this the guard can be deleted and the test still passes — the pass then
    # dies inside ``trash_folder`` and ``sweep`` turns it into ``fail``, which
    # also collects nothing.
    assert reg.state().phase == "done"
    assert reg.state().failures == []


def test_the_orphan_pass_is_skipped_when_the_origin_store_cannot_be_used(
    reorganize_lib: Library, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A store that is wired but UNUSABLE skips the phase; it must not fail the job.

    The sibling above covers the store not being wired at all. This one is the
    store being there and refusing: a regular FILE where the directory belongs,
    which is the shape that denies for uid 0 as well, so this does not self-skip
    for a maintainer running the suite inside the shipped image, which declares
    no ``USER`` (``Dockerfile``).

    Asked ONCE, up front, for two reasons the test asserts between them. Per
    folder it would refuse identically for every husk and the ``except OSError``
    inside the loop — written to isolate one bad folder — would swallow every
    one of them in silence. And failing the JOB would cost the run its ``.m3u8``
    re-export tail, since ``sweep``'s blanket handler calls ``reg.fail`` and
    skips it: the phase has nothing to do with the files this run already moved.
    So: nothing collected, the husk left alone, no Trash directory created, a
    WARNING carrying the traceback, and the run still finishing ``done``.
    """
    from app.reorganize_jobs.registry import ReorganizeRegistry
    from app.reorganize_jobs.runner import sweep
    from tests.conftest import make_test_handle

    music_dir = Path(os.fsdecode(reorganize_lib.directory))
    husk = music_dir / "Ghost Artist"
    husk.mkdir(parents=True, exist_ok=True)
    (husk / "artist-poster.jpg").write_bytes(b"x")
    trash = tmp_path / "trash"
    origins = tmp_path / "trash-origins"
    origins.write_bytes(b"not a directory")
    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")

    with caplog.at_level(logging.WARNING, logger="app.reorganize_jobs.runner"):
        sweep(
            reg,
            make_test_handle(reorganize_lib, tmp_path),
            scope="library",
            trash_dir=trash,
            trash_origins_dir=origins,
        )

    assert reg.state().orphans_trashed == 0
    assert husk.is_dir(), "the husk stays in the library rather than moving unrestorably"
    assert not trash.exists(), "and no Trash directory was created for it"
    skips = [r for r in caplog.records if "orphan sweep skipped" in r.getMessage()]
    assert len(skips) == 1, "asked once for the whole phase, not once per husk"
    assert skips[0].levelname == "WARNING"
    assert skips[0].exc_info is not None, "the errno is what tells an operator what to fix"
    # ...and the run still finishes, so the .m3u8 re-export tail after this
    # phase still runs. Without the guard the raise escapes the per-folder
    # ``except OSError`` into ``sweep``'s blanket handler, which calls reg.fail.
    assert reg.state().phase == "done"
    assert reg.state().failures == []
