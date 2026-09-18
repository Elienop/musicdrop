"""API tests for the Trash management endpoints (list / restore / empty)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.beets.config_editor import _settings
from app.beets.protected import ProtectedTrees
from app.beets.trash import resolve_trash_origins_dir
from app.beets.trash_manage import TrashEntryUnreadableError
from app.beets.trash_origins import write_trash_origin
from app.models.trash import EmptyResult


def _trash_dir(client: TestClient) -> Path:
    """The active Trash dir (read off the list endpoint's response)."""
    return Path(client.get("/api/trash").json()["trash_path"])


def _origins_dir(client: TestClient) -> Path:
    """The active origin store, resolved exactly as the routes resolve it.

    Deliberately NOT read off a response field: the origins path is not on the
    wire (adding it would be a contract change for something no UI shows), so a
    test that needs it has to resolve it the way ``app/api/trash.py`` does.
    """
    app: Any = client.app  # TestClient.app is typed as a bare ASGI callable
    return resolve_trash_origins_dir(_settings(app), app.state.beets_library)


def test_get_trash_empty(client: TestClient) -> None:
    r = client.get("/api/trash")
    assert r.status_code == 200
    body = r.json()
    assert body["albums"] == []
    assert isinstance(body["trash_path"], str)
    assert body["trash_path"]


def test_list_trash_reports_the_move_back_a_real_record_offers(
    client: TestClient, tmp_path: Path
) -> None:
    """The one argument the ROUTE contributes: which music dir the record is judged against.

    ``restore_mode`` / ``restore_note`` / ``origin`` were exercised only below
    the API, and ``list_trash`` supplies the value that decides all three —
    whether a recorded origin is still inside the library. Point it anywhere
    else and every row degrades to ``"import"`` carrying the "not inside the
    current music library" note, with every unit test still green and the UI
    quietly telling the user their exact restore is gone.

    Since the record moved to a sibling store the route supplies TWO such values
    — the music dir AND the origins dir — and forgetting either degrades every
    row the same silent way, so this covers both at once.

    The record is written by the real writer, into the store the route itself
    resolves, so the row is produced end to end rather than from a hand-built
    model.
    """
    trash = _trash_dir(client)
    entry = trash / "Weird Folder"
    entry.mkdir(parents=True)
    (entry / "cover.jpg").write_bytes(b"\x00")
    origin = tmp_path / "music" / "Weird Folder"
    write_trash_origin(_origins_dir(client), entry.name, origin=str(origin), moved="folder")

    r = client.get("/api/trash")

    assert r.status_code == 200
    (row,) = r.json()["albums"]
    assert row["restore_mode"] == "move_back"
    assert row["restore_note"] is None  # nothing to warn about on an exact restore
    assert row["origin"] == str(origin)


def test_a_trash_swapped_after_the_check_removes_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry's identity was pinned and its PARENT's was not.

    ``resolve_trash_child`` resolves the child from the Trash the request
    checked, and the removal then opened that path again. With the Trash
    renamed away and another directory put at its name in between, the removal
    ran by name inside whatever took its place — measured, the impostor's
    ``01.flac`` was gone. The parent is now opened through the same
    ``open_checked_dir`` the sweep uses, so the identity has to still be the one
    that was checked.
    """
    import app.api.trash as trash_mod
    from app.beets.trash_manage import empty_one as real_empty_one

    trash = _trash_dir(client)
    (trash / "Album").mkdir(parents=True)
    away = trash.parent / "away"
    impostor = trash.parent / "impostor"
    (impostor / "Album").mkdir(parents=True)
    (impostor / "Album" / "01.flac").write_bytes(b"x")

    def spy(
        path: str,
        *,
        origins_dir: Path,
        protected: ProtectedTrees,
        lib: object = None,
    ) -> EmptyResult:
        os.rename(trash, away)
        os.rename(impostor, trash)
        return real_empty_one(
            path,
            origins_dir=origins_dir,
            protected=protected,
            lib=lib,  # type: ignore[arg-type]  # the route's own handle, typed loosely here
        )

    monkeypatch.setattr(trash_mod, "empty_one", spy)
    r = client.delete("/api/trash", params={"folder": "Album"})

    assert r.status_code == 503
    assert "Nothing was removed" in r.json()["detail"]
    assert (trash / "Album" / "01.flac").exists(), "the directory that took the Trash's name"


def test_a_mode_000_entry_answers_the_declared_500(client: TestClient) -> None:
    """The single delete's twin of the sweep's failed-entry arm.

    An entry the process cannot open is one it cannot remove, and the removal
    raises. That used to reach the blanket 500 — the same status with no
    declared body, so the generated client had no type for what it reads.
    """
    if os.getuid() == 0:
        pytest.skip("root opens a mode-000 directory anyway")
    trash = _trash_dir(client)
    locked = trash / "Locked"
    locked.mkdir(parents=True)
    locked.chmod(0o000)

    try:
        r = client.delete("/api/trash", params={"folder": "Locked"})
    finally:
        locked.chmod(0o700)

    assert r.status_code == 500
    assert "Empty Trash" in r.json()["detail"]
    assert locked.is_dir(), "still in Trash"


def test_a_trash_root_that_stops_opening_answers_the_declared_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep's twin of the arm above, which it did not have.

    The root open is the one syscall between the route's check and the first
    removal, so a share that drops in that window (EIO/ESTALE) raises out of the
    sweep — into the blanket 500, the same status with no declared body, so the
    generated client had no type for what it reads. A merely unreadable Trash
    does not get here: the route's own store check refuses it 503 first, which is
    why the fault is injected at the open rather than made with a mode bit.
    """
    import errno as errno_mod

    trash = _trash_dir(client)
    trash.mkdir(parents=True, exist_ok=True)
    (trash / "Album").mkdir()

    def _gone(*args: Any, **kwargs: Any) -> int:
        raise OSError(errno_mod.ESTALE, "Stale file handle")

    monkeypatch.setattr("app.beets.trash_manage.open_checked_dir", _gone)

    r = client.delete("/api/trash/all")

    assert r.status_code == 500
    assert "Empty Trash" in r.json()["detail"]
    assert (trash / "Album").is_dir(), "still in Trash"


def test_a_sweep_that_removed_an_entry_then_raised_still_tells_the_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OSError arm's event, in the one state where it has anything to say.

    The arm above injects at the ROOT OPEN, which is before the first removal —
    nothing changed, so the ``emit_library_changed`` beside it survived removal.
    The other way into that arm is an origin record that could not be dropped
    AFTER its entry went: the record delete is the one call in the loop outside
    the per-entry ``try``. Here ``A`` is really gone and ``B`` was never reached,
    so a page that does not hear about it keeps listing an entry that no longer
    exists.
    """
    import errno as errno_mod

    from app.api import trash as trash_api
    from app.beets.trash_origins import delete_trash_origin as real_drop

    trash = _trash_dir(client)
    (trash / "A").mkdir(parents=True)
    (trash / "B").mkdir(parents=True)

    def _drop(origins_dir: Path, name: str) -> None:
        if name == "A":
            raise OSError(errno_mod.EROFS, "Read-only file system")
        real_drop(origins_dir, name)

    monkeypatch.setattr("app.beets.trash_manage.delete_trash_origin", _drop)
    emitted: list[str] = []
    monkeypatch.setattr(trash_api, "emit_library_changed", lambda _app: emitted.append("lib"))

    r = client.delete("/api/trash/all")

    assert r.status_code == 500
    assert "Empty Trash" in r.json()["detail"]
    assert emitted == ["lib"], "the page is told about the entry that did go"
    assert not (trash / "A").exists(), "removed before the record drop raised"
    assert (trash / "B").is_dir(), "the sweep stopped there"


def test_empty_one_removes_folder(client: TestClient) -> None:
    trash = _trash_dir(client)
    (trash / "Album").mkdir(parents=True)
    r = client.delete("/api/trash", params={"folder": "Album"})
    assert r.status_code == 200
    assert r.json()["removed"] == 1
    assert not (trash / "Album").exists()


def test_empty_all_removes_seeded_folders(client: TestClient) -> None:
    trash = _trash_dir(client)
    (trash / "A").mkdir(parents=True)
    (trash / "B").mkdir(parents=True)
    r = client.delete("/api/trash/all")
    assert r.status_code == 200
    assert r.json()["removed"] == 2
    assert list(trash.iterdir()) == []


def _album_row_inside(
    client: TestClient, entry: Path, *, spelled: str | None = None, album: bool = True
) -> Path:
    """One item row naming a file inside ``entry`` — the row-drop failure's state.

    What a delete leaves when ``Album.move`` finished and ``Album.remove`` then
    raised: the files are in Trash and the library still lists the album, so the
    entry is its ONLY copy.

    ``spelled`` is what goes in the ROW when that differs from where the file
    really is — beets stores what it was given, ``..`` and ``//`` included. An
    entry that is itself a loose file is addressed by passing that file as
    ``entry``. ``album=False`` makes it a singleton, which has no album to delete
    again and so is not protected.
    """
    from beets.library import Item

    app: Any = client.app
    lib = app.state.beets_library.lib
    track = entry if entry.suffix else entry / "01 T1.mp3"
    track.parent.mkdir(parents=True, exist_ok=True)
    track.write_bytes(b"\x00")
    item = Item(album="Alb", albumartist="Art", artist="Art", title="T1", track=1)
    item.path = os.fsencode(spelled if spelled is not None else str(track))
    if album:
        lib.add_album([item]).store()
    else:
        lib.add(item)
    return track


def test_empty_one_refuses_an_entry_the_library_still_lists(client: TestClient) -> None:
    """One Empty click used to destroy an album's only copy. MEASURED.

    After a delete whose row drop raised, the album stays listed with its rows
    naming Trash and the entry looks ordinary on the page. The refusal says what
    to do: delete the album again (the retry arm finishes it), then empty.
    """
    trash = _trash_dir(client)
    entry = trash / "Art - Alb"
    _album_row_inside(client, entry)

    r = client.delete("/api/trash", params={"folder": "Art - Alb"})

    assert r.status_code == 503
    assert r.json()["detail"] == (
        "Refused: 'Art - Alb' — the library still lists files inside."
        " Delete the album again, then empty Trash. Nothing was removed."
    )
    assert (entry / "01 T1.mp3").is_file(), "the only copy is still there"


def test_empty_all_keeps_the_entry_the_library_lists_and_empties_the_rest(
    client: TestClient,
) -> None:
    """The sweep is not all-or-nothing: the rest goes, the kept one is named."""
    trash = _trash_dir(client)
    entry = trash / "Art - Alb"
    _album_row_inside(client, entry)
    (trash / "Ordinary").mkdir(parents=True)

    r = client.delete("/api/trash/all")

    assert r.status_code == 503
    assert r.json()["detail"] == (
        "Refused: 'Art - Alb' — the library still lists files inside."
        " Delete the album again, then empty Trash. Removed 1."
    )
    assert not (trash / "Ordinary").exists(), "the rest was emptied"
    assert (entry / "01 T1.mp3").is_file()


# --- the spellings a row can hold, all through the route ----------------------
#
# The route is where the spelling changes: it hands ``empty_one`` the RESOLVED
# entry while the rows hold what Delete wrote. Each of these was measured
# answering 200 and destroying the album's only copy under the SQL-prefix check.


def test_empty_one_refuses_when_the_default_trash_leaf_is_a_link(
    client: TestClient, tmp_path: Path
) -> None:
    """``<beets_dir>/trash -> /bigdisk/trash``, which MusicDrop itself produces.

    Delete writes rows under the LINK spelling (the default Trash is not
    resolved, ``trash.resolve_trash_dir``), and the per-entry route removes the
    resolved path. Measured before the fix: ``200 {'removed': 1}`` with the album
    still listed. The sweep cannot even run on that layout (its root open is
    ``O_NOFOLLOW``), so this button is the only way to empty there.
    """
    elsewhere = tmp_path / "bigdisk-trash"
    elsewhere.mkdir()
    linked_leaf = _trash_dir(client)
    assert not linked_leaf.exists(), "the default leaf has not been created yet"
    linked_leaf.symlink_to(elsewhere)
    entry = linked_leaf / "Art - Alb"
    track = _album_row_inside(client, entry)

    r = client.delete("/api/trash", params={"folder": "Art - Alb"})

    assert r.status_code == 503
    assert "the library still lists files inside" in r.json()["detail"]
    assert track.is_file(), "the only copy is still there"


@pytest.mark.parametrize(
    ("label", "spell", "refuses", "twin_sees"),
    [
        ("dotdot", lambda t: f"{t}/Art - Alb/../Art - Alb/01 T1.mp3", True, True),
        ("double-slash", lambda t: f"{t}//Art - Alb//01 T1.mp3", False, True),
    ],
)
def test_what_a_hand_built_row_spelling_answers(
    client: TestClient, label: str, spell: Any, refuses: bool, twin_sees: bool
) -> None:
    """A RESIDUAL, pinned: beets' ``path:`` query compares path STRINGS.

    Neither spelling is producible through MusicDrop's own writer — ``Album.move``
    normpaths what it stores — so a row like this needs an alien or hand-edited
    database. Measured here so the boundary is visible rather than assumed:
    ``..`` still carries the entry's own prefix and refuses, ``//`` does not and
    Empty removes the entry. beets has no path identity beyond the string, and
    guarding it here would put back the hand-rolled compare three rounds of
    review found faults in.

    ``twin_sees`` is the delete-side retry arm's answer for the SAME row, and it
    is ``True`` for both: the twin asks the ROOT query, where ``trash//Art…``
    still carries the ``trash/`` prefix, while the gate asks the per-ENTRY query,
    which ``Art - Alb//`` does not. So the two sides part company on ``//`` only,
    and on the safe side — a retry delete treats the album as already in Trash
    and moves nothing.
    """
    from app.beets import delete as delete_mod

    trash = _trash_dir(client)
    entry = trash / "Art - Alb"
    track = _album_row_inside(client, entry, spelled=spell(trash))
    # BEFORE the route: a removed entry takes the file with it, and the twin's
    # ``S_ISREG`` test would then answer ``False`` for a reason that is not this.
    app: Any = client.app
    handle, _checked_trash, _origins, protected = delete_mod._checked_store(app)
    with handle.lib.music_dir_context():
        album = next(iter(handle.lib.albums()))
        twin = delete_mod._all_rows_are_in_trash(
            handle.lib, list(album.items()), protected.trash_spellings
        )
    assert twin is twin_sees, label

    r = client.delete("/api/trash", params={"folder": "Art - Alb"})

    if refuses:
        assert r.status_code == 503, label
        assert "the library still lists files inside" in r.json()["detail"]
        assert track.is_file(), "the only copy is still there"
    else:
        assert r.status_code == 200, label
        assert not entry.exists(), "the residual: this spelling is not recognised"


def test_empty_one_refuses_a_relative_row_when_trash_is_inside_the_music_folder(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A supported layout, and the one where beets stores the row RELATIVE.

    Inside ``directory:`` beets keeps ``.trash/Art - Alb/01 T1.mp3``, so the
    check has to make the row absolute before it compares — which is what the
    delete side has always done.
    """
    trash = tmp_path / "music" / ".trash"
    trash.mkdir(parents=True)
    monkeypatch.setattr("app.config.settings.trash_dir", str(trash))
    entry = trash / "Art - Alb"
    track = _album_row_inside(
        client, entry, spelled=os.path.join(".trash", "Art - Alb", "01 T1.mp3")
    )

    r = client.delete("/api/trash", params={"folder": "Art - Alb"})

    assert r.status_code == 503
    assert "the library still lists files inside" in r.json()["detail"]
    assert track.is_file(), "the only copy is still there"


def test_empty_one_refuses_a_relative_row_with_the_music_dir_context_unbound(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check binds beets' music dir itself rather than inheriting it.

    beets keeps it in a ``ContextVar`` set when the ``Library`` is constructed,
    and both halves of relative-row handling read it — the query's pattern and
    ``Item.path`` coming back out of the DB. A ``ContextVar`` set in one task
    does not reach another, and a config Apply builds the new ``Library`` in
    whichever task ran it, so the value the request sees is not guaranteed.
    Unbound here, which is that state exactly: the entry must still be refused.
    """
    from beets import context

    trash = tmp_path / "music" / ".trash"
    trash.mkdir(parents=True)
    monkeypatch.setattr("app.config.settings.trash_dir", str(trash))
    track = _album_row_inside(
        client, trash / "Art - Alb", spelled=os.path.join(".trash", "Art - Alb", "01 T1.mp3")
    )

    with context.music_dir(b""):
        r = client.delete("/api/trash", params={"folder": "Art - Alb"})

    assert r.status_code == 503
    assert "the library still lists files inside" in r.json()["detail"]
    assert track.is_file(), "the only copy is still there"


def test_empty_one_refuses_when_the_trash_is_configured_through_a_link(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other link layout, which is safe for a different reason.

    A CONFIGURED path is resolved at the source (``trash.resolve_trash_dir``), so
    the rows and the route already agree. Here to keep both link shapes pinned
    together: only the default leaf (above) was ever blind.
    """
    real = tmp_path / "real-trash"
    real.mkdir()
    alias = tmp_path / "trash-alias"
    alias.symlink_to(real)
    monkeypatch.setattr("app.config.settings.trash_dir", str(alias))
    track = _album_row_inside(client, real / "Art - Alb")

    r = client.delete("/api/trash", params={"folder": "Art - Alb"})

    assert r.status_code == 503
    assert "the library still lists files inside" in r.json()["detail"]
    assert track.is_file()


# --- the Trash's spelling changes between the failed delete and the Empty ------
#
# The rows hold what the mover used; the gate asks what the settings resolve to
# now. Both review seats measured the forward direction independently through
# the real routes, with a real Delete whose row drop raised: 200, only copy gone,
# album still listed. The reverse — a CONFIGURED Trash later cleared — was
# measured here the same way and answered 200 too. Nothing in this section is a
# spelling MusicDrop does not itself name.


def test_empty_one_refuses_after_the_trash_is_moved_to_a_bigger_disk(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mv trash bigdisk-trash && ln -s bigdisk-trash trash`` — the canonical full disk.

    The configured string still names the Trash; what it RESOLVES to changed, and
    that is the only spelling the gate used to ask with.
    """
    configured = tmp_path / "trash"
    configured.mkdir()
    monkeypatch.setattr("app.config.settings.trash_dir", str(configured))
    track = _album_row_inside(client, configured / "Art - Alb")
    bigger = tmp_path / "bigdisk-trash"
    configured.rename(bigger)
    configured.symlink_to(bigger)

    r = client.delete("/api/trash", params={"folder": "Art - Alb"})

    assert r.status_code == 503
    assert "the library still lists files inside" in r.json()["detail"]
    assert track.is_file(), "the only copy is still there"


def test_empty_all_refuses_after_the_trash_is_moved_to_a_bigger_disk(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep took a bystander with it, so it gets its own pin."""
    configured = tmp_path / "trash"
    configured.mkdir()
    monkeypatch.setattr("app.config.settings.trash_dir", str(configured))
    track = _album_row_inside(client, configured / "Art - Alb")
    (configured / "ZZZ-ordinary").mkdir()
    bigger = tmp_path / "bigdisk-trash"
    configured.rename(bigger)
    configured.symlink_to(bigger)

    r = client.delete("/api/trash/all")

    assert r.status_code == 503
    assert "the library still lists files inside" in r.json()["detail"]
    assert track.is_file(), "the only copy is still there"
    assert not (bigger / "ZZZ-ordinary").exists(), "the rest was still emptied"


def test_empty_one_refuses_after_the_operator_configures_the_default_trash(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``mv`` at all: the default is returned UNRESOLVED and a configured one resolved.

    A default Trash whose leaf is a link holds the rows under ``<beets_dir>/trash``.
    The operator then writes that Trash's own location into Settings, and every
    spelling the gate knew changed at once.
    """
    elsewhere = tmp_path / "bigdisk-trash"
    elsewhere.mkdir()
    default_leaf = _trash_dir(client)
    default_leaf.symlink_to(elsewhere)
    track = _album_row_inside(client, default_leaf / "Art - Alb")
    monkeypatch.setattr("app.config.settings.trash_dir", str(elsewhere))

    r = client.delete("/api/trash", params={"folder": "Art - Alb"})

    assert r.status_code == 503
    assert "the library still lists files inside" in r.json()["detail"]
    assert track.is_file(), "the only copy is still there"


def test_empty_one_refuses_after_the_operator_clears_a_configured_trash(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REVERSE of the door above, and the reason the resolution is asked too.

    ``resolve_trash_dir`` resolves a CONFIGURED path and returns the default
    UNRESOLVED, so the rows written while a Trash was configured at the real
    path hold that real path — and after the setting is cleared, every spelling
    the app names is the default leaf, which is only a link to it. Without the
    resolution of the checked dir the gate asks ``{D, D}`` and misses rows under
    ``R``; Empty answered ``200`` and destroyed the album's only copy.

    The remedy is pinned here too, because a refusal is only worth having if its
    sentence works: deleting the album again drops the rows and the next Empty
    succeeds.
    """
    default_leaf = _trash_dir(client)
    assert not default_leaf.exists(), "the default leaf has not been created yet"
    real = tmp_path / "bigdisk-trash"
    real.mkdir()
    monkeypatch.setattr("app.config.settings.trash_dir", str(real))
    track = _album_row_inside(client, real / "Art - Alb")
    # The operator clears the setting; the default leaf now points at the same
    # place, so the entry is still listed and still the album's only copy.
    monkeypatch.setattr("app.config.settings.trash_dir", "")
    default_leaf.symlink_to(real)
    app: Any = client.app
    album_id = next(iter(app.state.beets_library.lib.albums())).id

    r = client.delete("/api/trash", params={"folder": "Art - Alb"})

    assert r.status_code == 503
    assert "the library still lists files inside" in r.json()["detail"]
    assert track.is_file(), "the only copy is still there"

    again = client.delete(f"/api/albums/{album_id}")
    assert again.status_code == 200, again.text
    assert list(app.state.beets_library.lib.albums()) == [], "the rows are gone"
    assert track.is_file(), "and the files were left where they already are"

    emptied = client.delete("/api/trash", params={"folder": "Art - Alb"})
    assert emptied.status_code == 200
    assert emptied.json()["removed"] == 1
    assert not track.exists()


def test_the_remedy_still_clears_a_refusal_raised_under_an_older_spelling(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal is only worth having if the sentence it prints works.

    Delete's retry arm asks the same widened question from the same helper, so
    "Delete the album again" drops the rows written under the previous spelling
    and the next Empty succeeds.
    """
    configured = tmp_path / "trash"
    configured.mkdir()
    monkeypatch.setattr("app.config.settings.trash_dir", str(configured))
    track = _album_row_inside(client, configured / "Art - Alb")
    bigger = tmp_path / "bigdisk-trash"
    configured.rename(bigger)
    configured.symlink_to(bigger)
    app: Any = client.app
    album_id = next(iter(app.state.beets_library.lib.albums())).id

    assert client.delete("/api/trash", params={"folder": "Art - Alb"}).status_code == 503

    again = client.delete(f"/api/albums/{album_id}")
    assert again.status_code == 200, again.text
    assert again.json()["trashed_albums"] == 1
    assert list(app.state.beets_library.lib.albums()) == [], "the rows are gone"
    assert track.is_file(), "and the files were left where they already are"

    emptied = client.delete("/api/trash", params={"folder": "Art - Alb"})
    assert emptied.status_code == 200
    assert emptied.json()["removed"] == 1
    assert not track.exists()


def test_empty_one_removes_an_entry_a_row_outside_every_spelling_names(
    client: TestClient, tmp_path: Path
) -> None:
    """The CONTROL: widening the set may not make Empty refuse for ever.

    A row under a directory none of the app's settings name says nothing about
    this Trash, and the entry goes.
    """
    trash = _trash_dir(client)
    entry = trash / "Art - Alb"
    entry.mkdir(parents=True)
    _album_row_inside(client, tmp_path / "somewhere-else" / "Art - Alb")

    r = client.delete("/api/trash", params={"folder": "Art - Alb"})

    assert r.status_code == 200
    assert r.json()["removed"] == 1
    assert not entry.exists()


def test_a_same_named_entry_under_the_unused_default_trash_refuses_and_can_be_cleared(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cost of asking about all three, MEASURED: a false refusal, with a remedy.

    The operator configured a Trash elsewhere, and ``<beets_dir>/trash`` still
    holds an entry of the same name that a row names. That row is about a
    different directory, and this refuses anyway. Acceptable because the sentence
    still works — deleting the album again drops the row and the next Empty
    succeeds — and the opposite mistake destroys the only copy.
    """
    default_trash = _trash_dir(client)
    (default_trash / "Art - Alb").mkdir(parents=True)
    _album_row_inside(client, default_trash / "Art - Alb")
    configured = tmp_path / "other-trash"
    (configured / "Art - Alb").mkdir(parents=True)
    monkeypatch.setattr("app.config.settings.trash_dir", str(configured))
    app: Any = client.app
    album_id = next(iter(app.state.beets_library.lib.albums())).id

    r = client.delete("/api/trash", params={"folder": "Art - Alb"})
    assert r.status_code == 503, "a false refusal — the row is about the other directory"

    assert client.delete(f"/api/albums/{album_id}").status_code == 200
    cleared = client.delete("/api/trash", params={"folder": "Art - Alb"})
    assert cleared.status_code == 200, "and the remedy the refusal names clears it"
    assert not (configured / "Art - Alb").exists()


def test_empty_one_refuses_a_loose_file_the_library_lists(client: TestClient) -> None:
    """An entry that IS the file: the check appended a separator and never matched it."""
    trash = _trash_dir(client)
    trash.mkdir(parents=True, exist_ok=True)
    loose = trash / "01 Only Copy.mp3"
    _album_row_inside(client, loose)

    r = client.delete("/api/trash", params={"folder": "01 Only Copy.mp3"})

    assert r.status_code == 503
    assert "the library still lists files inside" in r.json()["detail"]
    assert loose.is_file()


def test_empty_one_removes_an_entry_a_trash_old_sibling_row_names(client: TestClient) -> None:
    """The CONTROL for the prefix: ``<trash>-old`` is not inside ``<trash>``.

    Without the trailing separator every sibling whose name starts with the
    Trash's would read as "inside it" and Empty would refuse for ever.
    """
    trash = _trash_dir(client)
    entry = trash / "Art - Alb"
    entry.mkdir(parents=True)
    sibling = trash.parent / f"{trash.name}-old"
    _album_row_inside(client, sibling / "Art - Alb")

    r = client.delete("/api/trash", params={"folder": "Art - Alb"})

    assert r.status_code == 200
    assert r.json()["removed"] == 1
    assert not entry.exists()


def test_empty_one_refuses_an_entry_only_a_singleton_row_names(client: TestClient) -> None:
    """EVERY item row counts, album or not — a wrong removal costs the only copy.

    Round 3 stopped refusing these because "delete the album again" names nothing
    a singleton's owner can press. That was the wrong trade: the security seat
    measured it as a WIDENING (503 with the copy alive before, ``200 removed=1``
    with the track still listed after). A refusal costs a click; the removal
    costs the file. So the row still refuses, with a control that is true for
    IT.
    """
    trash = _trash_dir(client)
    entry = trash / "Loose"
    track = _album_row_inside(client, entry, album=False)

    r = client.delete("/api/trash", params={"folder": "Loose"})

    assert r.status_code == 503
    assert r.json()["detail"] == (
        "Refused: 'Loose' — the library still lists a track inside."
        " Move that entry out of Trash, or remove the track with beets."
        " Nothing was removed."
    )
    assert track.is_file(), "the only copy is still there"


@pytest.mark.parametrize("album_first", [True, False])
def test_an_entry_an_album_row_and_a_singleton_row_both_name_reads_with_the_album_fix(
    client: TestClient, album_first: bool
) -> None:
    """One entry, two kinds of row: the remedy that CLEARS it wins.

    Deleting the album again drops the album's rows, so the album sentence is the
    one that moves the user forward; the other order would send them to beets for
    a row the Delete button removes.

    BOTH row orders, because the rows come back in one query and "the last one
    seen wins" gives the right answer for exactly one of them — measured, that
    mutant survived the single-order version of this test.
    """
    trash = _trash_dir(client)
    entry = trash / "Mixed"
    if album_first:
        _album_row_inside(client, entry)
        _album_row_inside(client, entry, album=False)
    else:
        _album_row_inside(client, entry, album=False)
        _album_row_inside(client, entry)

    r = client.delete("/api/trash", params={"folder": "Mixed"})

    assert r.status_code == 503
    assert r.json()["detail"] == (
        "Refused: 'Mixed' — the library still lists files inside."
        " Delete the album again, then empty Trash. Nothing was removed."
    )


def test_empty_all_keeps_both_listed_entries_and_names_each_cause(client: TestClient) -> None:
    """TWO still-listed entries in one sweep, asserted per copy.

    The cross-check answers for every entry in one query; an early exit that
    stopped at the first hit emptied the second one (measured on the previous
    code). Both causes are in the message, each with its own control.
    """
    trash = _trash_dir(client)
    first = trash / "AAA-album"
    second = trash / "BBB-track"
    first_track = _album_row_inside(client, first)
    second_track = _album_row_inside(client, second, album=False)
    (trash / "ZZZ-ordinary").mkdir(parents=True)

    r = client.delete("/api/trash/all")

    assert r.status_code == 503
    assert r.json()["detail"] == (
        "Refused: 'AAA-album' — the library still lists files inside."
        " Delete the album again, then empty Trash."
        " 'BBB-track' — the library still lists a track inside."
        " Move that entry out of Trash, or remove the track with beets."
        " Removed 1."
    )
    assert first_track.is_file(), "the first entry's only copy is still there"
    assert second_track.is_file(), "and so is the second's"
    assert not (trash / "ZZZ-ordinary").exists(), "the rest was still emptied"


def test_empty_one_refuses_when_the_music_root_itself_is_a_link(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked ``directory:`` with Trash configured inside it.

    The adapter already names this layout. Measured end to end by driving the
    app's own mover and printing what it stored: the configured Trash is
    RESOLVED at the source, so the mover writes to ``<real>/.trash/...`` and
    beets stores that row ABSOLUTE — the resolved root is not a string-child of
    the link spelling, so its relative-path rule does not fire. The row the app
    really leaves and the spelling this check asks with are the same one.

    The hand-written RELATIVE row in that layout is a residual, not this: beets
    would not store one, and its own query would not find it either.
    """
    real = tmp_path / "real-music"
    (real / ".trash").mkdir(parents=True)
    link = tmp_path / "music-link"
    link.symlink_to(real)
    app: Any = client.app
    app.state.beets_library.lib.directory = os.fsencode(str(link))
    monkeypatch.setattr("app.config.settings.trash_dir", str(link / ".trash"))
    track = _album_row_inside(client, real / ".trash" / "Art - Alb")

    r = client.delete("/api/trash", params={"folder": "Art - Alb"})

    assert r.status_code == 503
    assert "the library still lists files inside" in r.json()["detail"]
    assert track.is_file(), "the only copy is still there"


def test_an_empty_all_that_keeps_a_listed_entry_still_names_the_stuck_one(
    client: TestClient,
) -> None:
    """Two causes at once: the new refusal used to hide the other one on every retry.

    Measured: with a still-listed entry present, an entry that could not be
    removed was never named — not on that click and not on any later one,
    because the new rung always won. Every entry left in Trash is in this
    message.
    """
    if os.geteuid() == 0:
        pytest.skip("root ignores the directory mode this fault needs")

    trash = _trash_dir(client)
    listed = trash / "ZZZ-listed"
    _album_row_inside(client, listed)
    stuck = trash / "AAA-bad"
    stuck.mkdir(parents=True)
    (stuck / "x").write_bytes(b"x")
    (trash / "Ordinary").mkdir(parents=True)
    stuck.chmod(0o500)

    try:
        r = client.delete("/api/trash/all")
    finally:
        stuck.chmod(0o700)

    assert r.status_code == 503
    assert r.json()["detail"] == (
        "Refused: 'ZZZ-listed' — the library still lists files inside."
        " Delete the album again, then empty Trash."
        " Removed 1, 1 could not be removed ('AAA-bad')."
    )
    assert not (trash / "Ordinary").exists(), "the rest was still emptied"


def test_two_entries_differing_only_in_case_are_two_entries(client: TestClient) -> None:
    """The CONTROL for the case arm: it confirms by inode, not by lowercasing.

    Where the filesystem IS case-sensitive, ``ART - ALB`` and ``Art - Alb`` are
    two directories, and a row inside one says nothing about the other. Matching
    on the lowercased name alone would refuse this Empty for ever with a remedy
    that clears a different entry.
    """
    trash = _trash_dir(client)
    listed = trash / "Art - Alb"
    _album_row_inside(client, listed)
    other = trash / "ART - ALB"
    try:
        other.mkdir(parents=True)
    except FileExistsError:  # pragma: no cover - a casefolding tmpdir
        pytest.skip("this filesystem folds case, so there is only one entry here")
    if other.samefile(listed):  # pragma: no cover - ditto
        pytest.skip("this filesystem folds case, so there is only one entry here")

    r = client.delete("/api/trash", params={"folder": "ART - ALB"})

    assert r.status_code == 200
    assert not other.exists(), "the entry no row names was emptied"
    assert (listed / "01 T1.mp3").is_file(), "and the one a row names was not"


def test_empty_all_reads_the_library_once_for_the_whole_sweep(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cost invariant: the library is asked once per REQUEST, not per entry.

    All of it runs inside the swap lock, where every other library route waits;
    the measured figures are on ``trash_manage._listed_entries``. Counted rather
    than timed, so it cannot go flaky.
    """
    from app.beets import trash_manage

    trash = _trash_dir(client)
    for name in ("A", "B", "C"):
        (trash / name).mkdir(parents=True)
    calls: list[int] = []
    real = trash_manage._listed_entries

    def _counts(lib: Any, roots: Any, names: Any) -> Any:
        calls.append(len(list(names)))
        return real(lib, roots, names)

    monkeypatch.setattr(trash_manage, "_listed_entries", _counts)

    r = client.delete("/api/trash/all")

    assert r.status_code == 200
    assert calls == [3], "one call, carrying every entry"


def test_empty_all_counts_the_entries_the_page_showed(client: TestClient) -> None:
    """A dot-leading name is not a row in the listing, so it is not one in the count.

    A Trash inside the music library can hold a ``.musicdrop-keep`` a killed
    delete left behind: measured, one visible entry answered ``removed=2``. It
    is still removed — the count is the only thing at stake, and this file is the
    ONE exception to "the count is what Empty removed" because the app planted
    it.
    """
    trash = _trash_dir(client)
    (trash / "Art - Alb").mkdir(parents=True)
    (trash / ".musicdrop-keep").write_bytes(b"")

    r = client.delete("/api/trash/all")

    assert r.status_code == 200
    assert r.json()["removed"] == 1, "one entry, plus the app's own keep-file"
    assert list(trash.iterdir()) == [], "the leftover went with it"


def test_empty_all_counts_a_hidden_entry_it_destroyed(client: TestClient) -> None:
    """Everything Empty removes is in the count — hiding is not the app's doing.

    Measured on the previous predicate (skip every dot-leading name): a hidden
    directory AND its contents were destroyed while the response said
    ``removed: 0``. A zero also suppresses the orphan origin-record sweep, so the
    records of what went are left behind too.
    """
    trash = _trash_dir(client)
    hidden = trash / ".stash"
    hidden.mkdir(parents=True)
    (hidden / "keepsake.flac").write_bytes(b"x")

    r = client.delete("/api/trash/all")

    assert r.status_code == 200
    assert r.json()["removed"] == 1, "it was destroyed, so it is counted"
    assert not hidden.exists()


def test_the_aliases_beets_cannot_see_are_recorded_not_guarded(tmp_path: Path) -> None:
    """Four aliases of one file that beets' ``path:`` query does not equate. RESIDUAL.

    Recorded, not guarded, and this test exists to say which is which. beets
    compares path STRINGS; an alias it cannot see is an entry Empty removes while
    a row still names it. All four need a hand-edited row or an alias the config
    never named, so MusicDrop's own writer produces none of them
    (``tests/probes/alias_rows.py`` measures each end to end).

    A check of our own that saw them is exactly the code three rounds of review
    found faults in — a fail-open stat, an unconfirmed root spelling, a
    dangling row that refused for ever. The owner's call: do not re-implement
    what beets already answers.
    """
    from tests._mountns import run_probe, unshare_works

    required = os.environ.get("MUSICDROP_REQUIRE_CASEFOLD") == "1"
    if not unshare_works():
        if required:
            pytest.fail("MUSICDROP_REQUIRE_CASEFOLD=1 but this box has no mount namespace")
        pytest.skip("no unprivileged mount namespace on this box")

    work = tmp_path / "work"
    work.mkdir()
    lines = run_probe("alias_rows", work)

    if "CASE-INSENSITIVE True" not in lines:
        if required:
            pytest.fail(f"MUSICDROP_REQUIRE_CASEFOLD=1 but no casefold tmpfs: {lines}")
        pytest.skip(f"no casefolding tmpfs on this box: {lines}")

    for label in ("NFD-ROW", "SS-ROW", "BIND", "SYMLINK"):
        # The premise first: each fixture really is one file under two names.
        assert f"{label} SAME FILE True" in lines, lines
        assert f"{label} REMOVED" in lines, lines
        assert f"{label} ONLY COPY SURVIVED False" in lines, lines


def test_empty_refuses_a_row_that_spells_the_entry_in_another_case(tmp_path: Path) -> None:
    """One inode, two spellings: a casefolding filesystem, not a contrived one.

    Measured by the security seat on a real ``casefold=utf8-12.1.0`` tmpfs: the
    byte-prefix check saw no match, Empty ran, and the album's only copy was
    destroyed while the album stayed listed. beets decides case sensitivity by
    PROBING the filesystem the path is on and folds both sides when it finds one,
    so two genuinely different entries on a case-SENSITIVE filesystem are not
    refused — ``test_two_entries_differing_only_in_case_are_two_entries`` is that
    control, and
    ``test_case_sensitivity_is_decided_by_probing_the_filesystem`` pins the beets
    property both rest on.

    Needs a casefolding tmpfs inside a mount namespace, so it runs as a probe,
    with the same skip-guard as its neighbours: ``MUSICDROP_REQUIRE_CASEFOLD=1``
    turns the skip into a failure.
    """
    from tests._mountns import run_probe, unshare_works

    required = os.environ.get("MUSICDROP_REQUIRE_CASEFOLD") == "1"
    if not unshare_works():
        if required:
            pytest.fail("MUSICDROP_REQUIRE_CASEFOLD=1 but this box has no mount namespace")
        pytest.skip("no unprivileged mount namespace on this box")

    work = tmp_path / "work"
    work.mkdir()
    lines = run_probe("casefold_empty", work)

    if "CASE-INSENSITIVE True" not in lines:
        if required:
            pytest.fail(f"MUSICDROP_REQUIRE_CASEFOLD=1 but no casefold tmpfs: {lines}")
        pytest.skip(f"no casefolding tmpfs on this box: {lines}")

    assert "SAME FILE True" in lines, lines  # the fixture really is one inode
    assert "EMPTY ONE REFUSED" in lines, lines
    assert "ONLY COPY SURVIVED ONE True" in lines, lines
    # The same divergence one level up: the Trash ROOT spelled in another case.
    assert "SAME FILE ROOT True" in lines, lines
    assert "EMPTY ROOT-CASE REFUSED" in lines, lines
    assert "ONLY COPY SURVIVED ROOT-CASE True" in lines, lines
    assert "EMPTY ALL REFUSED" in lines, lines
    assert "ONLY COPY SURVIVED ALL True" in lines, lines
    assert "ALBUM STILL LISTED 2" in lines, lines


def test_empty_rejects_path_traversal_404(client: TestClient) -> None:
    r = client.delete("/api/trash", params={"folder": "../escape"})
    assert r.status_code == 404


def test_empty_unknown_folder_404(client: TestClient) -> None:
    r = client.delete("/api/trash", params={"folder": "does-not-exist"})
    assert r.status_code == 404


def test_empty_overlong_folder_404_not_500(client: TestClient) -> None:
    """A >255-byte folder name must hit the same 404 as any other unknown
    folder. Path.exists() raises OSError(ENAMETOOLONG) on such a component
    (it only swallows ENOENT/ENOTDIR/EBADF/ELOOP), which used to 500 the
    endpoint from inside the resolver's not-found check. A sibling is seeded so
    the base dir exists — the kernel only reports ENAMETOOLONG once every
    leading component resolved (a missing base dir answers ENOENT instead)."""
    trash = _trash_dir(client)
    (trash / "Album").mkdir(parents=True)
    r = client.delete("/api/trash", params={"folder": "x" * 300})
    assert r.status_code == 404
    assert (trash / "Album").exists()  # the overlong name must not drag it down


def test_restore_overlong_folder_404_not_500(client: TestClient) -> None:
    trash = _trash_dir(client)
    (trash / "A").mkdir(parents=True)
    r = client.post("/api/trash/restore", json={"folder": "x" * 300})
    assert r.status_code == 404
    assert (trash / "A").exists()


def test_restore_503_when_the_music_share_is_unavailable(
    client: TestClient, tmp_path: Path
) -> None:
    """A move-back writes INTO the music library, so a dropped share is a 503 —
    the same answer delete gives — and not the blanket 500. The guard fires
    before anything leaves Trash, so the folder is still there afterwards."""
    trash = _trash_dir(client)
    entry = trash / "Dummy"
    entry.mkdir(parents=True)
    (entry / "cover.jpg").write_bytes(b"\x00")
    write_trash_origin(
        _origins_dir(client), entry.name, origin=str(tmp_path / "music" / "Dummy"), moved="folder"
    )
    shutil.rmtree(tmp_path / "music")

    r = client.post("/api/trash/restore", json={"folder": "Dummy"})

    assert r.status_code == 503
    assert (entry / "cover.jpg").exists()


def test_restore_503_when_the_entry_holds_a_non_regular_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The status the pre-flight's refusal reaches the page as.

    503 and not the blanket 500: the refusal fires before anything leaves Trash
    and the fix is the operator's, which is the tier the route already gives the
    dropped share and the protected tree. It also needs no OpenAPI change — the
    declared 503 reads "the folder was not moved out of Trash; the message says
    which setup fault refused it", true of this word for word — and the page
    renders the sentence (``SettingsTrashPage.tsx`` shows
    ``restore.error.message``, which ``useTrash.ts`` sets from ``detail``).

    The refusal is STUBBED rather than planted as a real FIFO, and that is the
    measurement talking: with the pre-flight removed, a real plant here leaves
    the request wedged in the threadpool holding ``app.state.beets_swap_lock``
    for the rest of the session, and ``test_empty_one_holds_swap_lock_during_removal``
    and its twin then fail too (measured 2026-09-13, both pass alone under the
    same mutant). The bounded refusal itself is pinned on the real plant, off the
    event loop, in ``test_trash_manage.py``; what is left for this seat is the
    mapping, and a stub cannot wedge anything.
    """
    trash = _trash_dir(client)
    entry = trash / "Dummy"
    entry.mkdir(parents=True)
    (entry / "cover.jpg").write_bytes(b"\x00")

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise TrashEntryUnreadableError(
            "This Trash entry holds '02 wedge.flac', which is not a regular file, so it"
            " was not restored."
        )

    monkeypatch.setattr("app.api.trash.restore_album", refuse)

    r = client.post("/api/trash/restore", json={"folder": "Dummy"})

    assert r.status_code == 503
    assert "02 wedge.flac" in r.json()["detail"]
    assert (entry / "cover.jpg").exists(), "nothing left Trash"


def test_restore_409_when_a_library_job_is_active(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.import_jobs.registry import get_registry

    monkeypatch.setattr(get_registry(), "has_active_job", lambda: True)
    r = client.post("/api/trash/restore", json={"folder": "whatever"})
    assert r.status_code == 409


class _Locked:
    """Stub for a held beets swap lock (``asyncio.Lock`` in production)."""

    @staticmethod
    def locked() -> bool:
        return True


def test_empty_one_409_while_swap_lock_held(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restore holds the beets swap lock WITHOUT registering as a library job,
    so the job-only gate let empty-trash proceed and ``rmtree`` the very folder the
    restore was mid-move on — irreversible loss. The gate must also see the lock."""
    from app.main import app

    trash = _trash_dir(client)
    (trash / "Album").mkdir(parents=True)
    monkeypatch.setattr(app.state, "beets_swap_lock", _Locked(), raising=False)
    r = client.delete("/api/trash", params={"folder": "Album"})
    assert r.status_code == 409
    assert (trash / "Album").exists()  # NOT deleted out from under the restore


def test_empty_all_409_while_swap_lock_held(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty-all must likewise refuse while a restore holds the swap lock."""
    from app.main import app

    trash = _trash_dir(client)
    (trash / "A").mkdir(parents=True)
    monkeypatch.setattr(app.state, "beets_swap_lock", _Locked(), raising=False)
    r = client.delete("/api/trash/all")
    assert r.status_code == 409
    assert (trash / "A").exists()


def test_empty_one_holds_swap_lock_during_removal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty must HOLD the swap lock while it rmtrees, so a Restore starting
    mid-Empty sees it (raise_if_library_busy) and 409s — this closes the
    symmetric Empty→Restore race (a Restore move-importing the folder Empty is
    concurrently deleting). Job-only exclusion left Empty invisible."""
    import app.api.trash as trash_mod
    from app.beets.trash_manage import empty_one as real_empty_one
    from app.main import app

    trash = _trash_dir(client)
    (trash / "Album").mkdir(parents=True)
    seen: dict[str, bool] = {}

    def spy(
        path: str,
        *,
        origins_dir: Path,
        protected: ProtectedTrees,
        lib: object = None,
    ) -> EmptyResult:
        lock = getattr(app.state, "beets_swap_lock", None)
        seen["locked"] = lock is not None and lock.locked()
        return real_empty_one(
            path,
            origins_dir=origins_dir,
            protected=protected,
            lib=lib,  # type: ignore[arg-type]  # the route's own handle, typed loosely here
        )

    monkeypatch.setattr(trash_mod, "empty_one", spy)
    r = client.delete("/api/trash", params={"folder": "Album"})
    assert r.status_code == 200
    assert seen.get("locked") is True  # the swap lock was held across the rmtree


def test_empty_all_holds_swap_lock_during_removal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty-all likewise holds the swap lock across its removals."""
    import app.api.trash as trash_mod
    from app.beets.trash_manage import empty_all as real_empty_all
    from app.main import app

    trash = _trash_dir(client)
    (trash / "A").mkdir(parents=True)
    seen: dict[str, bool] = {}

    def spy(
        trash_dir: Path, *, origins_dir: Path, protected: ProtectedTrees, lib: object = None
    ) -> EmptyResult:
        lock = getattr(app.state, "beets_swap_lock", None)
        seen["locked"] = lock is not None and lock.locked()
        return real_empty_all(trash_dir, origins_dir=origins_dir, protected=protected, lib=lib)  # type: ignore[arg-type]  # ditto

    monkeypatch.setattr(trash_mod, "empty_all", spy)
    r = client.delete("/api/trash/all")
    assert r.status_code == 200
    assert seen.get("locked") is True


def test_restore_409_while_swap_lock_held(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The symmetric guarantee that makes Empty holding the lock effective:
    Restore refuses (409) while the swap lock is held, so it can never start
    move-importing during an in-flight Empty (or config Apply / resolve)."""
    from app.main import app

    monkeypatch.setattr(app.state, "beets_swap_lock", _Locked(), raising=False)
    r = client.post("/api/trash/restore", json={"folder": "whatever"})
    assert r.status_code == 409
