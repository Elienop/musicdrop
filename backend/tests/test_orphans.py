from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_the_ignore_list_names_the_directory_the_exporter_writes_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_ignore_dirs`` and ``export_dir_for`` are one function now, not two copies.

    They were two, each computing ``<music>/.playlists`` for an empty setting,
    and the sweep spared the export dir only for as long as the copies agreed.
    All three shapes of the setting are asserted against a literal — the empty
    default, an absolute path and a relative one — because a drift could show in
    any one of them alone.

    The relative shape is the one with teeth: ``export_dir_for`` hands the value
    through unchanged and ``open()`` joins it to the process CWD, so the sweep
    has to be given the same unchanged value to resolve. What it does with it is
    pinned in ``test_a_relative_export_dir_matches_the_directory_it_is_written_to``.
    """
    from types import SimpleNamespace

    from app.api.reorganize import _ignore_dirs
    from app.config import Settings
    from app.playlists.reexport import export_dir_for
    from tests.conftest import beets_dir_for, build_library, make_test_handle

    root = tmp_path / "music"
    root.mkdir()
    beets_dir = beets_dir_for(tmp_path)
    origins = tmp_path / "records"
    handle = make_test_handle(build_library(str(beets_dir / "library.db"), str(root)), beets_dir)
    app = SimpleNamespace(
        state=SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_origins_dir=str(origins)),
        )
    )

    for configured, expected in (
        ("", root / ".playlists"),
        (str(tmp_path / "exports"), tmp_path / "exports"),
        ("exports", Path("exports")),
    ):
        monkeypatch.setattr("app.config.settings.playlists_export_dir", configured)
        assert _ignore_dirs(app, origins)[0] == expected, configured
        assert export_dir_for(handle.lib) == expected, configured


def test_the_ignore_list_names_the_beets_dir_whatever_its_position(tmp_path: Path) -> None:
    """The entry is unconditional: the shipped layout puts B beside the library.

    ``app.beets.store_layout`` refuses B nesting with M in either direction, so
    on any layout the app will boot with, this entry resolves outside the walked
    tree and the finder drops it. It is passed anyway — the sweep is what would
    move ``library.db`` — and the position it is passed from must not decide
    whether it is passed.
    """
    from types import SimpleNamespace

    from app.api.reorganize import _ignore_dirs
    from app.config import Settings
    from tests.conftest import beets_dir_for, build_library, make_test_handle

    origins = tmp_path / "records"
    for beets_dir, root in (
        (beets_dir_for(tmp_path), tmp_path / "music"),  # siblings — the shipped shape
        (tmp_path, tmp_path / "music"),  # B above M
        (tmp_path / "music" / "musicdrop", tmp_path / "music"),  # B inside M
    ):
        beets_dir.mkdir(parents=True, exist_ok=True)
        root.mkdir(parents=True, exist_ok=True)
        handle = make_test_handle(
            build_library(str(beets_dir / "library.db"), str(root)), beets_dir
        )
        app = SimpleNamespace(
            state=SimpleNamespace(
                beets_library=handle,
                settings=Settings(trash_origins_dir=str(origins)),
            )
        )
        assert beets_dir in _ignore_dirs(app, origins), beets_dir


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
    assert store in _ignore_dirs(app, store)


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


def test_a_husk_under_an_audio_BEARING_ancestor_is_reported_beside_an_ignored_dir(
    tmp_path: Path,
) -> None:
    """The control: the guard spares ancestors, not the whole sweep.

    The qualifier in the name is the whole of it. This husk's ancestor holds
    audio somewhere beneath it (``Real/Album/01.flac``), so the husk is not part
    of an audio-empty run and the ancestor drop does not reach it. A husk whose
    ancestors hold no audio at all is a different answer — see
    ``test_a_husk_under_an_audio_free_ancestor_of_an_ignored_dir_is_kept``.

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


def test_a_husk_directly_under_the_music_root_is_reported_beside_an_ignored_dir(
    tmp_path: Path,
) -> None:
    """The second reported case: the husk's parent IS the root.

    The root is never a candidate, so the audio-empty run cannot start above the
    husk and there is nothing for the ancestor drop to remove. Named separately
    from the audio-bearing case because the two are reported for different
    reasons and a change could break one without the other.
    """
    root = tmp_path / "music"
    _touch(root / "Real" / "Album" / "01.flac")
    _touch(root / "Old Artist" / "poster.jpg")  # husk directly under the root
    exports = root / "data" / "exports"
    _touch(exports / "p1.m3u8")
    _touch(root / "data" / "notes.txt")
    trash = tmp_path / "trash"

    assert find_orphan_folders(root, seeds=None, trash_dir=trash, ignore_dirs=(exports,)) == [
        root / "Old Artist"
    ]


def test_a_husk_under_an_audio_free_ancestor_of_an_ignored_dir_is_kept(tmp_path: Path) -> None:
    """The RESIDUAL, pinned rather than fixed: this husk is not reported.

    ``data`` holds no audio anywhere beneath it, so the sweep's candidate is
    ``data`` — the top of the audio-empty run — and ``data`` is an ancestor of
    the excluded ``data/exports``, so the drop removes it. Nothing below is
    re-selected, so ``data/Old Album`` goes unreported even though it is a
    genuine husk.

    Err toward keeping is the module's stated posture and the reason this is a
    pin and not a bug: a husk left alone is a folder somebody deletes by hand,
    where the other direction moves an excluded dir into Trash with its whole
    subtree. The pin exists so a later change that starts reporting
    ``data/Old Album`` is a decision rather than an accident.
    """
    root = tmp_path / "music"
    _touch(root / "Real" / "Album" / "01.flac")
    exports = root / "data" / "exports"
    _touch(exports / "p1.m3u8")
    _touch(root / "data" / "Old Album" / "cover.jpg")  # a genuine husk, and it is KEPT
    trash = tmp_path / "trash"

    assert find_orphan_folders(root, seeds=None, trash_dir=trash, ignore_dirs=(exports,)) == []


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
    ignore = _ignore_dirs(app, store)
    assert beets_dir in ignore
    assert find_orphan_folders(root, seeds=None, trash_dir=trash, ignore_dirs=ignore) == []


def test_an_ignore_root_above_the_music_root_is_dropped_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The entry is unconditional now, and the sweep survives it.

    ``excluded()`` is a prefix test, so an ignore root at or ABOVE the music root
    matches every walked directory and the sweep returns ``[]`` for the entire
    library. That used to be kept out by making the ``_ignore_dirs`` beets-dir
    entry conditional on the dir sitting inside the library, which left the same
    hole open for the two roots that have no such condition. It is handled in the
    finder now: the root is dropped and one WARNING is logged.

    Three assertions, and each covers a different mistake: the entry is present
    (a caller that stopped passing it), the husk is still reported (a drop that
    silenced the sweep anyway), and the line is logged (a silent no-op, which is
    what this was before).

    ``app.beets.store_layout`` refuses this layout at boot and at every
    destructive use site, so it does not arrive through the app — the finder is a
    public function and this is its own guard.
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
    ignore = _ignore_dirs(app, tmp_path / "records")
    assert beets_dir in ignore

    with caplog.at_level(logging.WARNING, logger="app.beets.orphans"):
        found = find_orphan_folders(root, seeds=None, trash_dir=trash, ignore_dirs=ignore)

    assert found == [root / "Old Artist"]
    warnings = [r for r in caplog.records if "at or above the music root" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]


# --------------------------------------------------------------------------
# The walk root and the exclusion roots can be two spellings of one directory.
# --------------------------------------------------------------------------


def _symlinked_library(tmp_path: Path) -> tuple[Path, Path]:
    """``(link, real)`` — ``/music -> /mnt/tank/music``, the shape a container has.

    beets stores ``lib.directory`` as the operator wrote it (``normpath``, not
    ``realpath`` — ``beets/util/__init__.py:178``), so the sweep walks the LINK
    while the exclusion roots reach it resolved.
    """
    real = tmp_path / "tank" / "music"
    real.mkdir(parents=True)
    link = tmp_path / "music"
    link.symlink_to(real)
    return link, real


def test_an_ignore_root_matches_through_a_symlinked_walk_root(tmp_path: Path) -> None:
    """The measured loss: a resolved beets dir under a symlinked library.

    Measured in the review round on the pre-fix finder — with the library reached
    through a symlink the beets data dir was REPORTED, and running the mover on
    the reported folder took ``library.db`` and ``config.yaml`` into Trash.

    The control is the first assertion: without the ignore root the dir IS the
    husk, so the second assertion is about the exclusion working rather than
    about the dir being uninteresting.
    """
    link, real = _symlinked_library(tmp_path)
    _touch(real / "Real" / "Album" / "01.flac")
    _touch(real / "musicdrop" / "library.db")
    trash = tmp_path / "trash"

    assert find_orphan_folders(link, seeds=None, trash_dir=trash) == [link / "musicdrop"]

    resolved_beets_dir = (real / "musicdrop").resolve()
    assert (
        find_orphan_folders(link, seeds=None, trash_dir=trash, ignore_dirs=(resolved_beets_dir,))
        == []
    )


def test_a_trash_dir_matches_through_a_symlinked_walk_root(tmp_path: Path) -> None:
    """The Trash twin, and the reason it is worse than a missed exclusion.

    A non-dot Trash inside the library that the sweep does not recognise has its
    OWN husks reported, so every run moves them back into Trash under a fresh
    ``(n)`` name — the pile grows on a schedule instead of being left alone.
    """
    link, real = _symlinked_library(tmp_path)
    _touch(real / "Real" / "Album" / "01.flac")
    _touch(real / "recycle" / "Old Album" / "cover.jpg")
    _touch(real / "Old Artist" / "poster.jpg")  # a husk the sweep SHOULD report

    found = find_orphan_folders(link, seeds=None, trash_dir=(real / "recycle").resolve())

    assert found == [link / "Old Artist"]


def test_the_export_dir_matches_through_a_symlinked_walk_root(tmp_path: Path) -> None:
    """The third root, whose contents are ``.m3u8`` files and nothing else.

    An export dir is audio-empty by definition, so a missed exclusion reports it
    every run — and the mover would take every exported playlist with it.
    """
    link, real = _symlinked_library(tmp_path)
    _touch(real / "Real" / "Album" / "01.flac")
    _touch(real / "exports" / "1.m3u8")
    _touch(real / "Old Artist" / "poster.jpg")  # a husk the sweep SHOULD report

    found = find_orphan_folders(
        link, seeds=None, trash_dir=tmp_path / "trash", ignore_dirs=((real / "exports").resolve(),)
    )

    assert found == [link / "Old Artist"]


def test_an_ignore_root_spelled_through_a_symlink_matches_a_real_walk_root(
    tmp_path: Path,
) -> None:
    """The other direction: the walk root is real and the exclude root is a link.

    ``MUSICDROP_PLAYLISTS_EXPORT_DIR`` is written by an operator, so the app has
    no say in which spelling arrives — the comparison has to hold from either
    side.
    """
    root = tmp_path / "music"
    _touch(root / "Real" / "Album" / "01.flac")
    _touch(root / "exports" / "1.m3u8")
    link = tmp_path / "exports-link"
    link.symlink_to(root / "exports")

    _touch(root / "Old Artist" / "poster.jpg")  # a husk the sweep SHOULD report

    assert sorted(find_orphan_folders(root, seeds=None, trash_dir=tmp_path / "trash")) == [
        root / "Old Artist",
        root / "exports",
    ]
    assert find_orphan_folders(
        root, seeds=None, trash_dir=tmp_path / "trash", ignore_dirs=(link,)
    ) == [root / "Old Artist"]


def test_a_relative_export_dir_matches_the_directory_it_is_written_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative ``MUSICDROP_PLAYLISTS_EXPORT_DIR``, which ``open()`` joins to the CWD.

    ``export_dir_for`` hands the configured value through unchanged, so the
    ``.m3u8`` files land under the process working directory. A comparison that
    kept the relative string could match no walked directory at all, and the dir
    the exporter writes to was swept.
    """
    root = tmp_path / "music"
    _touch(root / "Real" / "Album" / "01.flac")
    _touch(root / "exports" / "1.m3u8")
    _touch(root / "Old Artist" / "poster.jpg")  # a husk the sweep SHOULD report
    monkeypatch.chdir(root)

    found = find_orphan_folders(
        root, seeds=None, trash_dir=tmp_path / "trash", ignore_dirs=(Path("exports"),)
    )

    assert found == [root / "Old Artist"]


def test_an_ignore_root_outside_the_library_leaves_the_sweep_alone(tmp_path: Path) -> None:
    """The control for the two drops: a root elsewhere is neither warned nor fatal.

    The shipped layout puts the beets data dir beside the library rather than
    inside it, so this is the ordinary case — it must not consume the WARNING the
    at-or-above case is pinned on, and it must not stop the husk being reported.
    """
    root = tmp_path / "music"
    _touch(root / "Real" / "Album" / "01.flac")
    _touch(root / "Old Artist" / "poster.jpg")

    found = find_orphan_folders(
        root,
        seeds=None,
        trash_dir=tmp_path / "trash",
        ignore_dirs=(tmp_path / "beets", tmp_path / "records"),
    )

    assert found == [root / "Old Artist"]


def test_a_trash_dir_at_the_music_root_does_not_silence_the_sweep(tmp_path: Path) -> None:
    """``trash_dir`` goes through the same drop, and it is the one that is not optional.

    ``app.beets.store_layout`` refuses a Trash dir that is or contains the music
    root, so this arrives only if the finder is called directly — and a sweep
    that quietly reported nothing is how that call would go unnoticed.
    """
    root = tmp_path / "music"
    _touch(root / "Real" / "Album" / "01.flac")
    _touch(root / "Old Artist" / "poster.jpg")

    assert find_orphan_folders(root, seeds=None, trash_dir=root) == [root / "Old Artist"]
    assert find_orphan_folders(root, seeds=None, trash_dir=tmp_path) == [root / "Old Artist"]


def test_the_walk_root_spelling_is_what_comes_back(tmp_path: Path) -> None:
    """Returned paths keep the spelling the caller walked with.

    The mover and ``protected_dirs`` are built on ``lib.directory``; a result set
    in resolved form would miss every protected root and hand a live album's
    folder to Trash. ``os.path.realpath`` on the result is what shows the two are
    the same directory.
    """
    link, real = _symlinked_library(tmp_path)
    _touch(real / "Real" / "Album" / "01.flac")
    _touch(real / "Old Artist" / "poster.jpg")

    found = find_orphan_folders(link, seeds=None, trash_dir=tmp_path / "trash")

    assert found == [link / "Old Artist"]
    assert os.path.realpath(found[0]) == str(real / "Old Artist")


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


# --------------------------------------------------------------------------
# An exclude root is matched by ``(st_dev, st_ino)``, not by a path prefix.
# A bind mount is the alias a prefix (translated or not) does not see.
# --------------------------------------------------------------------------

_BIND_PROBE = """
import subprocess, sys
from pathlib import Path
base = Path(sys.argv[1])
src = base / "srv" / "music"
(src / "Real").mkdir(parents=True)
(src / "Real" / "01.flac").write_bytes(b"x")
(src / "Old Artist").mkdir()
(src / "Old Artist" / "poster.jpg").write_bytes(b"x")
(src / "bin").mkdir()
(src / "bin" / "note.txt").write_bytes(b"x")
mnt = base / "music"
mnt.mkdir()
trash = base / "trash"
trash.mkdir()
subprocess.run(["mount", "--bind", str(src), str(mnt)], check=True)
subprocess.run(["mount", "--bind", str(src / "bin"), str(trash)], check=True)
from app.beets.orphans import find_orphan_folders
assert (mnt / "bin").samefile(trash), "the fixture did not alias the Trash"
print(sorted(p.name for p in find_orphan_folders(mnt, seeds=None, trash_dir=trash)))
"""


def _unshare_works() -> bool:
    """Whether this box grants an unprivileged mount namespace.

    Measured rather than assumed: the probe below is the only shape that tells
    inode identity apart from a path prefix, and a box without user namespaces
    would otherwise fail the test for a reason that is not about this code.
    """
    try:
        done = subprocess.run(
            ["unshare", "-Urm", "true"], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


@pytest.mark.skipif(not _unshare_works(), reason="no unprivileged mount namespace on this box")
def test_a_bind_mounted_trash_is_excluded_where_a_path_prefix_does_not_see_it(
    tmp_path: Path,
) -> None:
    """The alias that makes exclusion an IDENTITY question rather than a string one.

    ``-v /srv/music:/music`` plus ``-v /srv/music/bin:/trash`` is one directory
    reached by two paths, and ``realpath`` collapses neither: a mount point's
    ancestors are the mount point's, never the source's. Measured on the parent
    commit's finder with this exact fixture — it reported ``['Old Artist', 'bin']``
    and the mover would have emptied the Trash into itself; on this one it
    reports ``['Old Artist']``. ``samefile`` inside the probe asserts the fixture
    really aliased the two, so a mount that silently did nothing fails loudly
    rather than passing for the wrong reason.

    Run in a child process because the bind mounts need a mount namespace of
    their own; the child imports the app package from this repo.
    """
    work = tmp_path / "work"
    work.mkdir()
    script = tmp_path / "probe.py"
    script.write_text(_BIND_PROBE, encoding="utf-8")
    backend = Path(__file__).resolve().parent.parent
    # The child inherits this process's environment, which the rootdir conftest
    # has already floored (``BEETSDIR`` under the test tree), and adds only the
    # import root — ``app.beets.orphans`` imports nothing but the stdlib, so the
    # child opens no beets config either way.
    env = {**os.environ, "PYTHONPATH": str(backend)}
    done = subprocess.run(
        ["unshare", "-Urm", sys.executable, str(script), str(work)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(backend),
        env=env,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "['Old Artist']", done.stdout + done.stderr


# --------------------------------------------------------------------------
# "I could not look" is not "there is nothing there".
# --------------------------------------------------------------------------


@pytest.mark.skipif(os.getuid() == 0, reason="root reads a mode-000 directory anyway")
def test_an_unreadable_album_dir_does_not_make_its_parent_a_husk(tmp_path: Path) -> None:
    """A walk error marks the subtree audio-BEARING, in both modes.

    Measured on the parent commit: ``music/Perm/Album`` at mode 000 with
    ``music/Perm/cover.jpg`` beside it made ``Perm`` read as a husk in library
    mode, and the mover relocates a reported folder with its whole subtree — the
    audio inside the unreadable dir went to Trash. ``os.walk``'s ``onerror`` had
    swallowed the ``PermissionError``, so the dir was never recorded and
    contributed no audio to its parent.

    The control is the same tree readable: ``Perm`` is not a husk then either,
    for the ordinary reason, so the third case below (no audio anywhere) is what
    shows the guard has not simply switched the sweep off.
    """
    root = tmp_path / "music"
    _touch(root / "Perm" / "Album" / "01.flac")
    _touch(root / "Perm" / "cover.jpg")
    _touch(root / "Real" / "Album" / "01.flac")
    trash = tmp_path / "trash"

    os.chmod(root / "Perm" / "Album", 0o000)
    try:
        assert find_orphan_folders(root, seeds=None, trash_dir=trash) == []
        assert find_orphan_folders(root, seeds=[root / "Perm" / "Gone"], trash_dir=trash) == []
    finally:
        os.chmod(root / "Perm" / "Album", 0o755)

    assert find_orphan_folders(root, seeds=None, trash_dir=trash) == []

    # ... and a husk with nothing unreadable about it is still reported, so the
    # guard above is not a blanket "report nothing".
    _touch(root / "Old Artist" / "poster.jpg")
    assert find_orphan_folders(root, seeds=None, trash_dir=trash) == [root / "Old Artist"]


def test_the_seeds_climb_stops_at_a_symlinked_ancestor(tmp_path: Path) -> None:
    """A symlink is not a husk the mover may relocate.

    ``shutil.move`` moves the LINK, so everything beneath it leaves the library
    in one step and the Trash row is not restorable — ``trash._record_origin``
    writes no record for a symlinked destination and the listing renders such an
    entry refused. Measured on the parent commit with this fixture: seeds mode
    returned ``['Sub']``, the symlink holding the configured export dir.

    The assertion is two-sided. ``Sub`` is not reported (the climb stopped), and
    the real directory below it still is, so the stop did not turn the whole
    subtree off.
    """
    elsewhere = tmp_path / "elsewhere"
    _touch(elsewhere / "Album" / "cover.jpg")
    _touch(elsewhere / "exports" / "p1.m3u8")
    root = tmp_path / "music"
    root.mkdir()
    (root / "Sub").symlink_to(elsewhere)

    found = find_orphan_folders(
        root,
        seeds=[root / "Sub" / "Album" / "gone"],
        trash_dir=tmp_path / "trash",
        ignore_dirs=(root / "Sub" / "exports",),
    )

    assert found == [root / "Sub" / "Album"]


def test_a_protected_root_in_another_spelling_still_shields(tmp_path: Path) -> None:
    """``protected_dirs`` is compared in the same identity namespace as the exclusion.

    The set comes from the beets DB, which stores what the operator configured;
    the walk uses ``lib.directory``. With the library reached through a symlink
    the two spellings differ, and the string comparison alone reported the live
    album's own booklet folder. Both spellings are asserted for each shape, so a
    fix that dropped the string test would fail here too.

    All THREE shapes the filter documents are exercised, because they are three
    separate comparisons: the candidate's parent is a protected root (the
    booklet folder), the candidate IS one (an album whose audio has gone from
    disk), and a protected root lies strictly UNDER the candidate (the artist
    dir, whose one album is still live). Measured with the identity arm
    disabled, the first shape alone still passed — the parent test is a third
    comparison and covers it — so this test pinned one third of the change.
    """
    real = tmp_path / "tank" / "music"
    link = tmp_path / "music"
    real.mkdir(parents=True)
    link.symlink_to(real)
    _touch(real / "Live" / "Box" / "Disc 1" / "01.flac")
    _touch(real / "Live" / "Box" / "Scans (LP)" / "front.jpg")
    trash = tmp_path / "trash"

    # Unprotected, the booklet folder is the reported husk (pins the shape).
    assert find_orphan_folders(link, seeds=None, trash_dir=trash) == [
        link / "Live" / "Box" / "Scans (LP)"
    ]
    for spelling in (real / "Live" / "Box", link / "Live" / "Box"):
        assert (
            find_orphan_folders(link, seeds=None, trash_dir=trash, protected_dirs={str(spelling)})
            == []
        ), spelling

    # The album's audio is gone from disk: the candidate IS the protected root,
    # and its ancestor is what would be reported if the root were not spared.
    (real / "Live" / "Box" / "Disc 1" / "01.flac").unlink()
    _touch(real / "Live" / "Box" / "Disc 1" / "cover.jpg")
    assert find_orphan_folders(link, seeds=None, trash_dir=trash) == [link / "Live"]
    for spelling in (real / "Live" / "Box", link / "Live" / "Box"):
        # ``Live`` is an ANCESTOR of the root; ``Box`` IS one once ``Live`` is spared.
        assert (
            find_orphan_folders(link, seeds=None, trash_dir=trash, protected_dirs={str(spelling)})
            == []
        ), spelling


# --------------------------------------------------------------------------
# D3: the directory holding ``library.db`` is excluded, not refused.
# --------------------------------------------------------------------------


def test_the_ignore_list_names_the_directory_holding_the_beets_database(
    tmp_path: Path,
) -> None:
    """``library:`` may sit in an audio-free subfolder of the music root.

    ``app.beets.store_layout`` allows that on purpose — it refuses ``L`` only
    inside the Trash or the origin store — and ``<music>/db/library.db`` is then
    a directory whose whole content is a ``.db`` file and its SQLite sidecars:
    audio-empty, non-empty, parent audio-bearing, which is the husk shape
    exactly.

    The control is the same tree with that one entry removed from the tuple: it
    reports ``db``. Without it the assertion would pass on a tuple that had
    stopped carrying the entry at all.
    """
    from types import SimpleNamespace

    from app.api.reorganize import _ignore_dirs
    from app.config import Settings
    from tests.conftest import build_library, make_test_handle

    beets_dir = tmp_path / "data"
    beets_dir.mkdir()
    root = tmp_path / "music"
    _touch(root / "Real" / "Album" / "01.flac")
    db_dir = root / "db"
    db_dir.mkdir()

    handle = make_test_handle(build_library(str(db_dir / "library.db"), str(root)), beets_dir)
    app = SimpleNamespace(
        state=SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_origins_dir=str(tmp_path / "records"), playlists_export_dir=""),
        )
    )
    ignore = _ignore_dirs(app, tmp_path / "records")
    assert db_dir in ignore

    trash = beets_dir / "trash"
    assert find_orphan_folders(root, seeds=None, trash_dir=trash, ignore_dirs=ignore) == []
    without = tuple(d for d in ignore if d != db_dir)
    assert find_orphan_folders(root, seeds=None, trash_dir=trash, ignore_dirs=without) == [db_dir]


def test_the_ignore_list_holds_the_default_library_directory_once(tmp_path: Path) -> None:
    """On the DEFAULT layout ``library:`` resolves to ``<B>/library.db``.

    The beets dir would then appear twice and the finder — which logs one
    WARNING per at-or-above root — would log it twice for one directory. Pinned
    as a count rather than a membership, which is what a duplicate breaks.
    """
    from types import SimpleNamespace

    from app.api.reorganize import _ignore_dirs
    from app.config import Settings
    from tests.conftest import build_library, make_test_handle

    beets_dir = tmp_path / "data"
    beets_dir.mkdir()
    root = tmp_path / "music"
    root.mkdir()

    handle = make_test_handle(build_library(str(beets_dir / "library.db"), str(root)), beets_dir)
    app = SimpleNamespace(
        state=SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_origins_dir=str(tmp_path / "records"), playlists_export_dir=""),
        )
    )
    ignore = _ignore_dirs(app, tmp_path / "records")
    assert len(ignore) == len(set(ignore))
    assert ignore.count(beets_dir) == 1


def test_the_containment_helper_holds_for_the_filesystem_root() -> None:
    """``_under`` compares with ``commonpath``, not a ``root + os.sep`` prefix.

    For ``root == '/'`` that prefix is ``'//'`` and every absolute path reads as
    OUTSIDE. This is a unit assertion because the end-to-end route to it is
    ``directory: /`` — a music library configured at the filesystem root, which
    no fixture can build. Measured with the prefix form in place, the four cases
    below answer ``False, False, False, False``; the two ordinary ones are the
    control that says the helper still refuses a sibling and a self-comparison.

    The at-or-above WARNING does NOT depend on this: ``_exclude_ids`` decides
    that by climbing the walk root's ancestors and comparing inodes, so ``/``
    logs its one line whichever form ``_under`` takes. What this pins is the
    seeds climb, which walks up while ``_under`` says it is still inside.
    """
    from app.beets.orphans import _under

    assert _under("/x", "/") is True
    assert _under("/x/y", "/") is True
    assert _under("/", "/") is False
    assert _under("/xy", "/x") is False
    assert _under("/x/y", "/x") is True
    assert _under("relative/y", "/x") is False


def test_an_exclude_root_is_matched_by_identity_not_by_its_own_spelling(
    tmp_path: Path,
) -> None:
    """The unit half of the bind-mount pin, on a box with no mount namespace.

    A hard link cannot alias a directory, so the only in-process way to give one
    directory two paths is a symlink — and the exclusion is asked about the
    walk's spelling, which never contains the link. Here the exclude root is
    handed in resolved while the walk reaches it through a symlinked component,
    the shape ``realpath`` also handled; the bind-mount test above is what
    separates the two strategies. Kept because it runs everywhere.
    """
    real = tmp_path / "tank" / "music"
    real.mkdir(parents=True)
    link = tmp_path / "music"
    link.symlink_to(real)
    _touch(real / "Real" / "01.flac")
    _touch(real / "exports" / "p.m3u8")
    _touch(real / "exports" / "sub" / "n.txt")
    trash = tmp_path / "trash"

    assert find_orphan_folders(link, seeds=None, trash_dir=trash) == [link / "exports"]
    assert (
        find_orphan_folders(link, seeds=None, trash_dir=trash, ignore_dirs=(real / "exports",))
        == []
    )
