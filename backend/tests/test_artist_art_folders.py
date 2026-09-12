"""Which FOLDERS the artist-art writer will touch, and how it reads them.

Three questions live here, all about the folder rather than the file:
:func:`get_artist_dirs`' skip rule, :func:`has_background`'s read, and the arm
that reports ``failed`` when a folder's listing fails. The per-file write,
skip-existing and Trash behaviour stay in ``test_artist_art_write.py``.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from app.beets.artist_art import ArtTrashStore, get_artist_dirs, has_background, write_artist_art

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, "image/png")


class _StubAlbum:
    """An album with just the two attributes ``get_artist_dirs`` reads."""

    def __init__(self, item_dir: Path, *, comp: int = 0) -> None:
        self._item_dir = item_dir
        self.comp = comp

    def item_dir(self) -> bytes:
        return os.fsencode(str(self._item_dir))  # beets hands back bytes


class _StubLib:
    """A library whose root and album set are fixed by the test.

    A real ``Library`` cannot hold an album whose ``item_dir()`` IS the root or
    sits behind a symlink — beets' own ``destination()`` would place it — and
    both are exactly the layouts these tests are about.
    """

    def __init__(self, root: Path, albums: list[_StubAlbum]) -> None:
        self.directory = os.fsencode(str(root))
        self._albums = albums

    def music_dir_context(self) -> Any:
        return contextlib.nullcontext()

    def albums(self, _query: Any) -> list[_StubAlbum]:
        return self._albums


@pytest.fixture
def art_trash(tmp_path: Path) -> ArtTrashStore:
    return ArtTrashStore(trash_dir=tmp_path / "trash", origins_dir=tmp_path / "trash-origins")


def test_a_symlinked_artist_folder_has_no_background_to_read(tmp_path: Path) -> None:
    """``has_background`` decides whether the fanart.tv fetch is skipped, so it
    must answer for the folder the WRITER would reach. Measured before this: it
    globbed by path, followed the link and answered True for a folder the writer
    refuses — the fetch was skipped for art that could never be written.

    A folder that cannot be opened counts as LACKING the file, which is the
    conservative side: the fetch runs and the writer reports the refusal.
    """
    root = tmp_path / "music"
    root.mkdir()
    outside = tmp_path / "outside"
    (outside / "Album").mkdir(parents=True)
    (outside / "artist-background.jpg").write_bytes(b"x")
    os.symlink(outside, root / "Artist")
    lib = _StubLib(root, [_StubAlbum(root / "Artist" / "Album")])

    assert get_artist_dirs(lib, "A") == [root / "Artist"]  # the folder IS in scope
    assert has_background(lib, "A") is False


def test_a_real_artist_folder_still_reports_its_background(tmp_path: Path) -> None:
    """The control for the test above: through a real component the read still
    finds the file, so the False there is the link and not the rewrite."""
    root = tmp_path / "music"
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    (root / "Artist" / "artist-background.jpg").write_bytes(b"x")
    lib = _StubLib(root, [_StubAlbum(album)])

    assert has_background(lib, "A") is True


def test_a_background_name_is_matched_case_sensitively(tmp_path: Path) -> None:
    """``fnmatchcase`` over the names, the twin of the writer's own plan and of
    the ``Path.glob`` this replaced (case-sensitive on POSIX)."""
    root = tmp_path / "music"
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    (root / "Artist" / "Artist-Background.JPG").write_bytes(b"x")
    lib = _StubLib(root, [_StubAlbum(album)])

    assert has_background(lib, "A") is False


def test_a_flat_library_answers_no_folder_instead_of_failed(
    tmp_path: Path, art_trash: ArtTrashStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A flat ``path_formats`` leaves the tracks directly in the library root, so
    the album's item dir IS the root and its parent is the directory ABOVE it.

    Measured before this: that album escaped the ``parent == root`` skip, and
    every artist was reported ``failed`` with a per-folder traceback recommending
    a bind mount — for a layout whose honest answer the module already has.
    Before the descriptor work it was worse: ``artist-poster.png`` was written
    into the directory above the library root.
    """
    root = tmp_path / "music"
    root.mkdir()
    lib = _StubLib(root, [_StubAlbum(root)])

    assert get_artist_dirs(lib, "A") == []

    with caplog.at_level(logging.WARNING, logger="app.beets.artist_art"):
        out = write_artist_art(lib, "A", poster=PNG, background=None, force=True, trash=art_trash)

    assert out.status == "no_folder"
    assert out.dirs == 0
    assert caplog.records == []
    assert sorted(p.name for p in root.iterdir()) == []  # nothing in the root
    assert sorted(p.name for p in tmp_path.iterdir()) == ["music"]  # nor above it


def test_a_folder_whose_listing_fails_is_reported_with_a_reason(
    tmp_path: Path,
    art_trash: ArtTrashStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The arm that catches a listing failure used to report ``failed`` with
    nothing in the logs, unlike the arms on either side of it."""
    root = tmp_path / "music"
    folder = root / "Artist"
    (folder / "Album").mkdir(parents=True)
    lib = _StubLib(root, [_StubAlbum(folder / "Album")])

    real_scandir = os.scandir

    def failing_scandir(target: Any = ".") -> Any:
        if isinstance(target, int):  # only the fd listing, not a path one
            raise OSError(5, "I/O error")
        return real_scandir(target)

    monkeypatch.setattr(os, "scandir", failing_scandir)

    with caplog.at_level(logging.WARNING, logger="app.beets.artist_art"):
        out = write_artist_art(lib, "A", poster=PNG, background=None, force=True, trash=art_trash)

    assert out.status == "failed"
    assert out.written == 0
    assert len(caplog.records) == 1
    assert str(folder) in caplog.records[0].getMessage()
    assert caplog.records[0].exc_info is not None  # the traceback IS the reason
