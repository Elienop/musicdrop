"""Settings → Sources (branch 2, S8; ``decisions`` #54, #77).

Folder sources are a name and a folder in ``<beets_dir>/sources.json``. Adding
asks what an import start asks, with its sentences; slskd is never listed. The
layout helper builds the image's shape under one tmp ``root``: ``/media/music``,
``/data/beets`` and slskd's folder at ``/media/downloads/slskd``.
"""

from __future__ import annotations

import errno
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from beets.library import Library
from fastapi.testclient import TestClient

from app.api import sources as sources_api
from app.api.import_ import AMBIGUOUS_FOLDERS_DETAIL
from app.auth.session import SESSION_COOKIE_NAME
from app.beets.store_layout import (
    SOURCE_HOLDS_APP_DATA,
    SOURCE_IS_THE_INBOX,
    SOURCE_IS_THE_LIBRARY,
)
from app.config import Settings
from app.import_jobs.registry import get_registry
from app.import_jobs.runner import unreadable_source_sentence
from app.main import app
from app.playlists.atomic import write_atomic_text
from app.sources import store as store_module
from app.sources.store import SourcesStore
from app.wire import PLACEHOLDER
from tests.conftest import build_library, session_cookie_value

_MISSING = "That folder doesn’t exist."


@dataclass(frozen=True)
class _Layout:
    root: Path
    music: Path
    beets: Path
    inbox: Path
    downloads: Path

    @property
    def file(self) -> Path:
        return self.beets / "sources.json"


def _layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, attach: bool = True) -> _Layout:
    """The image's folders; the registry attached as in production.

    Every store is set explicitly, so no ``.env`` value can move one.
    """
    root = tmp_path / "root"
    music = root / "media" / "music"
    music.mkdir(parents=True)
    (music / ".keep").write_bytes(b"")
    beets_dir = root / "data" / "beets"
    beets_dir.mkdir(parents=True)
    downloads = root / "media" / "downloads"
    inbox = downloads / "slskd"
    inbox.mkdir(parents=True)
    settings = Settings(
        beets_dir=str(beets_dir),
        bank_dir="",
        plex_settings_dir="",
        slskd_settings_dir="",
        playlists_dir="",
        inbox_dir=str(inbox),
        playlists_export_dir="",
        artist_image_cache_dir=str(root / "data" / "cache" / "artist-images"),
        cover_thumb_cache_dir=str(root / "data" / "cache" / "cover-thumbs"),
    )
    monkeypatch.setattr("app.config.settings.beets_dir", str(beets_dir))
    if attach:
        lib: Library = build_library(str(beets_dir / "library.db"), str(music))
        get_registry().attach_library(
            lib,
            beets_dir / "trash",
            trash_origins_dir=beets_dir / "trash-origins",
            settings=settings,
            beets_dir=beets_dir,
        )
    return _Layout(root, music, beets_dir, inbox, downloads)


def _folder(parent: Path, name: str) -> Path:
    folder = parent / name
    folder.mkdir(parents=True)
    return folder


def _add(name: str, folder: str | Path) -> Any:
    return TestClient(app).post("/api/sources/folders", json={"name": name, "folder": str(folder)})


def _sources() -> list[dict[str, Any]]:
    response = TestClient(app).get("/api/sources")
    assert response.status_code == 200, response.text
    rows: list[dict[str, Any]] = response.json()["sources"]
    return rows


# ----- add, list, remove -----------------------------------------------------------


def test_add_list_and_remove_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = _layout(tmp_path, monkeypatch)
    yubal = _folder(layout.downloads, "yubal")
    other = _folder(layout.downloads, "other")

    first = _add("yubal", yubal)
    second = _add("other", other)

    assert (first.status_code, second.status_code) == (201, 201)
    added = first.json()
    assert {k: added[k] for k in ("name", "folder", "exists")} == {
        "name": "yubal",
        "folder": str(yubal),
        "exists": True,
    }
    assert _sources() == [added, second.json()]

    removed = TestClient(app).delete(f"/api/sources/folders/{added['id']}")

    assert (removed.status_code, removed.content) == (204, b"")
    assert _sources() == [second.json()]
    # Removing touches no files.
    assert yubal.is_dir()


def test_name_and_folder_are_trimmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = _layout(tmp_path, monkeypatch)
    yubal = _folder(layout.downloads, "yubal")

    response = _add("  yubal \t", f"  {yubal} ")

    assert response.status_code == 201, response.text
    assert (response.json()["name"], response.json()["folder"]) == ("yubal", str(yubal))


@pytest.mark.parametrize(("name", "status"), [("x" * 40, 201), ("x" * 41, 422), ("   ", 422)])
def test_a_name_is_1_to_40_characters_after_trimming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, status: int
) -> None:
    layout = _layout(tmp_path, monkeypatch)
    yubal = _folder(layout.downloads, "yubal")

    response = _add(name, yubal)

    assert response.status_code == status, response.text
    assert len(_sources()) == (1 if status == 201 else 0)


@pytest.mark.parametrize("folder", ["", "   ", "/x/\x00y", "/" + "x" * 4096])
def test_a_blank_nul_or_over_long_folder_fails_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folder: str
) -> None:
    _layout(tmp_path, monkeypatch)

    response = _add("yubal", folder)

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


def test_removing_an_unknown_id_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = _layout(tmp_path, monkeypatch)
    _add("yubal", _folder(layout.downloads, "yubal"))

    response = TestClient(app).delete("/api/sources/folders/nope")

    assert (response.status_code, response.json()) == (404, {"detail": "Source not found."})
    assert len(_sources()) == 1


# ----- what adding refuses ---------------------------------------------------------


def test_adding_refuses_what_a_start_refuses_with_its_sentences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing folder, a library parent, the beets dir and slskd's whole folder:
    each answers the sentence ``POST /api/import`` answers, and nothing is stored."""
    layout = _layout(tmp_path, monkeypatch)
    client = TestClient(app)
    cases = {
        layout.downloads / "nowhere": _MISSING,
        layout.root / "media": SOURCE_IS_THE_LIBRARY,
        layout.beets: SOURCE_HOLDS_APP_DATA,
        layout.inbox: SOURCE_IS_THE_INBOX,
    }
    for folder, sentence in cases.items():
        started = client.post("/api/import", json={"path": str(folder)})
        assert (started.status_code, started.json()["detail"]) == (422, sentence), folder
        added = _add("x", folder)
        assert (added.status_code, added.json()) == (422, {"detail": sentence}), folder
    assert _sources() == []
    assert not layout.file.exists()


def test_a_file_is_not_a_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """beets would import a file as one track; a Folder source must be a folder."""
    layout = _layout(tmp_path, monkeypatch)
    track = layout.downloads / "track.flac"
    track.write_bytes(b"x")

    response = _add("track", track)

    assert (response.status_code, response.json()) == (422, {"detail": _MISSING})


def test_a_folder_inside_slskds_folder_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACCEPT: an album inside slskd's folder still works (#77 names it)."""
    layout = _layout(tmp_path, monkeypatch)
    album = _folder(layout.inbox, "Album")

    assert _add("album", album).status_code == 201


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 folder")
def test_an_unreadable_folder_answers_the_shared_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path, monkeypatch)
    locked = _folder(layout.downloads, "locked")
    locked.chmod(0o000)
    try:
        response = _add("locked", locked / "Album")
    finally:
        locked.chmod(0o755)

    denied = PermissionError(errno.EACCES, os.strerror(errno.EACCES))
    assert (response.status_code, response.json()) == (
        422,
        {"detail": unreadable_source_sentence(denied)},
    )


# ----- display form ----------------------------------------------------------------


def test_a_non_utf8_folder_is_stored_as_shown_and_resolved_on_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The display form the browser handed out is stored as sent, and still finds
    the folder whose name is not valid UTF-8."""
    layout = _layout(tmp_path, monkeypatch)
    os.mkdir(os.fsencode(layout.downloads) + b"/Caf\xe9")
    shown = f"{layout.downloads}/Caf{PLACEHOLDER}"

    response = _add("Café", shown)

    assert response.status_code == 201, response.text
    assert response.json()["folder"] == shown
    assert [(row["folder"], row["exists"]) for row in _sources()] == [(shown, True)]


def test_a_lone_surrogate_never_reaches_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pydantic refuses it as a string, so the store is never asked to write one.

    ``json.dumps`` escapes it; the server's ``json.loads`` turns it back into one.
    """
    layout = _layout(tmp_path, monkeypatch)
    os.mkdir(os.fsencode(layout.downloads) + b"/Caf\xe9")
    raw = os.fsdecode(os.fsencode(layout.downloads) + b"/Caf\xe9")

    response = TestClient(app).post(
        "/api/sources/folders",
        content=json.dumps({"name": "Caf\udce9", "folder": raw}),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert {error["loc"][-1] for error in response.json()["detail"]} == {"name", "folder"}
    assert not layout.file.exists()


def test_two_folders_under_one_display_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding one is the start's 409; one already stored still reads as there."""
    layout = _layout(tmp_path, monkeypatch)
    base = os.fsencode(layout.downloads)
    os.mkdir(base + b"/a\xe9")
    shown = f"{layout.downloads}/a{PLACEHOLDER}"
    SourcesStore(layout.file).add("a", shown)
    os.mkdir(base + b"/a\xe8")

    response = _add("a", shown)

    assert (response.status_code, response.json()) == (409, {"detail": AMBIGUOUS_FOLDERS_DETAIL})
    assert [row["exists"] for row in _sources()] == [True]


# ----- the list --------------------------------------------------------------------


def test_exists_follows_the_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = _layout(tmp_path, monkeypatch)
    yubal = _folder(layout.downloads, "yubal")
    _add("yubal", yubal)

    yubal.rename(layout.downloads / "moved")

    assert [row["exists"] for row in _sources()] == [False]


def test_slskd_is_never_listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#77's control: slskd's folder exists and auto-import is on, and the list
    holds only the Folder source that was added."""
    layout = _layout(tmp_path, monkeypatch)
    monkeypatch.setattr(app.state, "inbox_dir", layout.inbox, raising=False)
    client = TestClient(app)
    client.put("/api/slskd/settings", json={"auto_import": True})
    slskd = client.get("/api/slskd/settings").json()
    assert (slskd["auto_import"], slskd["folder"], slskd["folder_exists"]) == (
        True,
        str(layout.inbox),
        True,
    )
    yubal = _folder(layout.downloads, "yubal")
    _add("yubal", yubal)

    assert [(row["name"], row["folder"]) for row in _sources()] == [("yubal", str(yubal))]


def test_slskds_folder_reports_whether_it_is_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path, monkeypatch, attach=False)
    gone = layout.downloads / "gone"
    monkeypatch.setattr(app.state, "inbox_dir", gone, raising=False)

    body = TestClient(app).get("/api/slskd/settings").json()

    assert (body["folder"], body["folder_exists"]) == (str(gone), False)


def test_the_file_survives_a_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = _layout(tmp_path, monkeypatch)
    yubal = _folder(layout.downloads, "yubal")
    added = _add("yubal", yubal).json()

    assert [row.model_dump() for row in SourcesStore(layout.file).folders()] == [
        {"id": added["id"], "name": "yubal", "folder": str(yubal)}
    ]
    assert _sources() == [added]


@pytest.mark.parametrize("content", [b"{not json", b'{"folders": [{"id": 1}]}', b"\xff\xfe"])
def test_a_corrupt_file_reads_as_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    layout = _layout(tmp_path, monkeypatch, attach=False)
    layout.file.write_bytes(content)

    assert _sources() == []
    assert layout.file.read_bytes() == content


def test_an_unreadable_file_reads_as_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = _layout(tmp_path, monkeypatch, attach=False)
    layout.file.mkdir()

    assert _sources() == []


# ----- the store -------------------------------------------------------------------


def test_a_row_that_cannot_be_serialised_leaves_the_store_unchanged(tmp_path: Path) -> None:
    store = SourcesStore(tmp_path / "sources.json")
    kept = store.add("kept", "/media/kept")
    before = (tmp_path / "sources.json").read_bytes()

    with pytest.raises(ValueError, match="surrogates not allowed"):
        store.add("Caf\udce9", "/media/x")

    assert (tmp_path / "sources.json").read_bytes() == before
    assert store.folders() == [kept]
    # And the store still takes the next good row.
    assert [row.name for row in [*store.folders(), store.add("next", "/media/n")]] == [
        "kept",
        "next",
    ]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a read-only folder")
def test_a_write_that_fails_leaves_the_store_unchanged(tmp_path: Path) -> None:
    beets = tmp_path / "beets"
    beets.mkdir()
    store = SourcesStore(beets / "sources.json")
    kept = store.add("kept", "/media/kept")
    before = (beets / "sources.json").read_bytes()
    beets.chmod(0o555)
    try:
        with pytest.raises(PermissionError):
            store.add("new", "/media/new")
        with pytest.raises(PermissionError):
            store.remove(kept.id)
        assert store.folders() == [kept]
    finally:
        beets.chmod(0o755)
    assert (beets / "sources.json").read_bytes() == before


def _held_in_its_write(
    monkeypatch: pytest.MonkeyPatch, first: Callable[[], object], second: Callable[[], object]
) -> None:
    """Run ``first`` until it is inside its write, run ``second``, then let ``first`` finish."""
    inside = threading.Event()
    release = threading.Event()
    calls = 0

    def slow_first_write(target: Path, text: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            inside.set()
            assert release.wait(5)
        write_atomic_text(target, text)

    monkeypatch.setattr(store_module, "write_atomic_text", slow_first_write)
    held = threading.Thread(target=first)
    other = threading.Thread(target=second)
    held.start()
    assert inside.wait(5)
    other.start()
    time.sleep(0.2)  # ``second`` reaches the lock (or, without it, its own write)
    release.set()
    held.join(5)
    other.join(5)


def test_two_adds_at_once_keep_both(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither change may be lost to the other's read of the file."""
    path = tmp_path / "sources.json"

    _held_in_its_write(
        monkeypatch,
        lambda: SourcesStore(path).add("first", "/a"),
        lambda: SourcesStore(path).add("second", "/b"),
    )

    assert [row.name for row in SourcesStore(path).folders()] == ["first", "second"]


def test_an_add_during_a_remove_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "sources.json"
    gone = SourcesStore(path).add("gone", "/g")

    _held_in_its_write(
        monkeypatch,
        lambda: SourcesStore(path).remove(gone.id),
        lambda: SourcesStore(path).add("new", "/n"),
    )

    assert [row.name for row in SourcesStore(path).folders()] == ["new"]


def test_the_store_sits_in_the_beets_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.config.settings.beets_dir", str(tmp_path))

    assert sources_api.get_sources_store().folders() == []
    SourcesStore(tmp_path / "sources.json").add("x", "/x")
    assert [row.name for row in sources_api.get_sources_store().folders()] == ["x"]


@pytest.mark.anyio
async def test_a_hung_source_takes_no_more_than_the_browsers_two_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The list stats every folder; on a hung mount three callers park behind the
    folder browser's cap of 2 and take no more of anyio's pool than that."""
    monkeypatch.setattr("app.config.settings.beets_dir", str(tmp_path))
    SourcesStore(tmp_path / "sources.json").add("hung", "/media/hung")
    lock = threading.Lock()
    release = threading.Event()
    inside = 0

    def stuck(folder: str) -> bool:
        nonlocal inside
        with lock:
            inside += 1
        release.wait(10)
        return True

    monkeypatch.setattr(sources_api, "_is_folder", stuck)
    transport = httpx.ASGITransport(app=app)
    jar = {SESSION_COOKIE_NAME: session_cookie_value()}
    answered: list[int] = []

    async def list_them() -> None:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", cookies=jar
        ) as client:
            answered.append((await client.get("/api/sources")).status_code)

    try:
        async with anyio.create_task_group() as group:
            for _ in range(3):
                group.start_soon(list_them)
            await anyio.sleep(0.3)  # every caller has queued by now
            with lock:
                stuck_now = inside
            borrowed = anyio.to_thread.current_default_thread_limiter().borrowed_tokens
            release.set()
    finally:
        release.set()

    assert (stuck_now, borrowed) == (2, 2)
    assert answered == [200, 200, 200]
