"""Repro: an in-library bank-apply must MOVE its source, never COPY it.

Bug (TrueNAS deploy): a multi-disc album banked from under the beets library
dir was re-imported via the sweep -> bank -> apply path and ended up DUPLICATED
on disk -- the original CD1/CD2 subfolders stayed intact AND a flattened,
re-tagged copy appeared at the album root. The chunk-1 in-library guard
(``run_import_worker`` -> ``is_in_library_source``) is supposed to force
move-mode for any source inside the library, but it did not fire.

These tests drive the REAL ``BeetsImportRunner`` with the exact translation the
bank-apply runner uses (``ImportOptions(operation="default")`` -> ``move=None``
+ a banked ``directive``), so the in-library guard is the only thing that can
flip the run from beets' default ``copy: yes`` to move.

``test_inlibrary_literal_apply_moves`` is the CONTROL: a source literally
string-prefixed by ``lib.directory`` -- the guard fires, the source is moved,
nothing is duplicated. It passes today.

``test_inlibrary_aliased_apply_moves_not_copies`` is the REPRO: the same
physical in-library source reached through a symlink alias (a TrueNAS
bind-mount / dataset-alias / case-folding analogue) so ``lib.directory`` and the
banked folder resolve to the same directory through DIFFERENT path strings.
``is_in_library_source`` does pure ``os.path.abspath`` prefix math with no
``os.path.realpath``, so the guard misses it, beets stays in copy mode, and the
in-library files are duplicated in place. This test asserts the source is moved
and therefore FAILS today -- it is the on-disk-duplication bug, reproduced.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

import pytest
from beets import config
from beets.library import Item, Library

from app.beets.import_session import ImportBridge, is_in_library_source
from app.import_jobs.runner import BeetsImportRunner
from app.models.bank import BankApplyDirective
from app.models.import_models import ImportOptions
from tests.conftest import build_library

SAMPLE = Path(__file__).parent / "fixtures" / "silent.flac"


def _make_tagged_flac(
    dst: Path, *, album: str, artist: str, title: str, track: int, disc: int
) -> None:
    """Copy the silent FLAC fixture to ``dst`` and embed real tags.

    ASIS imports re-read tags from the file during the read stage, so the seed
    files must carry album/artist/title/track/disc for beets to compute a
    destination that differs from the source (otherwise move vs copy is a no-op).
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SAMPLE, dst)
    item = Item(album=album, albumartist=artist, artist=artist, title=title, track=track, disc=disc)
    item.path = os.fsencode(str(dst))
    item.write()


def _build_multidisc(source_parent: Path) -> Path:
    """Seed ``<source_parent>/Artist - Album/CD1{,2}/...`` and return the album dir.

    Folder names CD1/CD2 trip beets' multi-disc collapse so the whole thing
    imports as ONE album (task.paths[0] = the album parent), mirroring the
    reported layout.
    """
    album_dir = source_parent / "Artist - Album"
    _make_tagged_flac(
        album_dir / "CD1" / "01 A.flac", album="Album", artist="Artist", title="A", track=1, disc=1
    )
    _make_tagged_flac(
        album_dir / "CD1" / "02 B.flac", album="Album", artist="Artist", title="B", track=2, disc=1
    )
    _make_tagged_flac(
        album_dir / "CD2" / "01 C.flac", album="Album", artist="Artist", title="C", track=1, disc=2
    )
    return album_dir


def _source_files(album_dir: Path) -> list[Path]:
    """Surviving source files under the original album dir (empty == moved)."""
    if not album_dir.exists():
        return []
    return [p for p in album_dir.rglob("*") if p.is_file()]


def _run_apply(lib: Library, source_album_dir: Path) -> None:
    """Drive a bank-apply ASIS run through the REAL runner and block until done.

    This is exactly what ``BankApplyRunner._apply_one`` does at the seam:
    ``registry.start(folder, options=ImportOptions(operation="default"),
    directive=...)`` -> ``BeetsImportRunner.run`` resolves ``move=None`` ->
    ``run_import_worker`` is the sole place the in-library guard can force move.
    ASIS keeps it hermetic (no candidate lookup, no network).
    """
    bridge = ImportBridge()
    runner = BeetsImportRunner(lib, trash_dir=None, bank_dir=None)
    directive = BankApplyDirective(action="asis")
    done = threading.Event()
    errors: list[str] = []

    def _on_error(message: str) -> None:
        errors.append(message)
        done.set()

    runner.run(
        str(source_album_dir),
        bridge,
        on_finish=done.set,
        on_error=_on_error,
        options=ImportOptions(operation="default"),
        directive=directive,
    )
    assert done.wait(timeout=30.0), "import worker did not finish in time"
    assert not errors, f"import worker errored: {errors}"


@pytest.fixture(autouse=True)
def _user_default_copy_mode() -> None:
    """Mirror the user's starter beets config: copy: yes (the manual default)."""
    config["threaded"] = False
    config["import"]["copy"] = True
    config["import"]["move"] = False


def _new_library(db_path: Path, music_dir: Path) -> Library:
    return build_library(str(db_path), str(music_dir))


def test_inlibrary_literal_apply_moves(tmp_path: Path) -> None:
    """CONTROL: a literally in-library source is recognized, so move is forced."""
    music = tmp_path / "music"
    music.mkdir()
    lib = _new_library(tmp_path / "library.db", music)
    album_dir = _build_multidisc(music / "incoming")  # literal /music/... prefix

    assert is_in_library_source(lib.directory, str(album_dir)) is True
    _run_apply(lib, album_dir)

    landed = sorted(p.name for p in (music / "Artist" / "Album").glob("*.flac"))
    assert landed == ["01 A.flac", "01 C.flac", "02 B.flac"], "album did not import"
    survivors = _source_files(album_dir)
    assert survivors == [], f"in-library source should be MOVED, but these remain: {survivors}"


def test_inlibrary_aliased_apply_moves_not_copies(tmp_path: Path) -> None:
    """REPRO (now fixed): same physical in-library source via a symlink alias.

    Before the fix the guard did ``abspath``-prefix math with no ``realpath``,
    misclassified the aliased-but-in-library source as OUTSIDE the library, beets
    stayed in copy mode, and the in-library files were duplicated in place. The
    hardened guard resolves both sides with ``realpath`` (collapsing the alias),
    recognizes the source as in-library, forces move-mode, and nothing is
    duplicated.
    """
    real_music = tmp_path / "real_music"
    real_music.mkdir()
    # TrueNAS analogue: the configured library path (directory: /library) reaches
    # the same dataset through a symlink/bind-mount, so its STRING differs from
    # the path beets walked for the source even though both are one directory.
    alias = tmp_path / "library"
    alias.symlink_to(real_music)
    lib = _new_library(tmp_path / "library.db", alias)
    album_dir = _build_multidisc(real_music / "incoming")  # physically in-library

    # The hardened guard resolves both sides with realpath, so the alias
    # collapses and the in-library source is recognized.
    assert is_in_library_source(lib.directory, str(album_dir)) is True

    _run_apply(lib, album_dir)

    # The flattened, re-tagged copy landed at the album root...
    landed = sorted(p.name for p in (real_music / "Artist" / "Album").glob("*.flac"))
    assert landed == ["01 A.flac", "01 C.flac", "02 B.flac"], "album did not import"

    # ...and the original CD1/CD2 files MUST be gone (moved). A regression would
    # leave them behind: the guard skips, beets copies, and the library holds the
    # album twice.
    survivors = _source_files(album_dir)
    assert survivors == [], (
        "in-library source was COPIED, not moved -- on-disk duplication. "
        f"Originals still present: {[str(p) for p in survivors]}"
    )
