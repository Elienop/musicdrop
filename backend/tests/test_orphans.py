from __future__ import annotations

from pathlib import Path

from app.beets.orphans import find_orphan_folders


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


def test_trash_folder_moves_whole_folder(tmp_path: Path) -> None:
    from app.beets.trash import trash_folder

    husk = tmp_path / "music" / "Old Name"
    _touch(husk / "artist-poster.jpg")
    _touch(husk / "artist-background.jpg")
    trash = tmp_path / "trash"

    dest = trash_folder(husk, trash_dir=trash)

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

    dest = trash_folder(husk, trash_dir=trash)
    assert dest.name == "Old Name (1)"
    assert (dest / "cover.jpg").exists()


def test_art_only_husk_is_listed_for_visibility(tmp_path: Path) -> None:
    from app.beets.trash import trash_folder
    from app.beets.trash_manage import list_trashed_albums

    husk = tmp_path / "music" / "Old Name"
    _touch(husk / "artist-poster.jpg")  # no audio
    trash = tmp_path / "trash"
    trash_folder(husk, trash_dir=trash)

    # Audio-free trashed folders (husks the orphan sweep moves here) must appear in
    # the listing as zero-track entries — otherwise the Trash UI never shows them
    # and Empty-all deletes them silently. Now visible + individually empty-able.
    listed = list_trashed_albums(trash)
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
        failures=[],
        finished_at=None,
    )
    assert status.orphans_trashed == 3
