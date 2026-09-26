"""The folder browser, ``GET /api/folders`` (branch 2, S7; ``decisions`` #54, #77).

One folder's direct child folders, hidden and sorted as beets' import walk
hides and sorts them, capped at 500, badged and refused off the rows the import
start refuses from. The layout helper builds the image's shape under one tmp
``root`` standing in for ``/``: ``/media/music`` and ``/data/beets``.
"""

from __future__ import annotations

import errno
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import beets
import httpx
import pytest
from beets.library import Library
from fastapi.testclient import TestClient

from app.acquisition.ledger import AcquisitionLedger
from app.api import folders as folders_api
from app.api.import_ import AMBIGUOUS_FOLDERS_DETAIL
from app.auth.session import SESSION_COOKIE_NAME
from app.beets.import_walk import walk_rules
from app.beets.store_layout import (
    SOURCE_HOLDS_APP_DATA,
    SOURCE_IS_THE_INBOX,
    SOURCE_IS_THE_LIBRARY,
)
from app.config import Settings
from app.import_jobs.registry import get_registry
from app.import_jobs.runner import (
    BeetsImportRunner,
    ImportSourceRefusedError,
    unreadable_source_sentence,
)
from app.main import app
from app.wire import PLACEHOLDER
from tests.conftest import build_library, session_cookie_value

_SENTINEL = object()


def _list(path: str | Path | None = None) -> dict[str, Any]:
    params = {} if path is None else {"path": str(path)}
    response = TestClient(app).get("/api/folders", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _names(body: dict[str, Any]) -> list[str]:
    return [entry["name"] for entry in body["folders"]]


def _dirs(parent: Path, *names: str) -> Path:
    for name in names:
        (parent / name).mkdir(parents=True, exist_ok=True)
    return parent


# ----- what is listed --------------------------------------------------------------


def test_only_folders_are_listed_and_a_link_to_one_counts(tmp_path: Path) -> None:
    """Invariant 1: never files; a link to a folder is one; a broken link is not."""
    _dirs(tmp_path, "Album", "Elsewhere/Target")
    (tmp_path / "cover.jpg").write_bytes(b"x")
    (tmp_path / "Linked").symlink_to(tmp_path / "Elsewhere" / "Target")
    (tmp_path / "Dangling").symlink_to(tmp_path / "nowhere")
    (tmp_path / "file-link").symlink_to(tmp_path / "cover.jpg")

    body = _list(tmp_path)

    assert _names(body) == ["Album", "Elsewhere", "Linked"]
    assert body["total"] == 3
    assert body["folders"][0]["path"] == str(tmp_path / "Album")


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("bad", id="loops-ELOOP"),
        pytest.param("../file.txt/sub", id="through-a-file-ENOTDIR"),
        pytest.param("x" * 300, id="too-long-ENAMETOOLONG"),
        pytest.param(
            "../locked/x",
            id="into-a-locked-folder-EACCES",
            marks=pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 folder"),
        ),
    ],
)
def test_a_link_whose_stat_fails_is_not_a_folder_and_the_rest_still_list(
    tmp_path: Path, target: str
) -> None:
    """A link beets' ``os.path.isdir`` calls a file never fails or moves the listing.

    Each target makes ``stat`` raise (not "missing"): the folder beside it still
    lists, at its own path, with 200, rather than a 422 or its parent.
    """
    folder = _dirs(tmp_path, "F/Album", "locked") / "F"
    (tmp_path / "file.txt").write_bytes(b"x")
    (folder / "bad").symlink_to(target)
    (tmp_path / "locked").chmod(0o000)
    try:
        body = _list(folder)
    finally:
        (tmp_path / "locked").chmod(0o755)

    assert (body["path"], _names(body), body["total"]) == (str(folder), ["Album"], 1)


def test_beets_default_ignore_hides_what_an_import_skips(tmp_path: Path) -> None:
    """Invariant 2, with beets' defaults: ``.*``, ``*~``, ``lost+found`` are left out.

    ACCEPT: a plain folder and a dotted name that does not START with a dot stay.
    """
    _dirs(tmp_path, ".x", "x~", "lost+found", "plain", "x.y")

    assert _names(_list(tmp_path)) == ["plain", "x.y"]


def test_a_custom_ignore_glob_hides_its_folders(tmp_path: Path) -> None:
    _dirs(tmp_path, "keep", "skip-me", "skip-too")
    beets.config["ignore"].set(["skip-*"])

    assert _names(_list(tmp_path)) == ["keep"]


def test_ignore_hidden_hides_a_dot_folder_no_glob_names(tmp_path: Path) -> None:
    """beets' own dot-name rule, on its own: no glob matches, ``ignore_hidden`` hides it."""
    _dirs(tmp_path, ".hidden", "shown")
    beets.config["ignore"].set([])

    assert _names(_list(tmp_path)) == ["shown"]


def test_with_both_keys_off_a_dot_folder_is_listed(tmp_path: Path) -> None:
    _dirs(tmp_path, ".hidden", "shown")
    beets.config["ignore"].set([])
    beets.config["ignore_hidden"].set(False)

    assert _names(_list(tmp_path)) == [".hidden", "shown"]


def test_a_typed_hidden_path_still_lists(tmp_path: Path) -> None:
    _dirs(tmp_path, ".hidden/inner")

    body = _list(tmp_path / ".hidden")

    assert (body["path"], _names(body)) == (str(tmp_path / ".hidden"), ["inner"])


def test_folders_are_sorted_ignoring_case(tmp_path: Path) -> None:
    _dirs(tmp_path, "banana", "Apple", "cherry", "Banana2")

    assert _names(_list(tmp_path)) == ["Apple", "banana", "Banana2", "cherry"]


def test_at_most_500_folders_are_listed_and_total_counts_them_all(tmp_path: Path) -> None:
    _dirs(tmp_path, *(f"f{index:03d}" for index in range(501)))

    body = _list(tmp_path)

    assert len(body["folders"]) == 500
    assert body["total"] == 501
    assert body["folders"][-1]["name"] == "f499"


# ----- names that are not UTF-8 ----------------------------------------------------


def test_a_non_utf8_name_round_trips(tmp_path: Path) -> None:
    """Invariant 4: out through ``display_path``, back in through ``resolve_posted_path``."""
    base = os.fsencode(tmp_path)
    os.makedirs(os.path.join(base, b"Caf\xe9", b"inner"))

    listed = _list(tmp_path)
    shown = listed["folders"][0]
    opened = _list(shown["path"])

    assert shown["name"] == f"Caf{PLACEHOLDER}"
    assert shown["path"] == f"{tmp_path}/Caf{PLACEHOLDER}"
    assert (opened["path"], _names(opened)) == (shown["path"], ["inner"])


def test_two_folders_that_display_alike_answer_409(tmp_path: Path) -> None:
    base = os.fsencode(tmp_path)
    os.mkdir(os.path.join(base, b"Caf\xe9"))
    os.mkdir(os.path.join(base, b"Caf\xe8"))

    response = TestClient(app).get("/api/folders", params={"path": f"{tmp_path}/Caf{PLACEHOLDER}"})

    assert (response.status_code, response.json()) == (409, {"detail": AMBIGUOUS_FOLDERS_DETAIL})


# ----- where it opens --------------------------------------------------------------


def test_a_missing_path_lists_its_nearest_parent(tmp_path: Path) -> None:
    """Invariant 5: a Recent folder moved away still opens nearby."""
    _dirs(tmp_path, "Music/Artist")

    body = _list(tmp_path / "Music" / "Gone" / "Album")

    assert (body["path"], _names(body)) == (str(tmp_path / "Music"), ["Artist"])


def test_a_path_is_stripped_as_the_start_strips_it(tmp_path: Path) -> None:
    """Use this folder posts the listed path, and ``POST /api/import`` strips it."""
    _dirs(tmp_path, "Album")

    assert _list(f"  {tmp_path}/Album  ")["path"] == str(tmp_path / "Album")


def test_a_file_lists_the_folder_it_is_in(tmp_path: Path) -> None:
    _dirs(tmp_path, "Album")
    (tmp_path / "Album" / "01.flac").write_bytes(b"x")

    assert _list(tmp_path / "Album" / "01.flac")["path"] == str(tmp_path / "Album")


def test_parent_is_the_folder_above_and_null_at_the_root(tmp_path: Path) -> None:
    assert _list(tmp_path)["parent"] == str(tmp_path.parent)
    root = _list("/")
    assert (root["path"], root["parent"]) == ("/", None)


def test_no_path_opens_the_media_folder_when_it_is_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = _dirs(tmp_path / "media", "downloads")
    monkeypatch.setattr(folders_api, "DEFAULT_FOLDER", str(media))

    assert _list()["path"] == str(media)


def test_no_path_opens_the_root_without_a_media_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(folders_api, "DEFAULT_FOLDER", str(tmp_path / "media"))

    assert _list()["path"] == "/"


# ----- failures --------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 folder")
def test_an_unreadable_folder_answers_422_with_the_shared_sentence(tmp_path: Path) -> None:
    locked = _dirs(tmp_path, "locked") / "locked"
    locked.chmod(0o000)
    try:
        response = TestClient(app).get("/api/folders", params={"path": str(locked)})
    finally:
        locked.chmod(0o755)

    denied = PermissionError(errno.EACCES, os.strerror(errno.EACCES))
    assert (response.status_code, response.json()) == (
        422,
        {"detail": unreadable_source_sentence(denied)},
    )


@pytest.mark.parametrize("path", ["/media/\x00x", "/" + "x" * 4096])
def test_a_nul_or_an_over_long_path_fails_validation(path: str) -> None:
    response = TestClient(app).get("/api/folders", params={"path": path})

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


# ----- badges and the refusal line -------------------------------------------------


@dataclass(frozen=True)
class _Layout:
    root: Path
    music: Path
    beets: Path
    inbox: Path
    settings: Settings
    lib: Library

    def attach(self) -> None:
        """The start and the browser read the same registry, as in production."""
        get_registry().attach_library(
            self.lib,
            self.beets / "trash",
            trash_origins_dir=self.beets / "trash-origins",
            settings=self.settings,
            beets_dir=self.beets,
        )

    def validate(self, path: Path) -> str | None:
        """The sentence the start's own check refuses ``path`` with, or ``None``."""
        runner = BeetsImportRunner(
            self.lib,
            self.beets / "trash",
            self.beets / "trash-origins",
            settings=self.settings,
            beets_dir=self.beets,
        )
        try:
            runner.validate([str(path)])
        except ImportSourceRefusedError as exc:
            return str(exc)
        return None


def _layout(tmp_path: Path, *, inbox: str = "") -> _Layout:
    """``<root>/media/music``, ``<root>/data/beets``, and slskd's folder.

    Every store is set explicitly, so no ``.env`` value can move one.
    """
    root = tmp_path / "root"
    music = root / "media" / "music"
    music.mkdir(parents=True)
    (music / ".keep").write_bytes(b"")
    beets_dir = root / "data" / "beets"
    beets_dir.mkdir(parents=True)
    slskd = Path(inbox) if inbox else root / "media" / "downloads" / "slskd"
    slskd.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        beets_dir=str(beets_dir),
        bank_dir="",
        plex_settings_dir="",
        slskd_settings_dir="",
        playlists_dir="",
        inbox_dir=str(slskd),
        playlists_export_dir="",
        artist_image_cache_dir=str(root / "data" / "cache" / "artist-images"),
        cover_thumb_cache_dir=str(root / "data" / "cache" / "cover-thumbs"),
    )
    lib = build_library(str(beets_dir / "library.db"), str(music))
    layout = _Layout(root, music, beets_dir, slskd, settings, lib)
    layout.attach()
    return layout


def _badges(body: dict[str, Any]) -> dict[str, str | None]:
    return {entry["name"]: entry["badge"] for entry in body["folders"]}


def test_the_library_and_app_folders_are_badged_and_slskds_folder_is_not(tmp_path: Path) -> None:
    """Invariant 6. ACCEPT: slskd's folder, and a folder beside it, carry none."""
    layout = _layout(tmp_path)
    _dirs(layout.root / "media" / "downloads", "yubal")

    assert _badges(_list(layout.root / "media")) == {"downloads": None, "music": "library"}
    assert _badges(_list(layout.root / "media" / "downloads")) == {"slskd": None, "yubal": None}
    assert _badges(_list(layout.root / "data")) == {"beets": "musicdrop"}


def test_a_library_parent_carries_no_badge_and_a_holder_of_app_data_does(tmp_path: Path) -> None:
    """``media`` holds the library: none, on purpose. ``data`` holds the beets dir."""
    layout = _layout(tmp_path)

    assert _badges(_list(layout.root)) == {"data": "musicdrop", "media": None}


def test_a_folder_inside_an_app_folder_is_badged_but_the_inbox_inside_one_is_not(
    tmp_path: Path,
) -> None:
    """Nearest container decides "inside": ``<beets>/trash/x`` is ours, while slskd's
    folder left at its default ``<beets>/inbox`` is slskd's, not MusicDrop's."""
    layout = _layout(tmp_path, inbox=str(tmp_path / "root" / "data" / "beets" / "inbox"))
    _dirs(layout.beets / "trash", "Album")
    _dirs(layout.beets / "inbox", "Download")

    assert _badges(_list(layout.beets)) == {"inbox": None, "trash": "musicdrop"}
    assert _badges(_list(layout.beets / "trash")) == {"Album": "musicdrop"}
    assert _badges(_list(layout.beets / "inbox")) == {"Download": None}


def test_a_folder_opened_through_a_link_is_badged_where_it_resolves(tmp_path: Path) -> None:
    """The listed folder is asked as spelled AND resolved (one resolve per listing),
    so the library still reads as the library through a link to its parent."""
    layout = _layout(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(layout.root / "media")

    body = _list(alias)

    assert (body["path"], _badges(body)) == (str(alias), {"downloads": None, "music": "library"})


def test_the_refusal_is_the_starts_own_422_text(tmp_path: Path) -> None:
    """Invariant 7, against the start itself: a library parent, the beets dir and
    slskd's folder each list with the sentence ``POST /api/import`` answers."""
    layout = _layout(tmp_path)
    client = TestClient(app)
    cases = {
        layout.root / "media": SOURCE_IS_THE_LIBRARY,
        layout.beets: SOURCE_HOLDS_APP_DATA,
        layout.inbox: SOURCE_IS_THE_INBOX,
    }
    for folder, sentence in cases.items():
        started = client.post("/api/import", json={"path": str(folder)})
        assert (started.status_code, started.json()["detail"]) == (422, sentence), folder
        assert _list(folder)["refusal"] == sentence, folder


def test_no_refusal_inside_slskds_folder_or_for_a_download_folder(tmp_path: Path) -> None:
    """ACCEPT: what the start lets through lists with no refusal line."""
    layout = _layout(tmp_path)
    album = _dirs(layout.inbox, "Album") / "Album"
    download = _dirs(layout.root / "media" / "downloads", "yubal") / "yubal"

    for folder in (album, download):
        assert layout.validate(folder) is None, folder
        assert _list(folder)["refusal"] is None, folder


@pytest.mark.usefixtures("inbox_bank_dir")
def test_a_folder_the_ledger_hides_is_still_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 10 (#77): the browser reads no ledger, so it is the way back."""
    inbox = _dirs(tmp_path / "inbox", "Imported", "Waiting")
    for album in ("Imported", "Waiting"):
        (inbox / album / "01 track.flac").write_bytes(b"\0")
    ledger = AcquisitionLedger(inbox / ".musicdrop-ledger.json")
    ledger.mark(inbox / "Imported", outcome="imported")
    monkeypatch.setattr(app.state, "inbox_dir", inbox, raising=False)
    monkeypatch.setattr(app.state, "acquisition_ledger", ledger, raising=False)
    client = TestClient(app)

    waiting = client.get("/api/acquisition/inbox/items").json()["items"]
    assert [item["name"] for item in waiting] == ["Waiting"]  # the ledger does hide it
    assert _names(_list(inbox)) == ["Imported", "Waiting"]


# ----- threads ---------------------------------------------------------------------


@pytest.mark.anyio
async def test_two_stuck_listings_start_no_third_worker_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 8: a hung mount parks the listings behind the browser's cap of 2,
    in ``inbox_read``'s shape, and takes no more of anyio's pool than that."""
    lock = threading.Lock()
    release = threading.Event()
    inside = 0

    def stuck(folder: bytes, rules: object) -> list[bytes]:
        nonlocal inside
        with lock:
            inside += 1
        release.wait(10)
        return []

    monkeypatch.setattr(folders_api, "_child_folders", stuck)
    # The suite resets beets' config between tests, and confuse's FIRST read is
    # not thread-safe: three threads materialising it at once saw "ignore not
    # found". The app reads it at boot, so prime it here as boot does.
    walk_rules()
    transport = httpx.ASGITransport(app=app)
    jar = {SESSION_COOKIE_NAME: session_cookie_value()}
    answered: list[int] = []

    async def list_it() -> None:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", cookies=jar
        ) as client:
            response = await client.get("/api/folders", params={"path": str(tmp_path)})
            answered.append(response.status_code)

    try:
        async with anyio.create_task_group() as group:
            for _ in range(3):
                group.start_soon(list_it)
            await anyio.sleep(0.3)  # every caller has queued by now
            with lock:
                stuck_now = inside
            borrowed = anyio.to_thread.current_default_thread_limiter().borrowed_tokens
            release.set()
    finally:
        release.set()

    assert (stuck_now, borrowed) == (2, 2)
    assert answered == [200, 200, 200]
