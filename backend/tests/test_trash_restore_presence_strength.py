"""Restore's mount guard is the STRONGER predicate, in the state that tells them apart.

README says Restore "runs that same stronger check" as delete, and every restore
test that asserted a 503 built a music root the CHEAP predicate already refuses:
``shutil.rmtree(music)`` (root missing) and ``rmtree`` + ``mkdir`` (root empty).
``require_library_root`` raises on both, so swapping
``restore_album``'s ``require_library_present`` for it changed nothing anyone
could see — measured: the swap survived the whole suite.

The gap state is the one ``require_library_present`` exists for: the share is
gone but its LOCAL mountpoint survives holding one entry nobody thinks of as
music (a ``.stfolder``), so "the root has entries" is satisfied while none of the
library's albums are really there. Each test below asserts the cheap check
ACCEPTS that root before asserting the restore refuses it, because "the restore
raised" is only evidence about strength beside "the weak predicate would have
let it through".

Both arms of ``restore_album`` are covered — the branch is on the record, the
guard is above it — so the parametrisation is the strength claim for the whole
endpoint rather than for one of its two ways of putting a folder back.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from beets import config
from beets.library import Item, Library

from app.beets.library import LibraryRootUnavailableError, require_library_root
from app.beets.trash_manage import restore_album
from app.beets.trash_origins import write_trash_origin
from tests.conftest import build_library, origins_for

SAMPLE = Path(__file__).parent / "fixtures" / "silent.flac"


@pytest.fixture(autouse=True)
def _serial() -> Iterator[None]:
    # Single-threaded, copy-mode: only the MUTATED run ever reaches the import,
    # and a threaded import worker there would report a hang rather than a
    # survivor.
    config["threaded"] = False
    config["import"]["copy"] = True
    config["import"]["move"] = False
    yield
    config["threaded"] = False


def _tagged_flac(dst: Path, *, artist: str, album: str, title: str, track: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SAMPLE, dst)
    item = Item(album=album, albumartist=artist, artist=artist, title=title, track=track)
    item.path = os.fsencode(str(dst))
    item.write()


def _library_whose_music_is_gone(tmp_path: Path) -> Library:
    """A library holding one real album, then the share it lives on drops.

    No bystander on purpose: this IS the dropped-share scenario, so EVERY file
    the library believes it owns has to be missing — a surviving FILE (one per
    sampled album, its ``MIN(path)``) is exactly what ``require_library_present``
    looks for, and seeding one would make the guard pass and the test assert
    nothing. ``rmtree`` of the whole music dir is what makes that true here
    regardless of the path template's depth.
    """
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))
    dst = music / "Radiohead" / "Amnesiac" / "01 Packt.flac"
    _tagged_flac(dst, artist="Radiohead", album="Amnesiac", title="Packt", track=1)
    item = Item(album="Amnesiac", albumartist="Radiohead", artist="Radiohead", title="Packt")
    item.path = os.fsencode(str(dst))
    lib.add_album([item]).store()

    shutil.rmtree(music)
    music.mkdir()
    (music / ".stfolder").mkdir()  # the stray entry the cheap predicate accepts
    return lib


@pytest.mark.parametrize("with_record", [True, False], ids=["move_back", "import"])
def test_restore_refuses_a_dropped_share_a_stray_entry_hides(
    tmp_path: Path, with_record: bool
) -> None:
    lib = _library_whose_music_is_gone(tmp_path)
    music = tmp_path / "music"
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    entry = trash / "Weird Folder"
    _tagged_flac(
        entry / "01 Mysterons.flac",
        artist="Portishead",
        album="Dummy",
        title="Mysterons",
        track=1,
    )
    if with_record:
        write_trash_origin(origins, entry.name, origin=str(music / "Weird Folder"), moved="folder")

    require_library_root(lib)  # accepts the stray-entry mountpoint — this is the gap

    with pytest.raises(LibraryRootUnavailableError):
        restore_album(lib, str(entry), trash_dir=trash, origins_dir=origins)

    assert list(entry.glob("*.flac")), "the files must not have left Trash"
    assert list(music.iterdir()) == [music / ".stfolder"], "nothing may land on the mountpoint"
