"""The listing row for a Trash entry that is itself a SYMLINK.

Nothing hostile is needed to get one there: ``trash._album_root`` is
``dirname(item.path)``, so an album whose own folder is a symlink into another
volume is trashed AS a symlink, because ``shutil.move`` recreates the link and
unlinks the original. Only the LINK ever moves — the album's files stay on the
volume they were on.

``trash._record_origin`` declines to write a record for such an entry, on
purpose, and the row then fell through to the generic no-record note. Every
claim in that sentence is false here, which is what this file pins: nothing
failed and the log says nothing, the row is not old, and "Restoring re-imports
it" is not on offer at all — ``resolve_trash_child`` refuses a child that is a
link, so Restore answers 404 and the note describes an action the app will not
take.

The note is pinned against OUTCOMES, not against its own wording. A membership
assertion on a phrase passes just as happily when the sentence is false: the
first version of this file asserted ``"removes only the link"`` while the Empty
button rendered beside that sentence answered 404 and left the link exactly
where it was. So the route tests below drive all four endpoints through the real
app and read each of the note's claims against what the route actually did.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.beets.trash_manage import list_trashed_albums, resolve_trash_child
from app.beets.trash_origins import write_trash_origin
from tests.conftest import origins_for


def _trash_with_a_symlinked_entry(tmp_path: Path) -> tuple[Path, Path]:
    """A Trash dir holding one ordinary entry and one symlinked one."""
    trash, elsewhere = tmp_path / "trash", tmp_path / "elsewhere"
    trash.mkdir()
    elsewhere.mkdir()
    (elsewhere / "01 a.flac").write_bytes(b"\x00")
    (tmp_path / "music").mkdir()
    (trash / "Real Album").mkdir()
    (trash / "Symlinked Album").symlink_to(elsewhere, target_is_directory=True)
    return trash, elsewhere


def _rows(trash: Path, tmp_path: Path) -> dict[str, tuple[str, str | None, str | None]]:
    return {
        row.folder: (row.restore_mode, row.restore_note, row.origin)
        for row in list_trashed_albums(
            trash, origins_dir=origins_for(trash), music_dir=str(tmp_path / "music")
        )
    }


def test_a_symlinked_row_says_what_restore_will_really_do(tmp_path: Path) -> None:
    """The note has to match the route, and it has to say where the files are.

    The user's next action here is not "wait", "re-point the library" or "put it
    back by hand" — it is "go to the volume the link points at", and the reason
    that action exists at all is the good news this row carries: the album's
    files were never moved. The generic no-record sentence sent them to look at
    the folder's age or at the server log instead, for an event that never
    happened.
    """
    trash, _ = _trash_with_a_symlinked_entry(tmp_path)

    mode, note, origin = _rows(trash, tmp_path)["Symlinked Album"]

    assert mode == "refused", "no per-row route will act on this at all"
    assert note is not None
    assert "link to a folder elsewhere" in note, "why it cannot be restored"
    assert "will not restore it" in note, "and that MusicDrop will not try"
    assert "files were never moved" in note, "where the album actually is"
    assert "before origins were recorded" not in note, "neither record cause applies"
    assert "writing that record failed" not in note
    assert origin is None


def test_the_symlinked_row_matches_what_the_restore_route_will_do(tmp_path: Path) -> None:
    """The note and the route have to agree, so both are asserted together.

    ``resolve_trash_child`` is the guard the endpoint maps to its 404, and it
    refuses on the listing's own predicate — the child IS a link — rather than
    on where the link resolves to. A note promising a re-import while this
    refuses is the mismatch the row used to ship. The ordinary sibling is
    asserted in the same breath, so a change that silenced the note by breaking
    the whole listing would be visible.
    """
    trash, _ = _trash_with_a_symlinked_entry(tmp_path)
    rows = _rows(trash, tmp_path)

    with pytest.raises(ValueError):
        resolve_trash_child(trash, "Symlinked Album")

    assert resolve_trash_child(trash, "Real Album") == (trash / "Real Album").resolve()
    real_mode, real_note, _ = rows["Real Album"]
    assert real_mode == "import"
    assert real_note is not None
    assert "before origins were recorded" in real_note, "the ordinary row keeps its own sentence"


def test_a_record_under_the_name_cannot_out_vote_the_link(tmp_path: Path) -> None:
    """The link is asked BEFORE the record, and a record must not overturn it.

    Such a record is reachable: the store is keyed on the entry NAME, so one
    written for a DIFFERENT entry that held this name earlier can still be
    sitting there (the hazard ``trash._unique_trash_dest`` closes by refusing to
    hand out a recorded name). ``_record_origin`` never writes one for a
    symlinked entry itself.

    Ordered the other way round — the link tested inside the "no record" branch —
    this row renders ``move_back`` with an origin, i.e. an "Exact restore"
    promise on a row whose Restore button 404s, which is the exact failure the
    symlink refusal exists to prevent. The record here names a folder INSIDE the
    music dir and is ``moved="folder"``, so nothing else would downgrade it.
    """
    trash, _ = _trash_with_a_symlinked_entry(tmp_path)
    write_trash_origin(
        origins_for(trash),
        "Symlinked Album",
        origin=str(tmp_path / "music" / "Symlinked Album"),
        moved="folder",
    )

    mode, note, origin = _rows(trash, tmp_path)["Symlinked Album"]

    assert mode == "refused", "the link decides alone"
    assert origin is None, "and no path is offered as one it would go back to"
    assert note is not None
    assert "link to a folder elsewhere" in note


def _client_trash(client: TestClient) -> Path:
    return Path(client.get("/api/trash").json()["trash_path"])


def _seed_symlinked_entry(client: TestClient, tmp_path: Path) -> tuple[Path, Path]:
    """One symlinked Trash entry pointing at a folder outside Trash entirely.

    Returns ``(the link, the folder it points at)``. The target holds a file so
    every assertion below can say whether anything behind the link was touched.
    """
    trash = _client_trash(client)
    trash.mkdir(parents=True, exist_ok=True)  # created lazily by the mover in production
    elsewhere = tmp_path / "other volume" / "Portishead - Dummy"
    elsewhere.mkdir(parents=True)
    (elsewhere / "01 Mysterons.flac").write_bytes(b"\x00")
    entry = trash / "Portishead - Dummy"
    entry.symlink_to(elsewhere, target_is_directory=True)
    return entry, elsewhere


def test_the_symlinked_note_is_true_of_every_button_beside_it(
    client: TestClient, tmp_path: Path
) -> None:
    """Each claim in the note, read against what the real route did.

    The row renders two controls, and until this test only ONE of them had been
    checked against the sentence. Both per-row routes refuse a child that is a
    link, so both answer 404 — the Empty beside the note included, which the
    note used to describe as working.

    ``restore_mode`` is asserted here too, because it is what the page keys the
    two disabled states off: a row that 404s twice must not arrive carrying a
    mode that reads as "this will import".
    """
    entry, elsewhere = _seed_symlinked_entry(client, tmp_path)

    (row,) = client.get("/api/trash").json()["albums"]
    restore = client.post("/api/trash/restore", json={"folder": entry.name})
    empty_one = client.delete("/api/trash", params={"folder": entry.name})

    note = row["restore_note"]
    assert row["restore_mode"] == "refused", "the value the UI disables both controls on"
    # "MusicDrop will not restore it" -> the route refuses before doing anything.
    assert "will not restore it" in note
    assert restore.status_code == 404
    assert restore.json()["detail"] == "Not in Trash"
    # "Restore and this row's own Empty both refuse it" -> the second half, which
    # the sentence used to get wrong in the user's favour.
    assert "own Empty both refuse it" in note
    assert empty_one.status_code == 404
    assert empty_one.json()["detail"] == "Not in Trash"
    # "the album's own files were never moved" -> nothing behind the link moved,
    # and the link itself is still in Trash after two refusals.
    assert "files were never moved" in note
    assert entry.is_symlink()
    assert (elsewhere / "01 Mysterons.flac").is_file()


def test_empty_all_removes_the_link_and_only_the_link(client: TestClient, tmp_path: Path) -> None:
    """The other half of the same sentence: what DOES clear this entry.

    ``empty_all`` unlinks a symlinked child instead of following it, which is
    both the only route that can remove this row and the reason the note can
    promise the album survives. Asserted together, because the sentence is only
    true if both hold: the entry goes, and the folder it pointed at does not.
    """
    entry, elsewhere = _seed_symlinked_entry(client, tmp_path)
    (row,) = client.get("/api/trash").json()["albums"]

    r = client.delete("/api/trash/all")

    assert "Empty all removes the link, and only the link" in row["restore_note"]
    assert r.status_code == 200
    assert r.json()["removed"] == 1
    assert not entry.is_symlink()
    assert not entry.exists()
    assert (elsewhere / "01 Mysterons.flac").is_file(), "nothing behind the link was followed"


def test_the_symlinked_note_never_promises_the_per_row_empty(
    client: TestClient, tmp_path: Path
) -> None:
    """A guard on the CLAIM, not on one phrasing of it.

    The two tests above pin the sentences that are there; this pins the sentence
    that must not come back. "Emptying this entry" and its neighbours describe
    the button that 404s, so any rewording that reintroduces one is describing a
    route the row does not offer.
    """
    _seed_symlinked_entry(client, tmp_path)

    (row,) = client.get("/api/trash").json()["albums"]

    note = row["restore_note"]
    for forbidden in ("Emptying this entry", "Emptying it", "Empty removes"):
        assert forbidden not in note, f"{forbidden!r} describes the Empty that 404s"


def _seed_alias_to_a_sibling_entry(client: TestClient) -> tuple[Path, Path]:
    """``<trash>/Alias -> ./RealAlbum``: a link whose target is ANOTHER Trash row.

    The same ordinary shape as the link above (an album folder that was already
    a symlink when it was deleted), pointed the one way the routes' guard could
    not see: the resolved child lands INSIDE the Trash dir, so containment said
    yes. Relative on purpose — that is what a link written inside a moved folder
    looks like — but an absolute one reaching back into Trash behaves the same.

    Returns ``(the link, the sibling entry it points at)``.
    """
    trash = _client_trash(client)
    trash.mkdir(parents=True, exist_ok=True)
    real = trash / "RealAlbum"
    real.mkdir()
    (real / "01 Mysterons.flac").write_bytes(b"\x00")
    alias = trash / "Alias"
    alias.symlink_to(Path("RealAlbum"), target_is_directory=True)
    return alias, real


def test_a_link_to_a_sibling_entry_is_refused_by_both_per_row_routes(client: TestClient) -> None:
    """The row said "refused" while Empty went through the link and ate the sibling.

    Two different predicates decided the same question: the listing asked
    ``os.path.islink`` on the entry, the routes resolved the child and tested
    containment. They agree for a link pointing OUT of Trash and disagree here,
    where the target IS a Trash entry — measured before they were one predicate,
    ``DELETE /api/trash?folder=Alias`` answered ``200 {"removed": 1}``, removed
    ``RealAlbum`` (a different row, with its own Restore) and left ``Alias``
    sitting in Trash.

    So the assertion is not just the status: it is that the sibling is still
    there afterwards, and that the link the user aimed at is too.
    """
    alias, real = _seed_alias_to_a_sibling_entry(client)

    rows = {row["folder"]: row for row in client.get("/api/trash").json()["albums"]}
    empty = client.delete("/api/trash", params={"folder": "Alias"})
    restore = client.post("/api/trash/restore", json={"folder": "Alias"})

    assert rows["Alias"]["restore_mode"] == "refused", "the row the routes must agree with"
    assert empty.status_code == 404, "the per-row Empty must not act through the link"
    assert empty.json()["detail"] == "Not in Trash"
    assert restore.status_code == 404
    assert (real / "01 Mysterons.flac").is_file(), "the sibling row was not touched"
    assert alias.is_symlink(), "and the entry the request named is still there"


def test_a_path_through_a_link_is_refused_as_well(client: TestClient) -> None:
    """Not only the leaf: ``folder`` is a request string, so it can name a child.

    ``Alias/Disc 1`` resolves to ``RealAlbum/Disc 1`` — inside Trash, existing,
    and part of a row the user did not name. The refusal walks every component
    between the Trash dir and the target for that reason; a leaf-only test
    clears this one.
    """
    alias, real = _seed_alias_to_a_sibling_entry(client)
    disc = real / "Disc 1"
    disc.mkdir()
    (disc / "02 Sour Times.flac").write_bytes(b"\x00")

    empty = client.delete("/api/trash", params={"folder": "Alias/Disc 1"})

    assert empty.status_code == 404
    assert (disc / "02 Sour Times.flac").is_file(), "nothing behind the link was removed"
    assert alias.is_symlink()


def test_empty_all_still_clears_a_link_that_points_at_a_sibling(client: TestClient) -> None:
    """The one route that does remove these entries keeps working on this shape.

    Both entries go, in either ``iterdir`` order: the link is unlinked rather
    than followed, and whichever of the two is removed first leaves the other
    removable (a link left dangling by its target's removal is still a link).
    """
    alias, real = _seed_alias_to_a_sibling_entry(client)

    r = client.delete("/api/trash/all")

    assert r.status_code == 200
    assert r.json()["removed"] == 2
    assert not alias.is_symlink()
    assert not real.exists()
    assert list(_client_trash(client).iterdir()) == []
