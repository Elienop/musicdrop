"""The Trash directory is created by the code that takes its identity.

A mover that sees ``protected.trash is None`` has no identity to compare, and
the arm that used to create the Trash there opened it BY PATH: measured
(security seat M-3), ``mkdir(parents=True)`` plus a leaf-only ``O_NOFOLLOW``
followed a symlink swapped in at an INTERMEDIATE component of a Trash at
``<M>/a/b/.trash``, the files left the music library for good, and every later
request then stat'd and opened the relocation through the same link and agreed
with it.

So the creation moved to ``store_layout.ensure_trash_root``, one line before the
stat that records the identity, and below the music root it goes through the
parent's descriptor — the chain the owner's layout ruling leaves
attacker-writable.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.beets.library import LibraryHandle
from app.beets.protected import ProtectedTrees
from app.beets.store_layout import StoreLayoutError, checked_protected_trees
from app.beets.trash import resolve_trash_dir
from app.config import Settings
from tests.conftest import build_library, make_test_handle, origins_for


def _handle(tmp_path: Path, music: Path) -> LibraryHandle:
    """A library handle whose music root is ``music`` and beets dir a sibling."""
    beets_dir = tmp_path / "beets"
    beets_dir.mkdir(exist_ok=True)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    return make_test_handle(lib, beets_dir)


def _trees(tmp_path: Path, music: Path, configured: Path | None) -> tuple[ProtectedTrees, Path]:
    """What a destructive request builds, through the real resolver.

    ``configured`` is what the operator spelled (``None`` = the shipped default,
    ``<beets_dir>/trash``); the resolved pair is what production hands on, so
    these tests resolve it the same way instead of naming it twice.
    """
    handle = _handle(tmp_path, music)
    settings = Settings(trash_dir=str(configured) if configured else "")
    trash_dir = resolve_trash_dir(settings, handle)
    trees = checked_protected_trees(
        settings, handle, trash_dir=trash_dir, origins_dir=origins_for(trash_dir)
    )
    return trees, trash_dir


def test_the_default_trash_is_created_and_its_identity_taken(tmp_path: Path) -> None:
    """First use of a fresh install: nothing creates the Trash at boot.

    The shipped default is ``<beets_dir>/trash``, whose chain is the operator's,
    so it is created with ``mkdir(parents=True)`` — and the identity the movers
    compare is the one taken right after.
    """
    music = tmp_path / "music"
    music.mkdir()

    trees, trash_dir = _trees(tmp_path, music, None)

    assert trash_dir.is_dir(), "created where the identity is taken"
    st = os.stat(trash_dir)
    assert trees.trash == (st.st_dev, st.st_ino)


def test_a_trash_below_the_music_root_is_created_part_by_part(tmp_path: Path) -> None:
    """The allowed nested layout, all of it missing: every part is created.

    ``store_layout`` permits a Trash strictly inside the music library (the
    owner's ruling — deletes are then same-disk renames), so the missing parts
    are made through the parent's descriptor rather than by path.
    """
    music = tmp_path / "music"
    music.mkdir()

    trees, trash_dir = _trees(tmp_path, music, music / "a" / "b" / ".trash")

    assert trash_dir == music / "a" / "b" / ".trash"
    assert trash_dir.is_dir()
    st = os.stat(trash_dir)
    assert trees.trash == (st.st_dev, st.st_ino)


def test_a_symlinked_component_below_the_music_root_is_refused(tmp_path: Path) -> None:
    """M-3, closed: the escape was a symlink at an INTERMEDIATE component.

    ``mkdir(parents=True)`` created the Trash through it and the leaf-only
    ``O_NOFOLLOW`` open saw nothing wrong, so the app's recovery store moved
    outside the music library permanently. The anchored walk refuses the part.
    """
    music = tmp_path / "music"
    music.mkdir()
    holder = music / "a"
    holder.mkdir()
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    os.rename(holder, tmp_path / "real-a")
    os.symlink(elsewhere, holder)

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, music / "a" / "b" / ".trash")

    assert "not reachable below the music library" in str(caught.value)
    assert list(elsewhere.iterdir()) == [], "nothing created outside the library"
    assert not (tmp_path / "real-a" / "b").exists()


def test_the_refusal_holds_for_every_later_request(tmp_path: Path) -> None:
    """No self-validating relocation: the second request refuses too.

    Measured on the arm this replaces (probe_2a4): once the Trash existed
    THROUGH the link, the per-request stat and the mover's open agreed on it,
    so the relocation validated itself and ``DELETE /api/trash/all`` then
    emptied the attacker's directory.
    """
    music = tmp_path / "music"
    music.mkdir()
    holder = music / "a"
    holder.mkdir()
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    os.rename(holder, tmp_path / "real-a")
    os.symlink(elsewhere, holder)

    for _attempt in (1, 2):
        with pytest.raises(StoreLayoutError):
            _trees(tmp_path, music, music / "a" / "b" / ".trash")

    assert sorted(p.name for p in elsewhere.rglob("*")) == []


def test_a_file_in_the_way_below_the_music_root_is_refused(tmp_path: Path) -> None:
    """The same refusal for a plain file at a part: the errno cannot tell them
    apart (measured — ``O_DIRECTORY|O_NOFOLLOW`` answers ENOTDIR for both), so
    the message says "not reachable", not "a link"."""
    music = tmp_path / "music"
    music.mkdir()
    (music / "a").write_bytes(b"not a directory")

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, music / "a" / "b" / ".trash")

    assert "not reachable below the music library" in str(caught.value)


def test_a_trash_outside_the_library_is_created_through_the_operators_chain(
    tmp_path: Path,
) -> None:
    """A Trash on another disk, spelled through a link the OPERATOR owns.

    An anchored walk would refuse this, and refusing it would refuse the layout
    the owner supports — so the arm that is not below the music root keeps
    ``mkdir(parents=True)``, links and all. ``resolve_trash_dir`` collapses the
    link, which is why the spelling test cannot find this one below the music
    root either way.
    """
    music = tmp_path / "music"
    music.mkdir()
    other_disk = tmp_path / "other-disk"
    other_disk.mkdir()
    os.symlink(other_disk, tmp_path / "mounted")

    trees, trash_dir = _trees(tmp_path, music, tmp_path / "mounted" / "deep" / "trash")

    assert (other_disk / "deep" / "trash").is_dir(), "created through the operator's own link"
    st = os.stat(trash_dir)
    assert trees.trash == (st.st_dev, st.st_ino)


def test_a_trash_that_cannot_be_created_is_refused_not_skipped(tmp_path: Path) -> None:
    """An uncreatable Trash is a 503 naming the setting, not a silent absence.

    The identity would be ``None`` and every mover would then refuse with the
    remover's wording; one refusal here says which path and why.
    """
    music = tmp_path / "music"
    music.mkdir()
    (music / "a").mkdir()
    os.chmod(music / "a", 0o500)
    try:
        with pytest.raises(StoreLayoutError) as caught:
            _trees(tmp_path, music, music / "a" / ".trash")
    finally:
        os.chmod(music / "a", 0o700)  # or the tmp_path teardown cannot clean up

    assert "could not be created" in str(caught.value)


@pytest.mark.skipif(os.getuid() == 0, reason="root writes a read-only directory anyway")
def test_the_trash_routes_answer_503_when_the_trash_cannot_be_created(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``DELETE /api/trash/all`` relays the refusal as the layout 503 it is.

    The creation moved INTO ``checked_protected_trees``, which the two
    destructive route helpers used to call outside their ``except
    StoreLayoutError`` arm — so this refusal would have been a 500.
    """
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    monkeypatch.setattr("app.config.settings.trash_dir", str(locked / "trash"))

    try:
        resp = client.delete("/api/trash/all")
    finally:
        locked.chmod(0o700)

    assert resp.status_code == 503
    assert "could not be created" in resp.json()["detail"]


def test_a_relative_trash_setting_below_the_music_root_is_anchored_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cwd-relative ``MUSICDROP_TRASH_DIR`` is the same layout, spelled shorter.

    ``config.py`` names the cwd-relative gotcha and this app's own defaults are
    relative, so the spelling test absolutises before it compares — without
    that, a relative setting never reads as "below the music root" and the whole
    chain is created by path.
    """
    music = tmp_path / "music"
    music.mkdir()
    holder = music / "a"
    holder.mkdir()
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    os.rename(holder, tmp_path / "real-a")
    os.symlink(elsewhere, holder)
    monkeypatch.chdir(music)

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, Path("a/b/.trash"))

    assert "not reachable below the music library" in str(caught.value)
    assert list(elsewhere.iterdir()) == []
