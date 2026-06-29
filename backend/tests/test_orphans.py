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


def test_trash_folder_moves_whole_folder(tmp_path: Path) -> None:
    from app.beets.trash import trash_folder

    husk = tmp_path / "music" / "Old Name"
    _touch(husk / "artist-poster.jpg")
    _touch(husk / "artist-background.jpg")
    trash = tmp_path / "trash"

    dest = trash_folder(husk, trash_dir=trash)

    assert not husk.exists()  # source gone
    assert dest.parent == trash and dest.name == "Old Name"
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


def test_art_only_husk_does_not_break_trash_listing(tmp_path: Path) -> None:
    from app.beets.trash import trash_folder
    from app.beets.trash_manage import list_trashed_albums

    husk = tmp_path / "music" / "Old Name"
    _touch(husk / "artist-poster.jpg")  # no audio
    trash = tmp_path / "trash"
    trash_folder(husk, trash_dir=trash)

    # list_trashed_albums groups by audio tags; an art-only folder yields no album
    # row (it is skipped), so the album-restore listing stays clean.
    assert list_trashed_albums(trash) == []
