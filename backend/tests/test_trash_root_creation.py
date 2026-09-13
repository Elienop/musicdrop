"""The Trash directory is created by the code that takes its identity.

A mover that sees ``protected.trash is None`` has no identity to compare, and
the arm that used to create the Trash there opened it BY PATH: measured
(security seat M-3), ``mkdir(parents=True)`` plus a leaf-only ``O_NOFOLLOW``
followed a symlink swapped in at an INTERMEDIATE component of a Trash at
``<M>/a/b/.trash``, the files left the music library for good, and every later
request then stat'd and opened the relocation through the same link and agreed
with it.

So the creation moved to ``store_layout._ensure_trash_root``, which hands back
the identity of the descriptor its own walk reached, and below the music root
every part is opened and created through its parent's descriptor — the chain the
owner's layout ruling leaves attacker-writable. Where "below the music root"
begins is decided by IDENTITY, because the two settings can spell one root two
ways (security seat H-2).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from beets.library import Item
from fastapi.testclient import TestClient

from app.beets.disk_sync import run_disk_sync
from app.beets.library import (
    LibraryHandle,
    LibraryRootUnavailableError,
    require_library_root,
)
from app.beets.protected import (
    ProtectedTreeError,
    ProtectedTrees,
    open_checked_dir,
    protected_trees,
)
from app.beets.store_layout import StoreLayoutError, _elided, checked_protected_trees
from app.beets.trash import resolve_trash_dir
from app.config import Settings
from tests.conftest import build_library, make_test_handle, origins_for


def _ignore(_value: object) -> None:
    """A progress callback the tests do not read."""


def _never() -> bool:
    """A stop predicate that never fires."""
    return False


def _ident_of(path: Path) -> tuple[int, int]:
    """``(st_dev, st_ino)``, the pair the movers compare."""
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


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
    so the walk creates every part of it through its parent's descriptor and
    opens none of them through a link: a link there is resolved by the walk
    itself and refused only when its target lands inside the library (owner
    ruling 2026-09-13). The identity the movers compare is ``fstat`` on the
    descriptor it ends on.
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
    _library_root_with(music)

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

    Refusing a link ABOVE the music root would refuse the layout the owner
    supports, so the walk follows every part until one of them IS the music root
    — and this chain never is, which the ``..`` climb from its deepest existing
    part confirms rather than assumes. The link pointing OUTSIDE is not what
    makes this arm safe (one pointing INSIDE is refused by
    ``test_a_trash_that_reaches_into_the_library_without_naming_it_is_refused``);
    what makes it safe is that nothing here is below the root.
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


def test_an_unmounted_music_root_is_reported_as_the_music_roots_fault(tmp_path: Path) -> None:
    """A dropped share is the ``directory:`` setting's fault, not the Trash's.

    Measured 2026-09-12 (security seat L-2, code seat W3) on the arm this
    replaces: with the README's own ``<M>/.trash`` layout and the share down, the
    delete route answered "MUSICDROP_TRASH_DIR could not be created … Fix its
    permissions or its mount", where before this round the operator got "Is the
    music share mounted?" and a remount as the remedy.
    """
    music = tmp_path / "music"  # never created: this is the bare mountpoint gone

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, music / ".trash")

    assert "could not be opened" in str(caught.value)
    assert "Is the music share mounted?" in str(caught.value)
    assert not music.exists(), "the music root is not created for the Trash's sake"


def test_the_default_trash_is_still_created_when_the_music_root_is_gone(tmp_path: Path) -> None:
    """The dropped share refuses only the arm that would write into the library.

    The shipped ``<beets_dir>/trash`` is outside it, so Empty Trash on a dropped
    share still has a Trash to work in — refusing that would be a refusal about a
    directory the fault cannot reach.
    """
    music = tmp_path / "music"

    _trees(tmp_path, music, None)

    assert (tmp_path / "beets" / "trash").is_dir()


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


def test_the_trash_routes_answer_503_when_the_library_looks_unmounted(
    client: TestClient, beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The presence guard reaches the routes as the sentence README documents.

    ``checked_protected_trees`` raises ``LibraryRootUnavailableError`` now, which
    is not a ``StoreLayoutError`` — without its own arm the guard would leave a
    500 on the one fault ("the share is down") the operator most needs named.
    """
    music = Path(beets_library.lib.directory.decode())  # the fixture's root: empty
    monkeypatch.setattr("app.config.settings.trash_dir", str(music / "a" / ".trash"))

    resp = client.delete("/api/trash/all")

    assert resp.status_code == 503
    assert "Is the music share mounted?" in resp.json()["detail"]
    assert list(music.iterdir()) == [], "nothing created on the bare mountpoint"


def test_a_relative_trash_setting_below_the_music_root_is_anchored_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cwd-relative ``MUSICDROP_TRASH_DIR`` is the same layout, spelled shorter.

    ``config.py`` names the cwd-relative gotcha and this app's own defaults are
    relative, so the walk absolutises the spelling before it descends it:
    without that, ``parts[1:]`` drops the first component and the walk descends a
    DIFFERENT absolute path from ``/`` — measured 2026-09-12, ``a/b/.trash``
    became an attempt to create ``/b/.trash``.
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


def _library_root_with(music: Path) -> None:
    """Give the music root one entry, so it reads as a mounted share.

    ``checked_protected_trees`` refuses to create anything below a music root
    that looks unmounted (``require_library_present``), and an EMPTY root is
    exactly what a dropped NAS/SMB mount leaves behind — so a fixture that wants
    the creation to happen has to look like a library that is really there.
    """
    (music / "An Artist").mkdir(exist_ok=True)


def _aliased_root(tmp_path: Path) -> tuple[Path, Path]:
    """``(the music_dir beets is given, the other spelling of it)``.

    Two settings naming one root through two spellings is a configuration this
    app invites — ``fsutil.ROOT_FLAGS`` supports a symlinked ``directory:`` —
    and nothing warns the operator that the spellings have to match.
    """
    music = tmp_path / "music"
    music.mkdir()
    _library_root_with(music)
    os.symlink(music, tmp_path / "srv-music")
    return music, tmp_path / "srv-music"


def _swap_for_a_link(component: Path, target: Path) -> None:
    """Replace an existing directory with a symlink to ``target``."""
    os.rename(component, component.parent / f"real-{component.name}")
    os.symlink(target, component)


def test_a_trash_spelled_through_an_alias_of_the_music_root_is_anchored_too(
    tmp_path: Path,
) -> None:
    """H-2: a spelling not lexically below the root that resolves inside it.

    Measured 2026-09-12 (security seat H-2, code seat W1) on the arm this
    replaces: ``relative_to`` raised, so the anchored walk was skipped and
    ``mkdir(parents=True)`` followed the attacker's link at ``a`` — round-2 M-3
    in full, with no race and every later request agreeing with the relocation.
    """
    music, alias = _aliased_root(tmp_path)
    (music / "a").mkdir()
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    _swap_for_a_link(music / "a", elsewhere)

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, alias / "a" / "b" / ".trash")

    assert "not reachable below the music library" in str(caught.value)
    assert list(elsewhere.iterdir()) == [], "nothing created outside the library"


def test_the_alias_spelling_is_created_when_nothing_is_in_the_way(tmp_path: Path) -> None:
    """The control for the test above: the same spelling, no link planted.

    The refusal has to be about the component and not about the alias, or
    anchoring the alias spelling would refuse the layout itself.
    """
    music, alias = _aliased_root(tmp_path)

    trees, trash_dir = _trees(tmp_path, music, alias / "a" / "b" / ".trash")

    assert (music / "a" / "b" / ".trash").is_dir()
    st = os.stat(trash_dir)
    assert trees.trash == (st.st_dev, st.st_ino)


def test_a_symlinked_music_root_spelled_through_its_target_is_anchored_too(
    tmp_path: Path,
) -> None:
    """The converse alias: ``directory:`` is the link, the Trash names the target.

    beets hands ``directory:`` back normpath'd but NOT resolved
    (``library._music_dir``), so the pair is incomparable lexically in this
    direction too (code seat W1, probe A).
    """
    real = tmp_path / "library"
    real.mkdir()
    _library_root_with(real)
    music = tmp_path / "music"
    os.symlink(real, music)
    (real / "a").mkdir()
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    _swap_for_a_link(real / "a", elsewhere)

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, real / "a" / "b" / ".trash")

    assert "not reachable below the music library" in str(caught.value)
    assert list(elsewhere.iterdir()) == []


def test_a_symlinked_music_root_is_created_through_when_nothing_is_in_the_way(
    tmp_path: Path,
) -> None:
    """The control for the converse alias."""
    real = tmp_path / "library"
    real.mkdir()
    _library_root_with(real)
    music = tmp_path / "music"
    os.symlink(real, music)

    trees, trash_dir = _trees(tmp_path, music, real / "a" / ".trash")

    assert (real / "a" / ".trash").is_dir()
    st = os.stat(trash_dir)
    assert trees.trash == (st.st_dev, st.st_ino)


def test_a_trash_that_reaches_into_the_library_without_naming_it_is_refused(
    tmp_path: Path,
) -> None:
    """A link the operator owns that lands BELOW the music root.

    The walk never meets the root's identity, so nothing would anchor the parts
    it then creates inside the library. One spelling is supported, and the
    refusal names it.

    This arm plants only the OPERATOR's half, so it exercises the walk with
    nothing below the link — which is why it passed through two review rounds
    while the attacker's half escaped (security seat H-1). The two tests below
    plant that half.
    """
    music = tmp_path / "music"
    music.mkdir()
    _library_root_with(music)
    (music / "a").mkdir()
    os.symlink(music / "a", tmp_path / "srv-x")

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, tmp_path / "srv-x" / ".trash")

    assert "reaches into the music library without naming it" in str(caught.value)
    assert "`directory:`" in str(caught.value)
    assert list((music / "a").iterdir()) == [], "refused before anything was created"


def _jump_in_link(tmp_path: Path, music: Path) -> Path:
    """The operator's half of the jump-in shape: ``<tmp>/srv-x -> <M>/a``.

    Only the operator can make this one — a link ABOVE the music root is outside
    the attacker's reach — and it is the configuration the refusal above exists
    for. The tests below add the attacker's half, which is a link INSIDE the
    library, where the owner's layout ruling leaves write access.
    """
    _library_root_with(music)
    (music / "a").mkdir()
    os.symlink(music / "a", tmp_path / "srv-x")
    return tmp_path / "srv-x"


def test_an_attackers_link_at_the_leaf_of_a_jump_in_spelling_is_refused(
    tmp_path: Path,
) -> None:
    """H-1: one link below the operator's jump-in point moved the walk out first.

    Measured 2026-09-12 (security seat H-1, J5) on the arm this replaces: the
    jump-in question was asked ONCE, about the descriptor the walk ended on, and
    every part above the root was opened following links — so the attacker's
    ``.trash`` link took the walk outside the library and the climb then answered
    "not inside" correctly. Both requests were ACCEPTED, the movers wrote to the
    attacker's directory, and ``empty_all`` enumerated it.
    """
    music = tmp_path / "music"
    music.mkdir()
    jump_in = _jump_in_link(tmp_path, music)
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    os.symlink(elsewhere, music / "a" / ".trash")

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, jump_in / ".trash")

    assert "reaches into the music library without naming it" in str(caught.value)
    assert list(elsewhere.iterdir()) == [], "nothing created outside the library"


def test_an_attackers_link_mid_chain_of_a_jump_in_spelling_is_refused(
    tmp_path: Path,
) -> None:
    """The same escape one component deeper (security seat H-1, J1/J4).

    The refusal is about the component the walk is standing on, so it fires at
    the operator's own link — before the attacker's part is opened at all.
    """
    music = tmp_path / "music"
    music.mkdir()
    jump_in = _jump_in_link(tmp_path, music)
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    os.symlink(elsewhere, music / "a" / "b")

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, jump_in / "b" / ".trash")

    assert "reaches into the music library without naming it" in str(caught.value)
    assert list(elsewhere.iterdir()) == [], "nothing created outside the library"


def test_an_attackers_link_at_the_operators_own_target_is_refused(tmp_path: Path) -> None:
    """H-1': the operator's link points at a path the ATTACKER owns.

    The escape needs no race and nothing below the jump-in point: the operator
    spells the Trash through ``<tmp>/srv-x -> <M>/a`` and the attacker makes
    ``<M>/a`` a link of their own, so the kernel resolved both hops at once and
    handed the walk a descriptor that was never inside the library. Measured
    2026-09-12 (security seat H-1', A3/A4) on the arm this replaces: both
    requests ACCEPTED, the Trash at the attacker's target, two origin records
    written promising the moves were recoverable, and ``empty_all`` then
    ``rmtree``d whatever lived there. The walk now reads the operator's link
    itself, so the attacker's link is met BELOW the music root, where a link is
    refused rather than followed.
    """
    music = tmp_path / "music"
    music.mkdir()
    jump_in = _jump_in_link(tmp_path, music)
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    (elsewhere / "precious").mkdir()
    (music / "a").rmdir()
    os.symlink(elsewhere, music / "a")

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, jump_in / ".trash")

    assert "not reachable below the music library" in str(caught.value)
    assert sorted(p.name for p in elsewhere.iterdir()) == ["precious"], "nothing created outside"


def test_an_attackers_link_deeper_in_the_operators_target_is_refused(tmp_path: Path) -> None:
    """The same shape one component deeper, which the destination alone misses.

    ``<tmp>/srv-x -> <M>/a/b`` with ``a`` a real directory and ``b`` the
    attacker's link: the refusal has to come from walking the TARGET part by
    part (security seat H-1', A5 — the cheapest form, since ``b`` need only be
    created where the operator's spelling names something absent, not swapped).
    """
    music = tmp_path / "music"
    music.mkdir()
    _library_root_with(music)
    (music / "a").mkdir()
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    os.symlink(elsewhere, music / "a" / "b")
    os.symlink(music / "a" / "b", tmp_path / "srv-x")

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, tmp_path / "srv-x" / ".trash")

    assert "not reachable below the music library" in str(caught.value)
    assert list(elsewhere.iterdir()) == [], "nothing created outside the library"


def test_the_refusal_names_the_link_and_the_target_it_reaches_through(tmp_path: Path) -> None:
    """Which component the operator has to re-point, and where it goes.

    The spelled path alone does not say it: the link can be any part of the
    chain and its target is not in the spelling at all.
    """
    music = tmp_path / "music"
    music.mkdir()
    jump_in = _jump_in_link(tmp_path, music)

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, jump_in / ".trash")

    assert f"('srv-x' -> '{_elided(str(music / 'a'))}')" in str(caught.value)


def test_the_unreachable_refusal_names_the_link_it_was_reached_through(
    tmp_path: Path,
) -> None:
    """A3/A4: the component in the way is the ATTACKER's and is not in the spelling.

    The operator's own link is the only part of this chain they can re-point, so
    the refusal names it. Without the cause this sentence named the spelled path
    and a remedy ("bind mounts") for a fault that is not the operator's
    (code seat S4, measured 2026-09-13).
    """
    music = tmp_path / "music"
    music.mkdir()
    jump_in = _jump_in_link(tmp_path, music)
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    (music / "a").rmdir()
    os.symlink(elsewhere, music / "a")

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, jump_in / ".trash")

    detail = str(caught.value)
    assert "not reachable below the music library" in detail
    assert f"reached through 'srv-x' -> '{_elided(str(music / 'a'))}'" in detail


def test_a_link_to_a_link_outside_the_library_is_followed(tmp_path: Path) -> None:
    """Why the rule is about the TARGET and not about links as such.

    ``/home -> /var/home`` is the default on Fedora's ostree variants and the
    shipped Trash is ``<beets_dir>/trash``, so refusing a link anywhere in the
    path would refuse the DEFAULT layout there (owner ruling 2026-09-13). Two
    hops, the first of them RELATIVE, because each hop is resolved from the
    directory holding the link and not from the one the walk started in.
    """
    music = tmp_path / "music"
    music.mkdir()
    other_disk = tmp_path / "other-disk"
    (other_disk / "data").mkdir(parents=True)
    os.symlink("mid", tmp_path / "srv-x")
    os.symlink(other_disk, tmp_path / "mid")

    trees, trash_dir = _trees(tmp_path, music, tmp_path / "srv-x" / "data" / "trash")

    assert (other_disk / "data" / "trash").is_dir(), "created through the operator's own links"
    assert trees.trash == _ident_of(trash_dir)


def test_a_link_to_a_link_into_the_library_is_refused_by_the_inner_link(
    tmp_path: Path,
) -> None:
    """The control for the pair above: the second hop is what lands inside.

    The cause names the link that reaches in rather than the one the spelling
    holds, because that is the one whose target has to change.
    """
    music = tmp_path / "music"
    music.mkdir()
    _library_root_with(music)
    (music / "a").mkdir()
    os.symlink("mid", tmp_path / "srv-x")
    os.symlink(music / "a", tmp_path / "mid")

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, tmp_path / "srv-x" / ".trash")

    assert "reaches into the music library without naming it" in str(caught.value)
    assert f"('mid' -> '{_elided(str(music / 'a'))}')" in str(caught.value)


def test_a_relative_link_target_that_climbs_is_walked_as_the_kernel_would(
    tmp_path: Path,
) -> None:
    """A ``..`` in a link TARGET, which is not the ``..`` the setting refuses.

    The configured value may not hold one — it is collapsed lexically while the
    kernel does not collapse it — but a target is the operator's own filesystem,
    and a relative ``..`` target is a shape distributions ship. ``openat(fd,
    "..")`` is the kernel's own answer for a descriptor the walk already holds,
    so the walk asks it rather than refusing the layout.
    """
    music = tmp_path / "music"
    music.mkdir()
    other_disk = tmp_path / "other-disk"
    other_disk.mkdir()
    (tmp_path / "hold").mkdir()
    os.symlink("../other-disk", tmp_path / "hold" / "mounted")

    trees, trash_dir = _trees(tmp_path, music, tmp_path / "hold" / "mounted" / "trash")

    assert (other_disk / "trash").is_dir(), "created through the operator's own link"
    assert trees.trash == _ident_of(trash_dir)


def test_a_link_target_that_climbs_back_out_of_the_library_is_refused(
    tmp_path: Path,
) -> None:
    """A ``..`` hop that LEAVES the library is refused at the hop.

    A target that dips into the library, climbs out with ``..`` and then crosses
    a link OUTSIDE it ends outside — and the directory it climbed into is the
    library root's parent, which the layout rule does not treat as the
    operator's. At this branch's own feature commit it was refused already, but
    by a stale ``below`` and with the wrong sentence ("not reachable below the
    music library" about a component that is not below it); round 1 turned that
    into an accept (security seat L-1 probe p1, then L-1'), and the owner's
    criterion for this branch closed it fail-closed. It is NOT refused before
    the branch: re-measured 2026-09-13 at ``v0.51.6``, the release ``main``
    was at when the branch started, this shape was ACCEPTED — which is why the
    decision note in ``BACKLOG.md`` counts it as part of the upgrade cost.
    """
    music = tmp_path / "music"
    music.mkdir()
    _library_root_with(music)
    other_disk = tmp_path / "other-disk"
    other_disk.mkdir()
    (tmp_path / "hold").mkdir()
    os.symlink(other_disk, tmp_path / "hold" / "mounted")
    os.symlink(f"{music}/../hold/mounted", tmp_path / "srv-x")

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, tmp_path / "srv-x" / "trash")

    detail = str(caught.value)
    assert "climbs out of the music library with '..'" in detail
    assert f"('srv-x' -> '{_elided(f'{music}/../hold/mounted')}')" in detail
    assert list(other_disk.iterdir()) == [], "nothing created at the link's target"


def test_a_link_target_climbing_out_to_the_roots_parent_cannot_be_diverted_there(
    tmp_path: Path,
) -> None:
    """Why the refusal above is the safer answer: the parent may be hostile.

    The deployment is the one the layout rule is written for with ``directory:``
    a SUBfolder of a writable share — ``directory: <share>/library``, the share
    itself attacker-writable. The operator's own target climbs out of the library
    with ``../..``, and the link waiting there is the attacker's: at the round-1
    tip the Trash was created at its target beside the attacker's own file, with
    ``require_library_present`` skipped (security seat L-1', probe d1, measured
    2026-09-13; refused at the base by a stale ``below``).
    """
    share = tmp_path / "share"
    music = share / "library"
    music.mkdir(parents=True)
    _library_root_with(music)
    attacker_dest = tmp_path / "attacker-dest"
    attacker_dest.mkdir()
    (attacker_dest / "precious.txt").write_text("not the Trash's to share")
    os.symlink(attacker_dest, share / "hop")
    os.symlink(f"{music}/../../share/hop", tmp_path / "srv-x")

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, tmp_path / "srv-x" / "trash")

    assert "climbs out of the music library with '..'" in str(caught.value)
    assert [p.name for p in attacker_dest.iterdir()] == ["precious.txt"], "nothing created there"


def test_the_same_chain_without_the_dip_into_the_library_is_followed_too(
    tmp_path: Path,
) -> None:
    """The control for the pair: one of these two without the other proves nothing.

    Same two links, same destination, and the target does not pass through the
    library — accepted before the ``..`` hop was re-asked and after (security
    seat L-1, probe p1b).
    """
    music = tmp_path / "music"
    music.mkdir()
    _library_root_with(music)
    other_disk = tmp_path / "other-disk"
    other_disk.mkdir()
    (tmp_path / "hold").mkdir()
    os.symlink(other_disk, tmp_path / "hold" / "mounted")
    os.symlink(f"{tmp_path}/hold/mounted", tmp_path / "srv-x")

    trees, trash_dir = _trees(tmp_path, music, tmp_path / "srv-x" / "trash")

    assert (other_disk / "trash").is_dir()
    assert trees.trash == _ident_of(trash_dir)


def test_a_link_target_that_climbs_back_to_the_root_is_still_the_root(
    tmp_path: Path,
) -> None:
    """The dangerous direction of the same change: ``<M>/a/..`` IS the music root.

    A re-ask that answered "outside" here would unanchor the alias spelling
    reached by a climb — the Trash created inside the library with every later
    component followed (security seat L-1, probe p2).
    """
    music = tmp_path / "music"
    music.mkdir()
    _library_root_with(music)
    (music / "a").mkdir()
    os.symlink(f"{music}/a/..", tmp_path / "srv-x")

    trees, trash_dir = _trees(tmp_path, music, tmp_path / "srv-x" / ".trash")

    assert trash_dir == music / ".trash"
    assert trees.trash == _ident_of(trash_dir)


def test_an_attackers_link_below_a_root_reached_by_climbing_is_refused(
    tmp_path: Path,
) -> None:
    """What the anchor is FOR: the component after the climb is inside the library.

    ``<T>/srv-x -> <M>/a/..`` lands the walk on the root, so the attacker's link
    at ``<M>/b`` is met below it — where a link is refused rather than resolved.
    The pair above and this one are what a re-ask after ``..`` may not lose.
    """
    music = tmp_path / "music"
    music.mkdir()
    _library_root_with(music)
    (music / "a").mkdir()
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    os.symlink(elsewhere, music / "b")
    os.symlink(f"{music}/a/..", tmp_path / "srv-x")

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, tmp_path / "srv-x" / "b" / ".trash")

    assert "not reachable below the music library" in str(caught.value)
    assert list(elsewhere.iterdir()) == [], "nothing created outside the library"


def test_a_target_that_climbs_only_back_into_the_library_is_refused_at_the_hop(
    tmp_path: Path,
) -> None:
    """The other half of the re-ask: ``<M>/a/b/..`` lands INSIDE the library.

    The hop is where the answer changes, so the hop is where the refusal comes
    from — and it names the link the same way the destination check does, or the
    operator is told a path reaches in without being told through what.
    """
    music = tmp_path / "music"
    music.mkdir()
    _library_root_with(music)
    (music / "a" / "b").mkdir(parents=True)
    os.symlink(f"{music}/a/b/..", tmp_path / "srv-x")

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, tmp_path / "srv-x" / ".trash")

    detail = str(caught.value)
    assert "reaches into the music library without naming it" in detail
    assert f"('srv-x' -> '{_elided(f'{music}/a/b/..')}')" in detail
    assert sorted(p.name for p in (music / "a").iterdir()) == ["b"], "nothing created inside"


def test_a_long_link_target_is_elided_in_the_middle_of_the_cause(tmp_path: Path) -> None:
    """The cause names the link and its target, and the message stays readable.

    Measured 2026-09-13 (security seat L-2): both halves printed whole made a
    1244-character 503 for a 900-byte target, worst case about twice
    ``PATH_MAX``. The head names the disk and the tail the folder, so the MIDDLE
    is what goes.
    """
    music = tmp_path / "music"
    music.mkdir()
    _library_root_with(music)
    deep = music
    while len(str(deep)) < 900:
        deep = deep / ("d" * 60)
    deep.mkdir(parents=True)
    os.symlink(deep, tmp_path / "srv-x")

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, tmp_path / "srv-x" / ".trash")

    detail = str(caught.value)
    assert "reaches into the music library without naming it" in detail
    assert str(deep) not in detail, "the target is not printed whole"
    assert str(deep)[:50] in detail, "the head, which names the disk, is kept"
    assert str(deep)[-50:] in detail, "the tail, which names the folder, is kept"
    # Relative to the SPELLED path, which is printed whole and is as long as the
    # machine's ``TMPDIR`` makes it: an absolute bound passes or fails on that
    # and not on the elision (security seat L-2').
    #
    # 300 and not 400, because this is the only line left that can see
    # ``_CAUSE_HALF_MAX`` move. The four assertions above compute from the
    # INPUT, deliberately, which is what kills a broken elision; the ones that
    # compute THROUGH ``_elided(...)`` and so follow the constant by
    # construction are the cause assertions in the neighbouring tests.
    #
    # Written as the DELTA, not as two absolute pairs. This comment used to
    # quote "detail 395 vs a 429 bound" and those figures came from a
    # standalone ``mkdtemp`` probe (spelled path 29 chars), not from this
    # test's ``tmp_path`` (78): re-measured 2026-09-14 through the test itself,
    # the pair is 444 vs 478. The delta is what generalises and it is
    # ``TMPDIR``-independent — the detail runs ``spelled + 246`` at the shipped
    # 120 and ``spelled + 366`` doubled, so +400 lets the doubled constant
    # through at every length, +300 fails it at every length, and 120 keeps 54
    # characters of slack.
    assert len(detail) < len(str(tmp_path / "srv-x" / ".trash")) + 300, detail


def test_an_empty_music_root_still_refuses_a_link_that_reaches_into_it(
    tmp_path: Path,
) -> None:
    """The link rule needs the root's IDENTITY, not its contents.

    Both arms of it are gated on ``root_ident``, which is None only for a music
    root that neither opens nor stats (:class:`_Chain`'s docstring states it). A
    root that IS there but empty — the dropped share an operator actually hits —
    still has an identity, so the reach-in link is refused rather than followed
    (security seat L-4, probe n3c).
    """
    music = tmp_path / "music"
    music.mkdir()
    (music / "a").mkdir()
    os.symlink(music / "a", tmp_path / "srv-x")

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, tmp_path / "srv-x" / ".trash")

    assert "reaches into the music library without naming it" in str(caught.value)
    assert list((music / "a").iterdir()) == [], "refused before anything was created"


def test_a_dangling_operator_link_creates_nothing_where_it_pointed(tmp_path: Path) -> None:
    """The missing part is inside the link's TARGET, not in the spelling.

    The walk now stands in the target's own directory when the ENOENT arrives, so
    the arm that reads ENOENT as "the rest has to be created" must see the prefix
    that really exists and not the one the failed hop reached: otherwise the
    create loop makes the LINK's name inside the target's parent. Refused the way
    the kernel's own ENOENT was before the walk resolved anything itself.
    """
    music = tmp_path / "music"
    music.mkdir()
    holder = tmp_path / "other-disk"
    holder.mkdir()
    os.symlink(holder / "missing", tmp_path / "srv-x")

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, tmp_path / "srv-x" / "trash")

    assert "could not be created" in str(caught.value)
    assert "No such file or directory" in str(caught.value)
    assert list(holder.iterdir()) == [], "nothing created where the link pointed"


def test_forty_links_resolve_and_a_forty_first_is_refused(tmp_path: Path) -> None:
    """The budget is the kernel's own, so no layout that worked loses its Trash.

    40 and 41 as literals: a boundary read off the constant it is meant to pin
    would hold at any value.
    """
    music = tmp_path / "music"
    music.mkdir()
    (tmp_path / "end").mkdir()
    os.symlink(tmp_path / "end", tmp_path / "l0")
    for index in range(1, 41):
        os.symlink(tmp_path / f"l{index - 1}", tmp_path / f"l{index}")

    trees, trash_dir = _trees(tmp_path, music, tmp_path / "l39" / "trash")  # 40 hops
    assert trees.trash == _ident_of(trash_dir), "the kernel resolves 40 links, and so does this"

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, tmp_path / "l40" / "trash")  # 41

    assert "Too many levels of symbolic links" in str(caught.value)


@pytest.mark.skipif(os.getuid() == 0, reason="root searches an unsearchable directory anyway")
def test_a_chain_the_walk_cannot_climb_is_refused_rather_than_read_as_outside(
    tmp_path: Path,
) -> None:
    """L-1: the climb used to answer "no" when it could not answer at all.

    "No" is the arm that anchors NOTHING, so an unanswerable question decided the
    Trash was outside the library and the identity was taken anyway. Reachable at
    the deepest component the walk OPENED, a middle one included (measured
    2026-09-13, code seat W-2): mode ``0o400`` is readable, so the walk opens it,
    and not searchable, so the ``..`` climb out of it answers EACCES (measured
    2026-09-12, with ``ROOT_FLAGS`` and with the ``O_PATH`` climb flags). The
    Trash ITSELF is that component here, so nothing is left to create and the
    fail-open arm ACCEPTED it — which is the difference this test pins. No loss was
    reachable through it (a mover needs write and search there too); the
    direction was the unsafe one (security seat L-1).
    """
    music = tmp_path / "music"
    music.mkdir()
    _library_root_with(music)
    unsearchable = tmp_path / "srv-trash"
    unsearchable.mkdir()
    os.chmod(unsearchable, 0o400)

    try:
        with pytest.raises(StoreLayoutError) as caught:
            _trees(tmp_path, music, unsearchable)
    finally:
        os.chmod(unsearchable, 0o700)  # or the tmp_path teardown cannot clean up

    assert "could not be checked against the music library" in str(caught.value)


@pytest.mark.skipif(os.getuid() == 0, reason="root opens an unreadable directory anyway")
def test_a_present_but_unreadable_music_root_still_anchors_the_walk(tmp_path: Path) -> None:
    """W2: ``open_root`` needs read, and the identity only needs a stat.

    Mode ``0o111`` — traversable, not readable — is the wrong-``PUID``/``PGID``
    shape the README already names for the origin store. Measured 2026-09-12
    (code seat W2) on the arm this replaces: EACCES meant no identity, so
    ``below`` never became true, the climb was skipped, and the jump-in spelling
    was ACCEPTED and created inside the library where the ``0o755`` control
    refuses it.
    """
    music = tmp_path / "music"
    music.mkdir()
    jump_in = _jump_in_link(tmp_path, music)
    os.chmod(music, 0o111)

    try:
        with pytest.raises(StoreLayoutError) as caught:
            _trees(tmp_path, music, jump_in / ".trash")
    finally:
        os.chmod(music, 0o755)  # or the tmp_path teardown cannot clean up

    assert "reaches into the music library without naming it" in str(caught.value)
    assert list((music / "a").iterdir()) == [], "refused before anything was created"


@pytest.mark.skipif(os.getuid() == 0, reason="root reads an unreadable directory anyway")
def test_a_search_only_ancestor_above_the_trash_is_climbed_not_refused(tmp_path: Path) -> None:
    """The control for the refusal above: ``0o111`` is not "cannot be checked".

    Now that an unfinished climb REFUSES, what the climb needs decides which
    layouts are still supported. It opens each rung ``O_PATH``, which needs
    search alone — measured 2026-09-12: ``0o111`` (the wrong-``PUID``/``PGID``
    shape) answers EACCES to ``ROOT_FLAGS`` and climbs fine with ``O_PATH``.
    The dir has to be one the WALK does not open, so the Trash is spelled through
    a link the operator owns, which is the supported outside-the-library layout.
    """
    music = tmp_path / "music"
    music.mkdir()
    _library_root_with(music)
    outer = tmp_path / "outer"
    (outer / "deep").mkdir(parents=True)
    os.symlink(outer / "deep", tmp_path / "mounted")
    os.chmod(outer, 0o111)

    try:
        trees, trash_dir = _trees(tmp_path, music, tmp_path / "mounted" / "trash")
    finally:
        os.chmod(outer, 0o755)  # or the tmp_path teardown cannot clean up

    assert (outer / "deep" / "trash").is_dir(), "created through the operator's own link"
    assert trees.trash == _ident_of(trash_dir)


def test_a_trash_spelling_that_climbs_is_refused(tmp_path: Path) -> None:
    """A ``..`` in the configured value names one directory and reads as another.

    ``os.path.normpath`` collapses it lexically and the kernel does not, so a
    ``..`` that crosses a symlinked component diverges: measured 2026-09-12 (code
    seat, probe D) the round created a stray ``<M>/b/.trash`` and reported
    success while the configured Trash stayed absent.
    """
    music = tmp_path / "music"
    music.mkdir()
    _library_root_with(music)

    with pytest.raises(StoreLayoutError) as caught:
        _trees(tmp_path, music, music / "x" / ".." / ".trash")

    assert "may not contain '..'" in str(caught.value)
    assert sorted(p.name for p in music.iterdir()) == ["An Artist"]


def _library_on_a_dropped_share(tmp_path: Path) -> tuple[LibraryHandle, Path]:
    """A 3-row library whose share has just gone: mountpoint present, EMPTY.

    What a dropped NAS/SMB/NFS mount really leaves behind — the kernel keeps the
    mountpoint directory, so ``os.path.isdir`` stays true and every file the
    library names reads as deleted.
    """
    music = tmp_path / "music"
    album = music / "Artist" / "Album"
    album.mkdir(parents=True)
    handle = _handle(tmp_path, music)
    for n in (1, 2, 3):
        track = album / f"0{n} Track.mp3"
        track.write_bytes(b"\x00" * 64)
        handle.lib.add(
            Item(
                path=os.fsencode(str(track)),
                title=f"T{n}",
                artist="Artist",
                album="Album",
                mtime=1,
            )
        )
    shutil.rmtree(music / "Artist")
    return handle, music


def test_a_dropped_share_gets_no_trash_inside_the_music_root(tmp_path: Path) -> None:
    """H-1: the creation used to run ahead of every library-presence guard.

    ``require_library_root``'s second half exists because a dropped mount leaves
    the mountpoint present but EMPTY, and disk sync's per-item removal is gated
    on it — its own comment says "the loop would wipe thousands of DB rows in one
    pass". Measured 2026-09-12 (security seat H-1): with a Trash configured
    inside the library, one destructive request supplied that entry itself, the
    cheap guard passed from then on, and the next sweep dropped 3 of 3 rows.
    """
    handle, music = _library_on_a_dropped_share(tmp_path)
    settings = Settings(trash_dir=str(music / "a" / "b" / ".trash"))
    trash_dir = resolve_trash_dir(settings, handle)
    origins_dir = origins_for(trash_dir)

    with pytest.raises(LibraryRootUnavailableError) as caught:
        checked_protected_trees(settings, handle, trash_dir=trash_dir, origins_dir=origins_dir)

    assert "Is the music share mounted?" in str(caught.value)
    assert list(music.iterdir()) == [], "nothing created on the bare mountpoint"
    with pytest.raises(LibraryRootUnavailableError):
        run_disk_sync(handle.lib, on_total=_ignore, on_item=_ignore, should_stop=_never)
    assert len(list(handle.lib.items())) == 3, "rows before == rows after"


def test_a_stray_marker_on_the_mountpoint_does_not_buy_a_trash_inside(tmp_path: Path) -> None:
    """Which guard: the DB sample, not the cheap "the root has an entry" one.

    Measured 2026-09-12: with a ``.stfolder`` on the local mountpoint —
    Syncthing's marker, and the shape ``require_library_root``'s own docstring
    names as its accepted residual — the cheap guard PASSES while the DB sample
    still refuses. The cheap one is what disk sync runs per removal, so it may
    stay O(1); this creation runs once per destructive request and can afford the
    sample, and it is the creation that supplies the stray entry the cheap guard
    is then fooled by.
    """
    handle, music = _library_on_a_dropped_share(tmp_path)
    (music / ".stfolder").mkdir()
    require_library_root(handle.lib)  # the cheap guard is satisfied by the marker
    settings = Settings(trash_dir=str(music / "a" / ".trash"))
    trash_dir = resolve_trash_dir(settings, handle)
    origins_dir = origins_for(trash_dir)

    with pytest.raises(LibraryRootUnavailableError) as caught:
        checked_protected_trees(settings, handle, trash_dir=trash_dir, origins_dir=origins_dir)

    assert "none of the music files the library names are in it" in str(caught.value)
    assert [p.name for p in music.iterdir()] == [".stfolder"], "nothing created beside the marker"


def test_a_dropped_share_still_allows_the_trash_outside_the_library(tmp_path: Path) -> None:
    """The control: the shipped ``<beets_dir>/trash`` is not the library's to lose.

    Nothing is created inside the music root on this arm, so refusing it would
    503 Empty Trash for a fault it cannot reach — the Trash is on local disk and
    reclaiming its space is exactly what the operator wants while the share is
    down.
    """
    handle, music = _library_on_a_dropped_share(tmp_path)
    settings = Settings(trash_dir="")
    trash_dir = resolve_trash_dir(settings, handle)

    trees = checked_protected_trees(
        settings, handle, trash_dir=trash_dir, origins_dir=origins_for(trash_dir)
    )

    assert trash_dir.is_dir()
    assert trees.trash == _ident_of(trash_dir)
    assert list(music.iterdir()) == []


def test_the_identity_the_movers_get_is_the_one_the_walk_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-1: the identity is ``fstat`` on the walk's descriptor, not a second stat.

    Measured (security seat M-1): the by-name stat ran 18 µs after the creation
    and a real racer won that window 537 times in 100 876 requests; a won window
    put that request's files outside the library and pointed ``empty_all``'s
    ``rmtree`` at a directory of the attacker's choosing. Taking the identity
    from the descriptor the walk reached means the mover's own open has to land
    on it or be refused.
    """
    music = tmp_path / "music"
    music.mkdir()
    _library_root_with(music)
    (music / "a").mkdir()
    elsewhere = tmp_path / "somewhere-else"
    (elsewhere / "b" / ".trash").mkdir(parents=True)

    def swap_the_component_then_build(**kwargs: Any) -> ProtectedTrees:
        """The window, won: the swap lands after the creation, before the stat."""
        _swap_for_a_link(music / "a", elsewhere)
        return protected_trees(**kwargs)

    monkeypatch.setattr("app.beets.store_layout.protected_trees", swap_the_component_then_build)

    trees, trash_dir = _trees(tmp_path, music, music / "a" / "b" / ".trash")

    walked = os.stat(music / "real-a" / "b" / ".trash")
    assert trees.trash == (walked.st_dev, walked.st_ino), "the directory the walk created"
    with pytest.raises(ProtectedTreeError) as caught:
        open_checked_dir(trash_dir, trees)
    assert "is not the directory MusicDrop checked" in str(caught.value)
