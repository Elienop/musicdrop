"""The album detail says when some of the album's files are not in the library.

Slice 5 of the import rebuild. Measured through the real in-process import
(``test_import_incremental_e2e.test_a_stop_during_placement_leaves_rows_naming_the_download``):
a stop during placement leaves the placed rows on library paths and the unplaced
rows on the download path, stored absolute, with no state file and no history
entry. The rows are the only record, so the detail endpoint reports the fact and
names one folder the user can act on.

The same fact is true, by design, for an ``in_place`` import, for every album
after the user edits ``directory:``, and for rows left in a Trash outside the
music folder. The field states the fact; the remedy sentence is the frontend's
and is conditional.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from beets import context
from beets.library import Item, Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.beets.library import _require_id, get_album_detail
from app.main import app
from tests.conftest import beets_dir_for, build_library, make_test_handle


def _library(tmp_path: Path) -> Library:
    music = tmp_path / "music"
    music.mkdir(exist_ok=True)
    return build_library(str(tmp_path / "library.db"), str(music))


def _album(lib: Library, *paths: bytes) -> int:
    """One album whose item rows name ``paths``, in order."""
    items = []
    for i, raw in enumerate(paths, start=1):
        item = Item(album="Alb", albumartist="Art", title=f"T{i}", track=i)
        item.path = raw
        items.append(item)
    album = lib.add_album(items)
    return _require_id(album.id)


def _inside(lib: Library, name: str) -> bytes:
    return os.path.join(lib.directory, os.fsencode(name))


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    lib = _library(tmp_path)
    handle = make_test_handle(lib, beets_dir_for(tmp_path))
    app.dependency_overrides[get_library] = lambda: handle
    client = TestClient(app)
    client.lib = lib  # type: ignore[attr-defined]  # the test needs the rows
    yield client
    app.dependency_overrides.clear()


def _folder(lib: Library, album_id: int) -> str | None:
    detail = get_album_detail(lib, album_id)
    assert detail is not None
    return detail.folder_outside_library


def test_an_album_whose_files_are_all_in_the_library_names_no_folder(tmp_path: Path) -> None:
    """The control: a whole album carries nothing."""
    lib = _library(tmp_path)
    aid = _album(lib, _inside(lib, "Art/Alb/01 T1.mp3"), _inside(lib, "Art/Alb/02 T2.mp3"))
    assert _folder(lib, aid) is None


def test_a_half_placed_album_names_the_folder_holding_the_outside_file(tmp_path: Path) -> None:
    """The stop-during-placement shape: one row placed, one still on the download.

    The folder named is the one really holding the outside file, not the library
    folder the placed row sits in.
    """
    lib = _library(tmp_path)
    download = tmp_path / "downloads" / "okc"
    aid = _album(
        lib,
        _inside(lib, "Art/Alb/01 T1.mp3"),
        os.fsencode(str(download / "02 Track 2.flac")),
    )
    assert _folder(lib, aid) == str(download)


def test_an_in_place_album_outside_the_library_is_reported_too(tmp_path: Path) -> None:
    """Intended: an ``in_place`` import files nothing, so every row is outside."""
    lib = _library(tmp_path)
    elsewhere = tmp_path / "elsewhere" / "Alb"
    aid = _album(lib, os.fsencode(str(elsewhere / "01 T1.mp3")))
    assert _folder(lib, aid) == str(elsewhere)


def test_rows_left_in_a_trash_outside_the_music_folder_are_reported(tmp_path: Path) -> None:
    """Documented, not special-cased.

    A Delete whose Trash move worked but whose row removal failed leaves rows in
    ``<beets_dir>/trash`` — outside the music folder by default. The stated fact
    is still true there, so the field answers it.
    """
    lib = _library(tmp_path)
    trashed = tmp_path / "data" / "trash" / "Art - Alb"
    aid = _album(lib, os.fsencode(str(trashed / "01 T1.mp3")))
    assert _folder(lib, aid) == str(trashed)


def test_the_answer_does_not_depend_on_an_inherited_music_dir_context(tmp_path: Path) -> None:
    """The read binds beets' music dir itself.

    beets stores an inside-library row RELATIVE and expands it through a
    ``ContextVar`` a FastAPI threadpool thread does not inherit. Unbound and
    unexpanded, every row of a whole album would read as outside.
    """
    lib = _library(tmp_path)
    aid = _album(lib, _inside(lib, "Art/Alb/01 T1.mp3"))
    with context.music_dir(b""):
        assert _folder(lib, aid) is None


def test_the_containment_question_never_touches_the_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unmounted library must not change the answer or raise.

    ``os.stat`` is made to raise for the whole read: the answer is unchanged.
    """
    lib = _library(tmp_path)
    inside_id = _album(lib, _inside(lib, "Art/Alb/01 T1.mp3"))
    outside_id = _album(lib, os.fsencode(str(tmp_path / "dl" / "Alb" / "01 T1.mp3")))

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise OSError("library is not mounted")

    monkeypatch.setattr(os, "stat", refuse)
    assert _folder(lib, inside_id) is None
    assert _folder(lib, outside_id) == str(tmp_path / "dl" / "Alb")


def test_a_relative_row_answers_instead_of_raising(tmp_path: Path) -> None:
    """``os.path.abspath`` is what lets ``commonpath`` take an unexpanded row.

    Mixing a relative and an absolute path raises ``ValueError`` there, which
    would 500 the endpoint. The ANSWER is only meaningful with beets' music dir
    bound (the test above); this pins that a caller who forgets still gets one.
    """
    from app.beets.library import _inside_library

    lib = _library(tmp_path)
    assert _inside_library(lib, SimpleNamespace(path=b"Art/Alb/01 T1.mp3")) is False


def test_a_row_with_no_path_names_no_folder(tmp_path: Path) -> None:
    """A NULL/empty path row names no folder a user could act on, so it is not one."""
    lib = _library(tmp_path)
    aid = _album(lib, b"", _inside(lib, "Art/Alb/02 T2.mp3"))
    assert _folder(lib, aid) is None


def test_the_containment_predicate_has_exactly_one_implementation() -> None:
    """The edit adapter's move phase and this read MUST ask the same question.

    A second copy is how one surface starts calling a file outside the library
    inside. ``vars`` rather than attribute access: the name is private.
    """
    from app.beets import edit as edit_mod
    from app.beets import library as library_mod

    assert vars(edit_mod)["_inside_library"] is vars(library_mod)["_inside_library"]


def test_an_undecodable_folder_answers_instead_of_500ing(client: TestClient) -> None:
    """Hostile input: a download folder whose name is not valid UTF-8."""
    lib: Library = client.lib  # type: ignore[attr-defined]
    root = os.fsencode(str(Path(os.fsdecode(lib.directory)).parent))
    aid = _album(lib, root + b"/Caf\xe9/01.mp3")

    resp = client.get(f"/api/albums/{aid}")
    assert resp.status_code == 200
    folder = resp.json()["folder_outside_library"]
    assert folder is not None
    assert folder.endswith("\ufffd")  # the wire sink degraded the byte


def test_a_very_long_folder_answers(client: TestClient) -> None:
    """Hostile input: a folder at the component-length limit."""
    lib: Library = client.lib  # type: ignore[attr-defined]
    long_name = "x" * 255
    root = Path(os.fsdecode(lib.directory)).parent / long_name
    aid = _album(lib, os.fsencode(str(root / "01.mp3")))

    resp = client.get(f"/api/albums/{aid}")
    assert resp.status_code == 200
    assert resp.json()["folder_outside_library"] == str(root)


def test_the_field_is_on_the_detail_contract(client: TestClient) -> None:
    """The client is always told, so a whole album answers ``null`` rather than absent."""
    lib: Library = client.lib  # type: ignore[attr-defined]
    aid = _album(lib, _inside(lib, "Art/Alb/01 T1.mp3"))

    body = client.get(f"/api/albums/{aid}").json()
    assert "folder_outside_library" in body
    assert body["folder_outside_library"] is None
