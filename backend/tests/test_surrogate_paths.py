"""App-wide invariant: no JSON endpoint may fail because a path is not valid UTF-8.

POSIX filenames are bytes. A directory named ``b"Caf\\xe9 Album"`` (latin-1 "é")
is not valid UTF-8, so ``os.fsdecode`` hands the app ``"Caf\\udce9 Album"`` — a
lone surrogate Starlette's strict UTF-8 encode cannot render. Before the wire
scrubber, ONE such folder took down every reorganize endpoint and the whole
Trash page with a 500.

These seed that byte-level name for real and assert the endpoints keep working,
that the undecodable byte surfaces as the U+FFFD placeholder, and — for the two
names a client sends straight back — that the round trip still lands on the real
entry on disk instead of silently 404ing.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from beets.library import Item, Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.main import app
from app.models.trash import RestoreResult
from tests.conftest import beets_dir_for, build_library, make_test_handle, origins_for

# One undecodable byte, and how it must look once it reaches the wire.
BAD_BYTES = b"Caf\xe9 Album"
BAD_DISPLAY = "Caf\ufffd Album"
# A second name that scrubs to the SAME display form — the ambiguity case.
TWIN_BYTES = b"Caf\xea Album"


def _bad_name() -> str:
    """The undecodable name as Python sees it after decoding (lone surrogate)."""
    return os.fsdecode(BAD_BYTES)


def _mkdir_raw(parent: Path, name: bytes) -> bytes:
    """Create ``parent/name`` at the BYTE level and return its raw path."""
    raw = os.path.join(os.fsencode(str(parent)), name)
    os.makedirs(raw, exist_ok=True)
    return raw


@dataclass
class SurrogateLibrary:
    """A hermetic library whose one album lives in an undecodable directory."""

    lib: Library
    music: Path
    album_dir: bytes
    track_file: bytes


@pytest.fixture
def surrogate_lib(tmp_path: Path) -> SurrogateLibrary:
    """One album mis-filed under ``music/junk/<undecodable>/``.

    Mis-filed on purpose: the path template renders a valid-UTF-8 destination
    from the tags, which can never equal the stored invalid-UTF-8 path, so
    reorganize always emits a move row for it. The file is a ``b"\\x00"`` stub —
    nothing here reads audio.
    """
    music = tmp_path / "music"
    (music / "junk").mkdir(parents=True)
    lib = build_library(str(tmp_path / "library.db"), str(music))
    album_dir = _mkdir_raw(music / "junk", BAD_BYTES)
    track_file = os.path.join(album_dir, b"01 Song.mp3")
    with open(track_file, "wb") as fh:
        fh.write(b"\x00")
    item = Item(
        album="Cafe Album",
        albumartist="Fixture Artist",
        artist="Fixture Artist",
        title="Song",
        track=1,
        disc=1,
    )
    item.path = track_file  # raw bytes pass straight through beets' bytestring_path
    lib.add_album([item]).store()
    return SurrogateLibrary(lib=lib, music=music, album_dir=album_dir, track_file=track_file)


@pytest.fixture
def surrogate_client(surrogate_lib: SurrogateLibrary, tmp_path: Path) -> Iterator[TestClient]:
    handle = make_test_handle(surrogate_lib.lib, beets_dir_for(tmp_path))
    app.dependency_overrides[get_library] = lambda: handle
    prior = getattr(app.state, "beets_library", None)
    app.state.beets_library = handle
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        if prior is None:
            del app.state.beets_library
        else:
            app.state.beets_library = prior


# ----- read-only plan/preview endpoints -----


def test_reorganize_preview_survives_an_undecodable_directory(
    surrogate_client: TestClient,
) -> None:
    resp = surrogate_client.get("/api/reorganize/preview")
    assert resp.status_code == 200
    moves = resp.json()["moves"]
    assert [m for m in moves if BAD_DISPLAY in m["from_path"]], moves


def test_album_reorganize_preview_survives_an_undecodable_directory(
    surrogate_client: TestClient,
) -> None:
    album_id = surrogate_client.get("/api/albums").json()["items"][0]["id"]
    resp = surrogate_client.get(f"/api/albums/{album_id}/reorganize/preview")
    assert resp.status_code == 200
    assert any(BAD_DISPLAY in m["from_path"] for m in resp.json()["moves"])


def test_edit_preview_survives_an_undecodable_directory(surrogate_client: TestClient) -> None:
    album_id = surrogate_client.get("/api/albums").json()["items"][0]["id"]
    resp = surrogate_client.post(
        f"/api/albums/{album_id}/edit/preview", json={"album": {"title": "Renamed"}, "tracks": []}
    )
    assert resp.status_code == 200
    plan = resp.json()["move_plan"]
    assert [row for row in plan if BAD_DISPLAY in row["old_path"]], plan


def test_disk_sync_preview_survives_an_undecodable_directory(
    surrogate_client: TestClient, surrogate_lib: SurrogateLibrary
) -> None:
    # A path only reaches the disk-sync wire as a removal row, so drop the file.
    os.remove(surrogate_lib.track_file)
    resp = surrogate_client.get("/api/disk-sync/preview")
    assert resp.status_code == 200
    body = resp.json()
    assert any(BAD_DISPLAY in row["path"] for row in body["removals"]), body


# ----- the two listing adapters owe their caller a display-safe name -----
#
# The HTTP sink would scrub these anyway, so these pin the ADAPTER's own
# contract: ``resolve_display_path`` matches a client's name against
# ``wire_safe`` of the real directory entries, so the listing that produced that
# name has to hand out exactly that form. Without these, dropping the scrub at
# either emit site looks harmless.


def test_list_inbox_emits_a_display_safe_name(tmp_path: Path) -> None:
    from app.acquisition.inbox import list_inbox

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    raw = _mkdir_raw(inbox, BAD_BYTES)
    with open(os.path.join(raw, b"01 track.flac"), "wb") as fh:
        fh.write(b"\x00")
    assert [item.name for item in list_inbox(inbox, None)] == [BAD_DISPLAY]


def test_list_trashed_albums_emits_a_display_safe_husk_folder(tmp_path: Path) -> None:
    # The audio-free branch: nothing here parses as media, so the folder is
    # listed as a zero-track husk.
    from app.beets.trash_manage import list_trashed_albums

    trash = tmp_path / "trash"
    trash.mkdir()
    raw = _mkdir_raw(trash, BAD_BYTES)
    with open(os.path.join(raw, b"cover.jpg"), "wb") as fh:
        fh.write(b"\x00")
    rows = list_trashed_albums(
        trash, origins_dir=origins_for(trash), music_dir=str(tmp_path / "music")
    )
    assert [(album.folder, album.track_count) for album in rows] == [(BAD_DISPLAY, 0)]


def test_list_trashed_albums_emits_a_display_safe_folder_for_a_real_album(tmp_path: Path) -> None:
    # The other branch: a folder holding actual audio is grouped by its top
    # segment, and that segment is the restore/empty key.
    import shutil

    from app.beets.trash_manage import list_trashed_albums

    trash = tmp_path / "trash"
    trash.mkdir()
    raw = _mkdir_raw(trash, BAD_BYTES)
    shutil.copyfile(
        Path(__file__).parent / "fixtures" / "silent.flac", os.path.join(raw, b"01.flac")
    )
    rows = list_trashed_albums(
        trash, origins_dir=origins_for(trash), music_dir=str(tmp_path / "music")
    )
    assert [(album.folder, album.track_count) for album in rows] == [(BAD_DISPLAY, 1)]


# ----- Trash: listing renders, and the folder still round-trips -----


def _seed_trash(client: TestClient, *names: bytes) -> tuple[Path, list[bytes]]:
    """Read the Trash dir (while the listing is still clean) and seed raw names."""
    trash = Path(client.get("/api/trash").json()["trash_path"])
    trash.mkdir(parents=True, exist_ok=True)
    made = []
    for name in names:
        raw = _mkdir_raw(trash, name)
        with open(os.path.join(raw, b"cover.jpg"), "wb") as fh:
            fh.write(b"\x00")  # not media: the folder lists as a zero-track husk
        made.append(raw)
    return trash, made


def test_trash_listing_survives_an_undecodable_folder(client: TestClient) -> None:
    _seed_trash(client, BAD_BYTES)
    resp = client.get("/api/trash")
    assert resp.status_code == 200
    assert [a for a in resp.json()["albums"] if a["folder"] == BAD_DISPLAY], resp.json()


def test_trash_delete_by_the_listed_folder_removes_the_real_directory(client: TestClient) -> None:
    _trash, made = _seed_trash(client, BAD_BYTES)
    folder = client.get("/api/trash").json()["albums"][0]["folder"]
    assert folder == BAD_DISPLAY  # the value the UI hands straight back

    resp = client.delete("/api/trash", params={"folder": folder})
    assert resp.status_code == 200
    assert resp.json()["removed"] == 1
    assert not os.path.exists(made[0])  # the REAL bytes path is gone


def test_trash_restore_resolves_the_listed_folder_to_the_real_directory(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Assert the RESOLUTION, not a full beets re-import: what the round trip has
    # to preserve is the exact on-disk bytes the restore then operates on.
    import app.api.trash as trash_api

    _trash, made = _seed_trash(client, BAD_BYTES)
    seen: list[str] = []

    def fake_restore(
        lib: object, folder_abs: str, *, trash_dir: Path, origins_dir: Path
    ) -> RestoreResult:
        seen.append(folder_abs)
        return RestoreResult(restored=True, reason="restored", album_id=1)

    monkeypatch.setattr(trash_api, "restore_album", fake_restore)
    folder = client.get("/api/trash").json()["albums"][0]["folder"]
    resp = client.post("/api/trash/restore", json={"folder": folder})

    assert resp.status_code == 200, resp.text
    assert seen
    assert os.fsencode(seen[0]) == os.path.realpath(made[0])


def test_trash_delete_refuses_when_two_folders_share_one_display_form(client: TestClient) -> None:
    _trash, made = _seed_trash(client, BAD_BYTES, TWIN_BYTES)
    resp = client.delete("/api/trash", params={"folder": BAD_DISPLAY})
    assert resp.status_code == 409, resp.text
    assert all(os.path.exists(raw) for raw in made)  # neither was guessed at


# U+FFFD is a perfectly legal filename character, so a folder can be GENUINELY
# named "Caf<U+FFFD> Album" and sit next to the damaged b"Caf\xe9 Album" that
# displays the same way. Trusting the literal path first would silently operate
# on the wrong one of the two.


def test_trash_delete_refuses_when_a_real_placeholder_name_shadows_a_damaged_twin(
    client: TestClient,
) -> None:
    _trash, made = _seed_trash(client, BAD_BYTES, os.fsencode(BAD_DISPLAY))
    resp = client.delete("/api/trash", params={"folder": BAD_DISPLAY})
    assert resp.status_code == 409, resp.text
    assert all(os.path.exists(raw) for raw in made)  # neither folder destroyed


def test_trash_restore_refuses_when_a_real_placeholder_name_shadows_a_damaged_twin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.api.trash as trash_api

    _trash, made = _seed_trash(client, BAD_BYTES, os.fsencode(BAD_DISPLAY))
    seen: list[str] = []

    def fake_restore(
        lib: object, folder_abs: str, *, trash_dir: Path, origins_dir: Path
    ) -> RestoreResult:
        seen.append(folder_abs)
        return RestoreResult(restored=True, reason="restored", album_id=1)

    monkeypatch.setattr(trash_api, "restore_album", fake_restore)
    resp = client.post("/api/trash/restore", json={"folder": BAD_DISPLAY})
    assert resp.status_code == 409, resp.text
    assert seen == []  # nothing was re-imported on a guess
    assert all(os.path.exists(raw) for raw in made)


def test_trash_delete_still_works_for_a_lone_real_placeholder_name(client: TestClient) -> None:
    # No damaged twin: the scan finds exactly one match, so the operation runs.
    _trash, made = _seed_trash(client, os.fsencode(BAD_DISPLAY))
    resp = client.delete("/api/trash", params={"folder": BAD_DISPLAY})
    assert resp.status_code == 200, resp.text
    assert resp.json()["removed"] == 1
    assert not os.path.exists(made[0])


def test_trash_error_detail_survives_an_undecodable_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The failure path carries paths too: an OSError message embeds the file it
    # could not touch, and that string goes into the 500 body.
    import app.api.trash as trash_api

    _seed_trash(client, BAD_BYTES)

    def boom(lib: object, folder_abs: str, *, trash_dir: Path, origins_dir: Path) -> RestoreResult:
        raise OSError(f"cannot move {_bad_name()}")

    monkeypatch.setattr(trash_api, "restore_album", boom)
    resp = client.post("/api/trash/restore", json={"folder": BAD_DISPLAY})
    assert resp.status_code == 500
    assert BAD_DISPLAY in resp.json()["detail"]


# ----- Inbox: listing renders, and the item name still round-trips -----


def test_inbox_listing_and_per_item_import_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.import_jobs.fakes import FakeImportRunner
    from app.import_jobs.registry import reset_registry

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    raw = _mkdir_raw(inbox, BAD_BYTES)
    with open(os.path.join(raw, b"01 track.flac"), "wb") as fh:
        fh.write(b"\x00")

    fake = FakeImportRunner(parked=[])
    reset_registry(runner=fake)
    monkeypatch.setattr(app.state, "inbox_dir", inbox, raising=False)
    client = TestClient(app)
    listing = client.get("/api/acquisition/inbox/items")
    assert listing.status_code == 200
    names = [i["name"] for i in listing.json()["items"]]
    assert names == [BAD_DISPLAY], names

    resp = client.post("/api/acquisition/inbox/items/import", json={"name": names[0]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["started"] is True
    # The importer must have been pointed at the REAL bytes path, not a lossy one.
    assert fake.received_paths is not None
    assert os.fsencode(fake.received_paths[0]) == os.path.realpath(raw)


def test_inbox_import_refuses_when_two_folders_share_one_display_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.import_jobs.fakes import FakeImportRunner
    from app.import_jobs.registry import reset_registry

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in (BAD_BYTES, TWIN_BYTES):
        raw = _mkdir_raw(inbox, name)
        with open(os.path.join(raw, b"01 track.flac"), "wb") as fh:
            fh.write(b"\x00")

    fake = FakeImportRunner(parked=[])
    reset_registry(runner=fake)
    monkeypatch.setattr(app.state, "inbox_dir", inbox, raising=False)
    resp = TestClient(app).post("/api/acquisition/inbox/items/import", json={"name": BAD_DISPLAY})
    assert resp.status_code == 409, resp.text
    assert fake.received_paths is None  # nothing was imported on a guess


def test_inbox_import_rejects_a_nul_inside_a_placeholder_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The endpoint promises 404 for a malformed name. os.scandir raises
    # ValueError (NOT OSError) on an embedded NUL, so a NUL sitting behind a
    # placeholder component reached the scan as an unhandled 500.
    from app.import_jobs.fakes import FakeImportRunner
    from app.import_jobs.registry import reset_registry

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    fake = FakeImportRunner(parked=[])
    reset_registry(runner=fake)
    monkeypatch.setattr(app.state, "inbox_dir", inbox, raising=False)
    resp = TestClient(app).post(
        "/api/acquisition/inbox/items/import",
        json={"name": f"{BAD_DISPLAY}\x00/{BAD_DISPLAY}"},
    )
    assert resp.status_code == 404, resp.text
    assert fake.received_paths is None


# ----- request validation echoes the client's own input back -----


# Sent as RAW bytes, not via ``json=``: httpx serialises with ensure_ascii=False
# and would choke on the surrogate client-side, never reaching the app. The
# escape below is plain ASCII on the wire — exactly what a hostile client sends.
_SURROGATE_BODY = b'{"folder": {"x": "\\udce9"}}'
_PLAIN_BODY = b'{"folder": {"x": "plain"}}'
_JSON = {"Content-Type": "application/json"}


def test_validation_error_echo_survives_a_client_injected_surrogate(client: TestClient) -> None:
    # json.loads accepts that escape and yields a LONE SURROGATE, which pydantic
    # puts in the row's `input` — rendered by a plain JSONResponse outside the
    # app's response class. `app/wire.py` now drops that key from every row
    # (a password field was being read back to the sender), so the surrogate no
    # longer reaches the renderer at all and this asserts both halves: the
    # request is still refused, and its value is not in the answer.
    resp = client.post("/api/trash/restore", content=_SURROGATE_BODY, headers=_JSON)
    assert resp.status_code == 422, resp.text
    assert "input" not in resp.text


def test_validation_error_control_is_422_for_an_ordinary_bad_value(client: TestClient) -> None:
    # Same shape, encodable value: proves the 422 above comes from validation
    # and not from something incidental to the surrogate.
    resp = client.post("/api/trash/restore", content=_PLAIN_BODY, headers=_JSON)
    assert resp.status_code == 422, resp.text
