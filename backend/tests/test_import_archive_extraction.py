"""Real beets, real archive: a tar handed to the importer cannot write outside.

beets unpacks a single archive FILE toppath with a bare ``extractall`` (2.14.0
``importer/tasks.py:1272``). ``app.beets.import_session`` sets PEP 706's
``data_filter`` as the tarfile default when it loads, so beets refuses the
escaping member and logs its own "extraction failed" (``tasks.py:1455-1456``).
These tests never set that default themselves: they run the app's own
``WebImportSession`` + ``run_import_worker``, so removing the production line
makes them fail.

Everything stays under ``tmp_path`` even with the default removed: beets'
``mkdtemp`` is pointed there, and every escape target sits beside it.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import beets.importer.tasks as beets_tasks
import pytest
from beets import config
from beets.autotag import AlbumInfo, AlbumMatch, Source, TrackInfo
from beets.autotag.distance import distance
from beets.autotag.match import Proposal, assign_items
from beets.autotag.match import Recommendation as BeetsRec

from app.beets.import_session import ImportBridge, WebImportSession, run_import_worker
from app.models.import_models import ImportAction, ImportChoice

if TYPE_CHECKING:
    from beets.library import Library

_ALBUM = "OK Computer"
_ARTIST = "Radiohead"
_DEADLINE_S = 30.0
_PAYLOAD = b"plugins: hook\n"


def _tagged_flac(dst: Path, i: int) -> None:
    from mediafile import MediaFile

    shutil.copyfile(Path(__file__).parent / "fixtures" / "silent.flac", dst)
    mf = MediaFile(str(dst))
    mf.artist = _ARTIST
    mf.albumartist = _ARTIST
    mf.album = _ALBUM
    mf.title = f"Airbag {i}"
    mf.track = i
    mf.save()


def _install_strong_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin beets' lookup to one strong match built from the items (auto-applies)."""

    def fake_tag_album(source: Source, search_ids: Any = None) -> Proposal:
        item_list = list(source.items)
        info = AlbumInfo(
            tracks=[
                TrackInfo(title=f"Airbag {i}", track_id=f"t{i}", index=i, length=1.0)
                for i in range(1, len(item_list) + 1)
            ],
            album=_ALBUM,
            artist=_ARTIST,
            album_id="mb-okc",
            data_source="MusicBrainz",
            data_url="https://mb/okc",
            year=1997,
            va=False,
        )
        pairs, extra_items, extra_tracks = assign_items(item_list, info.tracks)
        match = AlbumMatch(
            distance(source.data, info, pairs, len(extra_items)),
            info,
            dict(pairs),
            extra_items,
            extra_tracks,
        )
        return Proposal([match], BeetsRec.strong)

    monkeypatch.setattr(beets_tasks, "tag_album", fake_tag_album)


@pytest.fixture
def lib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Library:
    """A real library in copy mode, with beets' extract dir inside ``tmp_path``."""
    from tests.conftest import build_library

    extract_root = tmp_path / "tmp"
    extract_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(extract_root))
    _install_strong_lookup(monkeypatch)
    config["import"]["copy"] = True
    config["statefile"] = str(tmp_path / "state.pickle")
    music = tmp_path / "music"
    music.mkdir()
    return build_library(str(tmp_path / "library.db"), str(music))


def _import(lib: Library, archive: Path) -> list[str]:
    """Import one archive file on its own thread, skipping anything it parks."""
    bridge = ImportBridge()
    errors: list[str] = []

    def worker() -> None:
        session = WebImportSession(
            lib,
            None,
            [os.fsencode(str(archive))],
            None,
            bridge,
            None,
            trash_origins_dir=None,
            unattended=False,
            sweep=False,
            bank_dir=None,
            directive=None,
            playlists_dir=None,
        )
        try:
            run_import_worker(session)
        except Exception as exc:  # recorded for the assertions, not swallowed
            errors.append(f"{exc.__class__.__name__}: {exc}")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    deadline = time.monotonic() + _DEADLINE_S
    while thread.is_alive() and time.monotonic() < deadline:
        album = bridge.get_parked(timeout=0.05)
        if album is not None:
            bridge.push_choice(album.album_index, ImportChoice(action=ImportAction.skip))
    if thread.is_alive():
        bridge.request_stop()
    thread.join(timeout=5.0)
    assert not thread.is_alive(), "import worker hung"
    return errors


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _album_tar(tmp_path: Path, extra: Any = None) -> Path:
    """A gzipped album tar saved under an audio-looking name, as the seat measured.

    ``extra`` adds members after the two tracks.
    """
    staging = tmp_path / "staging"
    staging.mkdir()
    archive = tmp_path / "downloads" / "Some Album.flac"
    archive.parent.mkdir()
    with tarfile.open(archive, "w:gz") as tar:
        for i in (1, 2):
            track = staging / f"{i:02d} Track {i}.flac"
            _tagged_flac(track, i)
            tar.add(track, arcname=f"album/{track.name}")
        if extra is not None:
            extra(tar)
    return archive


def test_a_normal_album_tar_imports(lib: Library, tmp_path: Path) -> None:
    errors = _import(lib, _album_tar(tmp_path))

    assert errors == []
    titles = sorted(str(item.title) for item in lib.items())
    assert titles == ["Airbag 1", "Airbag 2"]


def _dotdot(tmp_path: Path) -> tuple[Any, Path]:
    # beets extracts into tmp_path/tmp/<mkdtemp>/, so two levels up is tmp_path.
    target = tmp_path / "escaped-dotdot.yaml"
    return (lambda tar: _add_bytes(tar, "../../escaped-dotdot.yaml", _PAYLOAD)), target


def _absolute(tmp_path: Path) -> tuple[Any, Path]:
    target = tmp_path / "escaped-absolute.yaml"
    return (lambda tar: _add_bytes(tar, str(target), _PAYLOAD)), target


def _through_symlink(tmp_path: Path) -> tuple[Any, Path]:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "escaped-symlink.yaml"

    def add(tar: tarfile.TarFile) -> None:
        link = tarfile.TarInfo("album/link")
        link.type = tarfile.SYMTYPE
        link.linkname = str(outside)
        tar.addfile(link)
        _add_bytes(tar, "album/link/escaped-symlink.yaml", _PAYLOAD)

    return add, target


@pytest.mark.parametrize("member", [_dotdot, _absolute, _through_symlink])
def test_a_tar_member_that_escapes_is_refused(
    lib: Library,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    member: Any,
) -> None:
    add, target = member(tmp_path)
    archive = _album_tar(tmp_path, add)

    with caplog.at_level(logging.ERROR, logger="beets"):
        errors = _import(lib, archive)

    assert not target.exists(), f"a tar member was written outside beets' extract dir: {target}"
    assert errors == []
    assert any("extraction failed" in r.getMessage() for r in caplog.records)
    assert list(lib.items()) == []
