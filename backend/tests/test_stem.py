"""The shared filename-stem helper used by both m3u parsing and library matching.

One helper so the parse side (strips a leading track number) and the match side
(does not) can't silently drift apart.
"""

from __future__ import annotations

from app.playlists.stem import filename_stem


def test_drops_directory_and_extension() -> None:
    assert filename_stem("/old/Music/Daft Punk/Around the World.mp3") == "Around the World"


def test_handles_windows_separators_and_drive_letter() -> None:
    assert filename_stem("C:\\Music\\Queen\\Somebody to Love.flac") == "Somebody to Love"


def test_no_extension_is_kept_whole() -> None:
    assert filename_stem("/x/README") == "README"


def test_keeps_leading_track_number_by_default() -> None:
    # The match side must NOT strip track numbers — the raw stem is the key.
    assert filename_stem("/x/07 - Somebody to Love.flac") == "07 - Somebody to Love"


def test_strips_leading_track_number_when_requested() -> None:
    # The parse side derives a title, so it strips the leading track number.
    assert (
        filename_stem("/x/07 - Somebody to Love.flac", strip_track_number=True)
        == "Somebody to Love"
    )


def test_strip_track_number_empties_to_blank_string() -> None:
    # "07 - " with nothing after it collapses to "" (caller maps "" -> None).
    assert filename_stem("/x/07 - .mp3", strip_track_number=True) == ""
