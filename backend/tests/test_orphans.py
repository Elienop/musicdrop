from __future__ import annotations

from pathlib import Path

from app.beets.orphans import find_orphan_folders
from tests.conftest import origins_for


def _touch(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")


def test_art_only_folder_is_flagged(tmp_path: Path) -> None:
    root = tmp_path / "music"
    _touch(root / "Real Artist" / "Album" / "01.flac")  # keeps the library "kept"
    _touch(root / "Old Artist feat. X" / "artist-poster.jpg")  # husk
    trash = tmp_path / "trash"
    found = find_orphan_folders(root, seeds=None, trash_dir=trash)
    assert found == [root / "Old Artist feat. X"]


def test_folder_with_audio_is_not_flagged(tmp_path: Path) -> None:
    root = tmp_path / "music"
    _touch(root / "Artist" / "Album" / "01.flac")
    _touch(root / "Artist" / "cover.jpg")
    trash = tmp_path / "trash"
    assert find_orphan_folders(root, seeds=None, trash_dir=trash) == []


def test_nested_album_husk_under_kept_artist(tmp_path: Path) -> None:
    root = tmp_path / "music"
    _touch(root / "Artist" / "Good Album" / "01.flac")
    _touch(root / "Artist" / "Old Album" / "cover.jpg")  # audio-empty album husk
    trash = tmp_path / "trash"
    found = find_orphan_folders(root, seeds=None, trash_dir=trash)
    assert found == [root / "Artist" / "Old Album"]  # NOT the whole Artist (it has audio)


def test_live_album_art_subfolder_is_not_flagged(tmp_path: Path) -> None:
    """A ``Scans/`` or ``Artwork/`` subfolder inside a LIVE album dir (tracks sit
    directly in the album folder) is the album's own art, NOT a stale husk — a
    whole-library sweep must never trash it. Distinguisher: the PARENT holds audio
    files DIRECTLY (a live album), vs a genuine husk whose parent is an artist dir
    that has audio only via other album subdirs."""
    root = tmp_path / "music"
    _touch(root / "Artist" / "Album" / "01.flac")  # live album (audio in the album dir)
    _touch(root / "Artist" / "Album" / "Scans" / "booklet.jpg")  # art subfolder, no audio
    _touch(root / "Artist" / "Album" / "Artwork" / "back.png")
    trash = tmp_path / "trash"
    assert find_orphan_folders(root, seeds=None, trash_dir=trash) == []


def test_multidisc_album_art_subfolder_is_not_flagged(tmp_path: Path) -> None:
    """Multi-disc album (audio lives in ``Disc N/`` children, so the album dir has
    no DIRECT audio): its ``Scans/`` art folder must still be spared. has_own_audio
    alone can't tell it from a genuine husk, so well-known art-folder names are
    skipped by name as a backstop."""
    root = tmp_path / "music"
    _touch(root / "Artist" / "Album" / "Disc 1" / "01.flac")
    _touch(root / "Artist" / "Album" / "Disc 2" / "01.flac")
    _touch(root / "Artist" / "Album" / "Scans" / "booklet.jpg")  # art, no audio
    trash = tmp_path / "trash"
    found = find_orphan_folders(root, seeds=None, trash_dir=trash)
    assert (root / "Artist" / "Album" / "Scans") not in found


def test_untracked_audio_in_husk_is_not_flagged(tmp_path: Path) -> None:
    root = tmp_path / "music"
    _touch(root / "Real" / "Album" / "01.flac")
    _touch(root / "Stray" / "song.mp3")  # untracked audio -> must be left alone
    _touch(root / "Stray" / "cover.jpg")
    trash = tmp_path / "trash"
    assert find_orphan_folders(root, seeds=None, trash_dir=trash) == []


def test_root_and_trash_and_empty_excluded(tmp_path: Path) -> None:
    root = tmp_path / "music"
    _touch(root / "Artist" / "Album" / "01.flac")
    (root / "Empty Husk").mkdir(parents=True)  # truly empty -> beets prunes, not us
    trash = root / "trash"  # trash sitting INSIDE the music root
    _touch(trash / "Discarded" / "cover.jpg")  # must be ignored (it's trash)
    found = find_orphan_folders(root, seeds=None, trash_dir=trash)
    assert found == []


def test_seeds_mode_returns_surviving_audioless_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "music"
    _touch(root / "Other" / "Album" / "01.flac")
    _touch(root / "Old Name" / "artist-poster.jpg")  # husk; its album subdir was pruned
    trash = tmp_path / "trash"
    # seed = the now-gone album dir under the husk
    seed = root / "Old Name" / "Some Album"
    found = find_orphan_folders(root, seeds=[seed], trash_dir=trash)
    assert found == [root / "Old Name"]


def test_seeds_mode_skips_when_parent_keeps_audio(tmp_path: Path) -> None:
    root = tmp_path / "music"
    _touch(root / "Artist" / "Keeps" / "01.flac")
    seed = root / "Artist" / "Moved Album"  # pruned, and Artist still has audio
    trash = tmp_path / "trash"
    assert find_orphan_folders(root, seeds=[seed], trash_dir=trash) == []


def test_dotdir_and_nas_dirs_are_never_flagged(tmp_path: Path) -> None:
    root = tmp_path / "music"
    _touch(root / "Real" / "Album" / "01.flac")
    _touch(root / ".playlists" / "p1.m3u8")  # app-owned export dir (dotdir)
    _touch(root / "@eaDir" / "thumb.jpg")  # Synology
    _touch(root / "#recycle" / "old.jpg")  # SMB recycle
    trash = tmp_path / "trash"
    assert find_orphan_folders(root, seeds=None, trash_dir=trash) == []


def test_ignore_dirs_excludes_a_configured_export_dir(tmp_path: Path) -> None:
    root = tmp_path / "music"
    _touch(root / "Real" / "Album" / "01.flac")
    export = root / "Playlists"  # non-dotfile, must still be skipped
    _touch(export / "p1.m3u8")
    trash = tmp_path / "trash"
    assert find_orphan_folders(root, seeds=None, trash_dir=trash, ignore_dirs=(export,)) == []
    # without the ignore it WOULD be flagged (proves the exclusion is load-bearing):
    assert find_orphan_folders(root, seeds=None, trash_dir=trash) == [export]


def test_ignore_dirs_excludes_the_trash_origin_store(tmp_path: Path) -> None:
    """A new exposure the sidecar did not have, closed at ``api.reorganize._ignore_dirs``.

    The origin store is a non-dotfile directory holding only ``.json`` files, so
    it is audio-empty BY DEFINITION and the sweep reads it as a husk. While the
    records lived INSIDE the trashed folders they sat under ``trash_dir``, which
    ``find_orphan_folders`` already excludes; a separate store configured under
    the music root does not get that for free, and sweeping it would take every
    row's exact restore into Trash in one pass.

    Both halves asserted, because the second is what makes the first mean
    anything: without the exclusion the store really is flagged.
    """
    from types import SimpleNamespace

    from app.api.reorganize import _ignore_dirs
    from app.config import Settings
    from tests.conftest import build_library, make_test_handle

    root = tmp_path / "music"
    _touch(root / "Real" / "Album" / "01.flac")
    store = root / "trash-origins"  # a configured store, directly under the library
    _touch(store / "Some Album.json")
    trash = tmp_path / "trash"

    assert find_orphan_folders(root, seeds=None, trash_dir=trash) == [store]
    assert find_orphan_folders(root, seeds=None, trash_dir=trash, ignore_dirs=(store,)) == []

    # ...and the API layer really hands that dir to the sweep.
    handle = make_test_handle(build_library(str(tmp_path / "library.db"), str(root)), tmp_path)
    app = SimpleNamespace(
        state=SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_origins_dir=str(store), playlists_export_dir=""),
        )
    )
    assert store in _ignore_dirs(app)


def test_trash_folder_moves_whole_folder(tmp_path: Path) -> None:
    from app.beets.trash import trash_folder

    husk = tmp_path / "music" / "Old Name"
    _touch(husk / "artist-poster.jpg")
    _touch(husk / "artist-background.jpg")
    trash = tmp_path / "trash"

    dest = trash_folder(husk, trash_dir=trash, origins_dir=origins_for(trash))

    assert not husk.exists()  # source gone
    assert dest.parent == trash
    assert dest.name == "Old Name"
    assert (dest / "artist-poster.jpg").exists()  # reversible: files live in Trash


def test_trash_folder_collision_gets_unique_name(tmp_path: Path) -> None:
    from app.beets.trash import trash_folder

    trash = tmp_path / "trash"
    (trash / "Old Name").mkdir(parents=True)  # name already taken in Trash
    husk = tmp_path / "music" / "Old Name"
    _touch(husk / "cover.jpg")

    dest = trash_folder(husk, trash_dir=trash, origins_dir=origins_for(trash))
    assert dest.name == "Old Name (1)"
    assert (dest / "cover.jpg").exists()


def test_art_only_husk_is_listed_for_visibility(tmp_path: Path) -> None:
    from app.beets.trash import trash_folder
    from app.beets.trash_manage import list_trashed_albums

    husk = tmp_path / "music" / "Old Name"
    _touch(husk / "artist-poster.jpg")  # no audio
    trash = tmp_path / "trash"
    trash_folder(husk, trash_dir=trash, origins_dir=origins_for(trash))

    # Audio-free trashed folders (husks the orphan sweep moves here) must appear in
    # the listing as zero-track entries — otherwise the Trash UI never shows them
    # and Empty-all deletes them silently. Now visible + individually empty-able.
    listed = list_trashed_albums(
        trash, origins_dir=origins_for(trash), music_dir=str(tmp_path / "music")
    )
    assert [a.folder for a in listed] == ["Old Name"]
    assert listed[0].track_count == 0


def test_reorganize_models_carry_orphan_fields() -> None:
    from app.models.reorganize import (
        OrphanFolder,
        ReorganizeBackfillStatus,
        ReorganizeOutcome,
        ReorganizePlan,
    )

    of = OrphanFolder(name="Old", path="Old", file_count=2)
    plan = ReorganizePlan(
        scope="library",
        scope_label="library",
        total=0,
        will_move=0,
        already_in_place=0,
        moves=[],
        truncated=False,
        orphans=[of],
        orphans_total=1,
        conflicts=[],
        conflicts_total=0,
    )
    assert plan.orphans[0].file_count == 2
    assert plan.orphans_total == 1
    out = ReorganizeOutcome(status="moved", label="A — B", source_dir="/m/A/B")
    assert out.source_dir == "/m/A/B"
    assert ReorganizeOutcome(status="skipped", label="x").source_dir is None
    status = ReorganizeBackfillStatus(
        phase="done",
        job_id="j",
        scope="library",
        total=0,
        processed=0,
        moved=0,
        skipped=0,
        failed=0,
        current=None,
        error=None,
        artist=None,
        album_id=None,
        scope_label="library",
        orphans_trashed=3,
        playlists_reexported=0,
        failures=[],
        finished_at=None,
    )
    assert status.orphans_trashed == 3


def test_protected_dirs_do_not_shield_genuine_husk_in_artist_dir(tmp_path: Path) -> None:
    """The sentinel for the discriminator: ``protected_dirs`` holds live album ROOTS,
    never artist containers, so a genuine husk sitting next to a live album is still
    swept. A naive "the parent has audio somewhere beneath" rule shields it (the
    artist dir has audio via its OTHER album) — this test is what forbids that rule."""
    root = tmp_path / "music"
    _touch(root / "Artist" / "Good Album" / "01.flac")
    _touch(root / "Artist" / "Old Album" / "cover.jpg")  # genuine husk
    trash = tmp_path / "trash"
    found = find_orphan_folders(
        root,
        seeds=None,
        trash_dir=trash,
        protected_dirs={str(root / "Artist" / "Good Album")},
    )
    assert found == [root / "Artist" / "Old Album"]


def test_multidisc_nonlisted_art_folder_protected_by_album_root(tmp_path: Path) -> None:
    """A live multi-disc album's art folder whose name is OUTSIDE ART_DIR_NAMES is
    protected because its PARENT is a live album root (the bug: 'Scans (LP)' was
    swept to Trash while the album stayed in the library)."""
    root = tmp_path / "music"
    _touch(root / "Artist" / "Album" / "Disc 1" / "01.flac")
    _touch(root / "Artist" / "Album" / "Disc 2" / "01.flac")
    _touch(root / "Artist" / "Album" / "Scans (LP)" / "booklet.jpg")
    trash = tmp_path / "trash"
    assert (
        find_orphan_folders(
            root,
            seeds=None,
            trash_dir=trash,
            protected_dirs={str(root / "Artist" / "Album")},
        )
        == []
    )


def test_protected_dirs_shield_the_album_root_itself(tmp_path: Path) -> None:
    """A DB-live album whose folder currently holds only art (its audio vanished
    from disk without a disk-sync) is protected by the candidate-itself clause."""
    root = tmp_path / "music"
    _touch(root / "Artist" / "Other Album" / "01.flac")  # keeps the artist dir "kept"
    _touch(root / "Artist" / "Live Album" / "cover.jpg")  # audio-empty, but DB-live
    trash = tmp_path / "trash"
    assert (
        find_orphan_folders(
            root,
            seeds=None,
            trash_dir=trash,
            protected_dirs={str(root / "Artist" / "Live Album")},
        )
        == []
    )


def test_protected_root_under_candidate_shields_the_ancestor(tmp_path: Path) -> None:
    """The candidate is an ANCESTOR of the album root, not the root or its parent:
    a lone album under an artist dir whose audio is gone from disk reports the whole
    ARTIST dir, and trashing it would take the live album's folder with it."""
    root = tmp_path / "music"
    _touch(root / "Artist" / "Album" / "cover.jpg")  # only art left on disk
    trash = tmp_path / "trash"
    # Without protection the whole artist dir is the reported husk (pins the shape):
    assert find_orphan_folders(root, seeds=None, trash_dir=trash) == [root / "Artist"]
    assert (
        find_orphan_folders(
            root,
            seeds=None,
            trash_dir=trash,
            protected_dirs={str(root / "Artist" / "Album")},
        )
        == []
    )


# --------------------------------------------------------------------------
# An excluded root's ANCESTORS. Excluding a subtree only protects it from a
# sweep that names it: the mover takes a reported folder WITH its subtree, so an
# ancestor of an ignored dir hands the ignored dir over anyway.
# --------------------------------------------------------------------------


def test_an_ancestor_of_an_ignored_dir_is_not_reported(tmp_path: Path) -> None:
    """The measured hole, and the file of its own that is needed to open it.

    An excluded subtree is never recorded, so it contributes no ``has_file`` to
    the dir above it — an only-child parent therefore reads as EMPTY and is
    skipped, which is why this needs a stray file to show at all. With one,
    ``data`` was reported on ``d65e635`` and its subtree includes the exports.
    """
    root = tmp_path / "music"
    _touch(root / "Real" / "Album" / "01.flac")
    exports = root / "data" / "exports"
    _touch(exports / "p1.m3u8")
    _touch(root / "data" / "notes.txt")  # the parent's own content
    trash = tmp_path / "trash"

    assert find_orphan_folders(root, seeds=None, trash_dir=trash, ignore_dirs=(exports,)) == []
    # Without the stray file the parent is empty and was already skipped, so this
    # second half is what shows the drop is doing the work rather than the old
    # empty-dir rule (both return [] and only one of them is about this guard).
    (root / "data" / "notes.txt").unlink()
    assert find_orphan_folders(root, seeds=None, trash_dir=trash, ignore_dirs=(exports,)) == []


def test_a_genuine_husk_beside_an_ignored_dir_is_still_reported(tmp_path: Path) -> None:
    """The control: the guard spares ancestors, not the whole sweep.

    Same run as the spared ancestor, so a drop that returned ``[]`` for
    everything would fail here instead of passing the test above for free.
    """
    root = tmp_path / "music"
    _touch(root / "Real" / "Album" / "01.flac")
    _touch(root / "Real" / "Old Album" / "cover.jpg")  # a real husk under a kept artist
    exports = root / "data" / "exports"
    _touch(exports / "p1.m3u8")
    _touch(root / "data" / "notes.txt")
    trash = tmp_path / "trash"

    assert find_orphan_folders(root, seeds=None, trash_dir=trash, ignore_dirs=(exports,)) == [
        root / "Real" / "Old Album"
    ]


def test_a_deeper_ignored_dir_does_not_push_the_report_one_level_up(tmp_path: Path) -> None:
    """Sparing the whole ancestor CHAIN, not just the immediate parent.

    Measured on ``d65e635`` with the file one level down: the report was
    ``data``, not ``data/sub`` — ``data`` holds nothing of its own, so what gets
    reported is the top of the audio-empty run. Sparing one level would have
    moved the answer up rather than removed it.
    """
    root = tmp_path / "music"
    _touch(root / "Real" / "Album" / "01.flac")
    store = root / "data" / "sub" / "origins"
    _touch(store / "Some Album.json")
    _touch(root / "data" / "sub" / "notes.txt")
    trash = tmp_path / "trash"

    assert find_orphan_folders(root, seeds=None, trash_dir=trash, ignore_dirs=(store,)) == []


def test_seeds_mode_also_spares_an_ignored_dirs_ancestor(tmp_path: Path) -> None:
    """Seeds mode stops its CLIMB at an excluded ancestor, which is a different guard.

    The climb starts below the stop and banks a candidate on the way up, so the
    dir it returns can still be an ancestor of a DIFFERENT excluded root. Both
    modes are covered because the drop runs in ``find_orphan_folders``, after
    either branch has produced its raw list.
    """
    root = tmp_path / "music"
    _touch(root / "Real" / "Album" / "01.flac")
    exports = root / "data" / "exports"
    _touch(exports / "p1.m3u8")
    _touch(root / "data" / "notes.txt")
    trash = tmp_path / "trash"

    assert (
        find_orphan_folders(
            root, seeds=[root / "data" / "gone"], trash_dir=trash, ignore_dirs=(exports,)
        )
        == []
    )


def test_a_beets_dir_inside_the_library_is_never_reported(tmp_path: Path) -> None:
    """A plain-named beets dir under the music root, end to end through the API helper.

    It is reported DIRECTLY rather than as an ancestor — beets plants
    ``library.db`` and ``config.yaml`` in it, so it has content of its own —
    which is why a dot-name (``.musicdrop``) escaped and this spelling did not.
    Measured both ways on ``d65e635``.

    Asserted through ``api.reorganize._ignore_dirs`` rather than by passing the
    dir in by hand: the finder was already able to skip a dir it was told about,
    and what was missing was the caller telling it.

    ``trash_dir`` sits OUTSIDE the beets dir here, which is the arm that loses
    data: with the default ``<B>/trash`` the move would be a directory into its
    own subtree and ``shutil.move`` refuses it, while a configured Trash
    elsewhere takes ``library.db``, ``config.yaml`` and the store in one pass.
    It is also the arm the ancestor drop does not already cover — see
    ``test_the_default_trash_position_already_spares_the_beets_dir``.
    """
    from types import SimpleNamespace

    from app.api.reorganize import _ignore_dirs
    from app.config import Settings
    from tests.conftest import build_library, make_test_handle

    root = tmp_path / "music"
    _touch(root / "Real" / "Album" / "01.flac")
    beets_dir = root / "musicdrop"
    _touch(beets_dir / "library.db")
    _touch(beets_dir / "config.yaml")
    trash = tmp_path / "trash"
    store = tmp_path / "records"

    # Without the exclusion the beets dir IS the reported husk (pins the shape):
    assert find_orphan_folders(root, seeds=None, trash_dir=trash) == [beets_dir]

    handle = make_test_handle(build_library(str(beets_dir / "library.db"), str(root)), beets_dir)
    app = SimpleNamespace(
        state=SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_origins_dir=str(store), playlists_export_dir=""),
        )
    )
    ignore = _ignore_dirs(app)
    assert beets_dir in ignore
    assert find_orphan_folders(root, seeds=None, trash_dir=trash, ignore_dirs=ignore) == []


def test_a_beets_dir_that_contains_the_library_is_not_an_ignore_root(tmp_path: Path) -> None:
    """The condition that keeps the previous test from silencing the whole sweep.

    ``excluded()`` is a prefix test, so an ignore root ABOVE the music root
    excludes every candidate and the sweep returns ``[]`` for the entire library.
    That layout is the ordinary one — the starter config ships
    ``directory: ../music``, and the test fixtures put the music dir under the
    beets dir — so the entry has to be conditional on the beets dir being INSIDE
    the library. The husk here proves the sweep is still alive.
    """
    from types import SimpleNamespace

    from app.api.reorganize import _ignore_dirs
    from app.config import Settings
    from tests.conftest import build_library, make_test_handle

    beets_dir = tmp_path
    root = tmp_path / "music"
    _touch(root / "Real" / "Album" / "01.flac")
    _touch(root / "Old Artist" / "poster.jpg")  # a genuine husk
    trash = beets_dir / "trash"

    handle = make_test_handle(build_library(str(beets_dir / "library.db"), str(root)), beets_dir)
    app = SimpleNamespace(
        state=SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_origins_dir=str(tmp_path / "records"), playlists_export_dir=""),
        )
    )
    ignore = _ignore_dirs(app)
    assert beets_dir not in ignore
    assert find_orphan_folders(root, seeds=None, trash_dir=trash, ignore_dirs=ignore) == [
        root / "Old Artist"
    ]


def test_the_default_trash_position_already_spares_the_beets_dir(tmp_path: Path) -> None:
    """The other arm, and why the ``_ignore_dirs`` entry is not the only guard.

    With ``trash_dir`` at its default ``<B>/trash``, the beets dir is an ANCESTOR
    of an excluded root, so ``_drop_excluded_ancestors`` spares it before the
    caller's ignore list is even consulted. Pinned so a later edit that made the
    ``_ignore_dirs`` entry unconditional (or dropped it) would still be measured
    against this case rather than assumed to cover it.
    """
    root = tmp_path / "music"
    _touch(root / "Real" / "Album" / "01.flac")
    beets_dir = root / "musicdrop"
    _touch(beets_dir / "library.db")
    _touch(beets_dir / "config.yaml")

    assert find_orphan_folders(root, seeds=None, trash_dir=beets_dir / "trash") == []
