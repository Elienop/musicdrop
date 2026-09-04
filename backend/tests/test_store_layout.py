"""The containment rule: where Trash and the Trash origin store may sit.

One test per refused relationship and one per allowed one, because the two
halves prove different things. The refusals are the guard; the allowances are
the controls that keep it from degenerating into "refuse everything" — and two
of them (a Trash inside the music library, a Trash under the beets dir) are
respectively the owner's ruling and the SHIPPED DEFAULT, so a guard that took
either would break every install.

Pure path arithmetic apart from ``test_a_symlinked_music_root_...``, which needs
real directories because the whole question there is what ``resolve()`` does.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.beets import store_layout
from app.beets.store_layout import (
    _FIX_TRASH,
    BEETS_SETTING,
    LIBRARY_SETTING,
    MUSIC_SETTING,
    ORIGINS_SETTING,
    TRASH_SETTING,
    StoreLayoutError,
    check_store_layout,
    resolve_configured_path,
)


def _check(
    *, music: Path, beets: Path, trash: Path, origins: Path, library: Path | None = None
) -> None:
    """``library`` defaults to where beets' own bundled default puts it.

    ``library: library.db`` (``beets/config_default.yaml:3``) is relative, so
    confuse joins it to ``BEETSDIR`` — the default database really does sit
    inside ``B``. Using that as the helper's default keeps every test that is
    about the four DIRECTORIES on a realistic ``L``, rather than on a path
    invented to be harmless.
    """
    check_store_layout(
        music_dir=music,
        beets_dir=beets,
        trash_dir=trash,
        origins_dir=origins,
        library_path=beets / "library.db" if library is None else library,
    )


# --------------------------------------------------------------------------
# REFUSED — one test per relationship in the rule table.
# --------------------------------------------------------------------------


def test_the_beets_dir_equal_to_the_music_library_is_refused(tmp_path: Path) -> None:
    """``directory: .`` — the beets data dir IS the library beets indexes.

    The data dir holds ``bank/``, ``plex/``, ``slskd/``, ``playlists/`` and
    ``inbox/``, none of which holds audio; as the music library it is also the
    tree the orphan sweep walks, which offered all five for trashing when the
    review round measured it.
    """
    both = tmp_path / "data"
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=both,
            beets=both,
            trash=both / "trash",
            origins=tmp_path / "records",
        )
    assert "The beets data directory is the music library" in str(exc.value)
    assert BEETS_SETTING in str(exc.value)


def test_a_beets_dir_inside_the_music_library_is_refused(tmp_path: Path) -> None:
    """``MUSICDROP_BEETS_DIR=/music/musicdrop`` — refused as of the review round.

    It used to be allowed with the origin store moved out. Two loose ends closed
    together by refusing it: the DEFAULT origin store is then inside the library
    and refused (so the layout secretly obliged a second env var), and
    ``library.db`` + ``config.yaml`` sit where a library-scope sweep and a
    whole-folder delete can move them — measured on ``d65e635``, where a
    plain-named beets dir inside the library was reported by the sweep.
    """
    music = tmp_path / "music"
    beets = music / "musicdrop"
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=music,
            beets=beets,
            trash=beets / "trash",
            origins=tmp_path / "records",
        )
    assert "The music library contains the beets data directory" in str(exc.value)
    assert str(music) in str(exc.value)
    assert str(beets) in str(exc.value)


def test_a_music_library_inside_the_beets_dir_is_refused(tmp_path: Path) -> None:
    """The other direction, and the one that costs the sweep its exclusions.

    Every app-owned exclusion (``ignore_dirs``, the origin store, the export
    dir) is then an ancestor of the walk root, and ``orphans._exclude_ids``
    drops such a root with one WARNING. Measured on this tree with the three
    app-owned roots passed as ``ignore_dirs``: the sweep reported the husk and
    logged one warning per root, so the layout costs the sweep every app-owned
    exclusion rather than the whole library. The refusal is the same either way;
    what changed is the loss it names.
    """
    beets = tmp_path / "data"
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=beets / "music",
            beets=beets,
            trash=beets / "trash",
            origins=beets / "trash-origins",
        )
    assert "The beets data directory contains the music library" in str(exc.value)


def test_the_database_inside_the_trash_is_refused(tmp_path: Path) -> None:
    """``library:`` is its own key: the DB can move while B, T and O stay disjoint.

    Measured in the review round: with ``library:`` under the Trash dir the
    layout passed and Empty Trash removed ``library.db``.
    """
    beets = tmp_path / "data"
    trash = beets / "trash"
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=tmp_path / "music",
            beets=beets,
            trash=trash,
            origins=beets / "trash-origins",
            library=trash / "library.db",
        )
    assert "The Trash directory contains the beets database" in str(exc.value)
    assert LIBRARY_SETTING in str(exc.value)


def test_the_database_inside_the_origin_store_is_refused(tmp_path: Path) -> None:
    """The store's own sweep unlinks ``*.json`` only, so this one is not a delete.

    It is refused because the store has to be a directory only the app's records
    live in — and the message says exactly that rather than claiming a loss the
    sweep does not cause.
    """
    beets = tmp_path / "data"
    origins = beets / "trash-origins"
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=tmp_path / "music",
            beets=beets,
            trash=beets / "trash",
            origins=origins,
            library=origins / "library.db",
        )
    message = str(exc.value)
    assert "The Trash origin store contains the beets database" in message
    assert "library.db is not a *.json, so that sweep leaves it" in message


def test_trash_equal_to_the_music_dir_is_refused(tmp_path: Path) -> None:
    """The typo the whole slice exists for: ``MUSICDROP_TRASH_DIR=/music``.

    ``trash_manage.empty_all`` rmtrees every child of what ``resolve_trash_dir``
    returns, so Empty Trash on this layout removes every artist folder.
    """
    music = tmp_path / "music"
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=music,
            beets=tmp_path / "data",
            trash=music,
            origins=tmp_path / "data" / "trash-origins",
        )
    assert "The Trash directory is the music library" in str(exc.value)


def test_trash_containing_the_music_dir_is_refused(tmp_path: Path) -> None:
    """One level worse than the case above: ``/music`` under ``MUSICDROP_TRASH_DIR=/``
    parent — Empty Trash removes the music dir itself, not just its contents."""
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=tmp_path / "vol" / "music",
            beets=tmp_path / "data",
            trash=tmp_path / "vol",
            origins=tmp_path / "data" / "trash-origins",
        )
    assert "The Trash directory contains the music library" in str(exc.value)


def test_trash_equal_to_the_beets_dir_is_refused(tmp_path: Path) -> None:
    beets = tmp_path / "data"
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=tmp_path / "music",
            beets=beets,
            trash=beets,
            origins=tmp_path / "records",
        )
    assert "The Trash directory is the beets data directory" in str(exc.value)
    assert "library.db, config.yaml" in str(exc.value)


def test_trash_containing_the_beets_dir_is_refused(tmp_path: Path) -> None:
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=tmp_path / "music",
            beets=tmp_path / "vol" / "data",
            trash=tmp_path / "vol",
            origins=tmp_path / "records",
        )
    assert "The Trash directory contains the beets data directory" in str(exc.value)


def test_origin_store_equal_to_the_music_dir_is_refused(tmp_path: Path) -> None:
    """``clear_trash_origins`` unlinks every ``*.json`` directly in the store."""
    music = tmp_path / "music"
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=music,
            beets=tmp_path / "data",
            trash=tmp_path / "data" / "trash",
            origins=music,
        )
    assert "The Trash origin store is the music library" in str(exc.value)


def test_origin_store_containing_the_music_dir_is_refused(tmp_path: Path) -> None:
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=tmp_path / "vol" / "music",
            beets=tmp_path / "data",
            trash=tmp_path / "data" / "trash",
            origins=tmp_path / "vol",
        )
    assert "The Trash origin store contains the music library" in str(exc.value)


def test_origin_store_inside_the_music_dir_is_refused(tmp_path: Path) -> None:
    """The direction Trash is ALLOWED in and the store is not.

    ``app/config.py`` has said "one NOT under the music library" since the store
    was split out; this is what makes that sentence enforced rather than
    advisory. A whole-folder delete of any folder above the store takes the
    records into Trash with it.
    """
    music = tmp_path / "music"
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=music,
            beets=tmp_path / "data",
            trash=music / ".trash",
            origins=music / ".trash-origins",
        )
    assert "The music library contains the Trash origin store" in str(exc.value)


def test_origin_store_equal_to_the_beets_dir_is_refused(tmp_path: Path) -> None:
    beets = tmp_path / "data"
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=tmp_path / "music",
            beets=beets,
            trash=beets / "trash",
            origins=beets,
        )
    assert "The Trash origin store is the beets data directory" in str(exc.value)


def test_origin_store_containing_the_beets_dir_is_refused(tmp_path: Path) -> None:
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=tmp_path / "music",
            beets=tmp_path / "vol" / "data",
            trash=tmp_path / "vol" / "data" / "trash",
            origins=tmp_path / "vol",
        )
    assert "The Trash origin store contains the beets data directory" in str(exc.value)


def test_origin_store_equal_to_trash_is_refused(tmp_path: Path) -> None:
    """Records in the entry namespace: each one would list as a trashed album."""
    trash = tmp_path / "data" / "trash"
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=tmp_path / "music",
            beets=tmp_path / "data",
            trash=trash,
            origins=trash,
        )
    assert "The Trash directory is the Trash origin store" in str(exc.value)


def test_origin_store_inside_trash_is_refused(tmp_path: Path) -> None:
    trash = tmp_path / "data" / "trash"
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=tmp_path / "music",
            beets=tmp_path / "data",
            trash=trash,
            origins=trash / "origins",
        )
    assert "The Trash directory contains the Trash origin store" in str(exc.value)


def test_trash_inside_the_origin_store_is_refused(tmp_path: Path) -> None:
    origins = tmp_path / "data" / "records"
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=tmp_path / "music",
            beets=tmp_path / "data",
            trash=origins / "trash",
            origins=origins,
        )
    assert "The Trash origin store contains the Trash directory" in str(exc.value)


# --------------------------------------------------------------------------
# ALLOWED — the controls. Each is a layout somebody runs.
# --------------------------------------------------------------------------


def test_trash_strictly_inside_the_music_dir_is_allowed(tmp_path: Path) -> None:
    """The owner's ruling, 2026-09-04: a Trash INSIDE the library is fine.

    It is the reason to set the var at all — ``<music>/.trash`` makes a delete a
    same-disk rename instead of a cross-device copy.
    """
    music = tmp_path / "music"
    _check(
        music=music,
        beets=tmp_path / "data",
        trash=music / ".trash",
        origins=tmp_path / "data" / "trash-origins",
    )


def test_the_shipped_default_layout_is_allowed(tmp_path: Path) -> None:
    """``<B>/trash`` + ``<B>/trash-origins``, siblings under the beets dir.

    Both defaults at once, because a guard that refused this would fail every
    install on the next restart — and the two names are one string-prefix bug
    apart (``/data/trash`` vs ``/data/trash-origins``).
    """
    beets = tmp_path / "data"
    _check(
        music=tmp_path / "music",
        beets=beets,
        trash=beets / "trash",
        origins=beets / "trash-origins",
    )


def test_disjoint_directories_are_allowed(tmp_path: Path) -> None:
    """Four unrelated trees — the layout the shipped image has (/music, /data)."""
    _check(
        music=tmp_path / "music",
        beets=tmp_path / "data",
        trash=tmp_path / "bin",
        origins=tmp_path / "records",
    )


def test_the_default_database_beside_the_config_is_allowed(tmp_path: Path) -> None:
    """The control for the two ``library:`` rows below.

    beets' bundled ``library: library.db`` puts the database inside ``B``, which
    is also where the DEFAULT Trash and origin store sit — so a rule about ``L``
    that was one comparison too wide would refuse every shipped install.
    """
    beets = tmp_path / "data"
    _check(
        music=tmp_path / "music",
        beets=beets,
        trash=beets / "trash",
        origins=beets / "trash-origins",
        library=beets / "library.db",
    )


def test_two_names_for_one_file_are_one_path(tmp_path: Path) -> None:
    """``_same_path`` asks the FILESYSTEM, not the two strings.

    A hard link is the one path alias this test can build without privileges:
    two names, one inode, neither a symlink, so ``resolve()`` leaves both
    spellings exactly as they are. The bind-mount and case-folding aliases the
    review round measured are the same defect and the same fix; they need a
    mount, which a unit test does not have.
    """
    from app.beets.store_layout import _same_path

    one = tmp_path / "one.db"
    one.write_bytes(b"x")
    alias = tmp_path / "alias.db"
    os.link(one, alias)
    other = tmp_path / "other.db"
    other.write_bytes(b"x")

    assert _same_path(one, alias) is True
    assert _same_path(one, other) is False  # same bytes, different inode


def test_an_aliased_trash_dir_is_refused_even_though_the_strings_differ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bind-mount shape: two spellings, one directory, both existing.

    The alias is supplied through ``_stat_id`` — the module's one seam onto the
    filesystem — because building a real second name for a DIRECTORY needs a
    mount. What is pinned is that the check consults inode identity at all; that
    ``_stat_id`` reports the kernel's answer faithfully is one ``Path.stat()``
    call, and the hard-link test above measures that end for real.
    """
    music = tmp_path / "music"
    music.mkdir()
    alias = tmp_path / "alias"
    alias.mkdir()

    real_stat_id = store_layout._stat_id

    def fake(path: Path) -> tuple[int, int] | None:
        if path in (music, alias):
            return (1, 1)
        return real_stat_id(path)

    monkeypatch.setattr(store_layout, "_stat_id", fake)
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=music,
            beets=tmp_path / "data",
            trash=alias,
            origins=tmp_path / "records",
        )
    assert "The Trash directory is the music library" in str(exc.value)


def test_an_alias_of_the_librarys_parent_is_refused_as_containing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half an inode test on the two ENDPOINTS alone would miss.

    A bind mount of ``<M>``'s PARENT onto the Trash path leaves ``T`` and ``M``
    with different inodes — measured in the review round, where Empty Trash then
    removed the music dir AND the beets dir. Containment therefore walks ``M``'s
    ancestors and asks about each.
    """
    vol = tmp_path / "vol"
    music = vol / "music"
    music.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.mkdir()

    real_stat_id = store_layout._stat_id

    def fake(path: Path) -> tuple[int, int] | None:
        if path in (vol, alias):
            return (2, 2)
        return real_stat_id(path)

    monkeypatch.setattr(store_layout, "_stat_id", fake)
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=music,
            beets=tmp_path / "data",
            trash=alias,
            origins=tmp_path / "records",
        )
    assert "The Trash directory contains the music library" in str(exc.value)


def test_a_layout_whose_aliases_do_not_overlap_is_still_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the two above: the seam is consulted, not obeyed blindly.

    Same fake in place, reporting DIFFERENT ids for every path, so a check that
    had started refusing whenever it stats anything would fail here.
    """
    music = tmp_path / "music"
    music.mkdir()
    trash = tmp_path / "bin"
    trash.mkdir()

    ids = {music: (3, 3), trash: (3, 4)}
    monkeypatch.setattr(store_layout, "_stat_id", lambda p: ids.get(p))
    _check(
        music=music,
        beets=tmp_path / "data",
        trash=trash,
        origins=tmp_path / "records",
    )


# --------------------------------------------------------------------------
# Paths that will not resolve at all: a refusal, not a traceback.
# --------------------------------------------------------------------------


def test_a_symlink_loop_is_a_refusal_not_a_runtime_error(tmp_path: Path) -> None:
    """``Path.resolve()`` raises ``RuntimeError`` on a loop, on 3.11 and 3.12.

    Before this it escaped every ``except StoreLayoutError`` in the app: the
    lifespan printed a traceback with no "refusing to start" line, and
    Validate/Save answered 500. The message names the setting, because the
    traceback named only the path.
    """
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=tmp_path / "music",
            beets=tmp_path / "data",
            trash=loop,
            origins=tmp_path / "records",
        )
    message = str(exc.value)
    assert TRASH_SETTING in message
    assert "could not be resolved" in message
    assert str(loop) in message


def test_a_trash_path_that_cannot_be_stat_d_is_a_refusal_not_a_pass(tmp_path: Path) -> None:
    """A Trash path behind a directory this process cannot traverse.

    Non-strict ``Path.resolve()`` re-raises only ELOOP, so on EACCES it hands
    back the string it was given, ``_stat_id`` answers ``None``, and the
    comparison silently becomes a string comparison. Measured on this tree with
    the guard removed: ``MUSICDROP_TRASH_DIR`` set to a symlink to the music
    library inside a mode-000 directory was ALLOWED, and the same layout with
    that directory traversable — the control below — was refused. The four Trash
    routes then answered 500 (``PermissionError``) rather than the sentence.

    Skipped as root, which traverses a mode-000 directory regardless.
    """
    music = tmp_path / "music"
    music.mkdir()
    locked = tmp_path / "locked"
    locked.mkdir()
    alias = locked / "alias"
    alias.symlink_to(music)
    locked.chmod(0o000)
    try:
        if os.access(alias, os.F_OK):  # root, or an fs that ignores the mode
            pytest.skip("this process can traverse a mode-000 directory")
        with pytest.raises(StoreLayoutError) as exc:
            _check(
                music=music,
                beets=tmp_path / "data",
                trash=alias,
                origins=tmp_path / "records",
            )
        message = str(exc.value)
        assert TRASH_SETTING in message
        assert "could not be examined" in message
    finally:
        locked.chmod(0o700)

    # The control: the SAME layout, readable. It is refused for what it is.
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=music,
            beets=tmp_path / "data",
            trash=alias,
            origins=tmp_path / "records",
        )
    assert "The Trash directory is the music library" in str(exc.value)


def test_a_path_that_is_merely_absent_still_passes(tmp_path: Path) -> None:
    """The other side of the stat guard: none of the five has to exist yet.

    ENOENT and ENOTDIR are the allowed errnos — a first boot creates the Trash
    on demand, and the module docstring says so. A guard that refused every
    ``stat`` failure would refuse the shipped default before the first delete.
    """
    _check(
        music=tmp_path / "music",
        beets=tmp_path / "data",
        trash=tmp_path / "data" / "trash",
        origins=tmp_path / "data" / "trash-origins",
    )
    # ENOTDIR: a path whose PARENT is a regular file.
    notdir = tmp_path / "data" / "afile"
    notdir.parent.mkdir(parents=True, exist_ok=True)
    notdir.write_text("x", encoding="utf-8")
    _check(
        music=tmp_path / "music",
        beets=tmp_path / "data",
        trash=notdir / "trash",
        origins=tmp_path / "data" / "trash-origins",
    )


def test_an_embedded_nul_is_a_refusal_not_a_value_error(tmp_path: Path) -> None:
    """``directory: "/music/\\0evil"`` is a plain double-quoted YAML scalar ruamel
    accepts, and ``lstat`` raises ``ValueError`` on it.

    The sibling of the loop case and a different exception type, which is why
    both are pinned: one ``except`` that covered only ``RuntimeError`` would
    pass the test above and still 500 here.
    """
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=Path("/music/\0evil"),
            beets=tmp_path / "data",
            trash=tmp_path / "bin",
            origins=tmp_path / "records",
        )
    message = str(exc.value)
    assert MUSIC_SETTING in message
    assert "could not be resolved" in message


def test_a_symlinked_music_root_counts_as_inside(tmp_path: Path) -> None:
    """``/music -> /mnt/tank/music`` with Trash spelled on the REAL path.

    ``_music_dir`` normalises but does not resolve symlinks, so without
    resolving both sides these two compare as unrelated trees and the layout
    reads as "disjoint" instead of "Trash inside the library". Allowed either
    way — the point is that it is allowed for the RIGHT reason, which the
    refusing twin below is what proves.
    """
    real = tmp_path / "tank" / "music"
    real.mkdir(parents=True)
    (real / ".trash").mkdir()
    link = tmp_path / "music"
    link.symlink_to(real)

    _check(
        music=link,
        beets=tmp_path / "data",
        trash=real / ".trash",
        origins=tmp_path / "records",
    )


def test_a_symlinked_music_root_is_still_refused_when_trash_is_the_real_dir(
    tmp_path: Path,
) -> None:
    """The twin that makes the test above mean something.

    Same symlink, but Trash points at the REAL directory the link targets.
    Compared lexically the two strings are unrelated and this passes; compared
    resolved they are the same directory and Empty Trash would delete the
    library.
    """
    real = tmp_path / "tank" / "music"
    real.mkdir(parents=True)
    link = tmp_path / "music"
    link.symlink_to(real)

    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=link,
            beets=tmp_path / "data",
            trash=real,
            origins=tmp_path / "records",
        )
    assert "The Trash directory is the music library" in str(exc.value)


def test_a_dotdot_spelling_is_normalised_before_comparing(tmp_path: Path) -> None:
    """``is_relative_to`` is lexical, which ``resolve_trash_child`` documents too.

    ``<music>/../music`` is the music dir; without ``resolve()`` it is a
    different string and the equality check misses it.
    """
    music = tmp_path / "music"
    music.mkdir()
    with pytest.raises(StoreLayoutError):
        _check(
            music=music,
            beets=tmp_path / "data",
            trash=music / ".." / "music",
            origins=tmp_path / "records",
        )


# --------------------------------------------------------------------------
# The message, and how a candidate ``directory:`` is resolved.
# --------------------------------------------------------------------------


def test_the_message_names_the_setting_both_paths_the_loss_and_the_fix(
    tmp_path: Path,
) -> None:
    """Four things an operator reading ``docker logs`` needs, in one line.

    Asserted as four separate substrings rather than one golden string: the
    wording is meant to be edited, the four ingredients are not.

    The fix is a FIXED sentence per setting, not a computed spelling. The
    remedies used to offer example paths tested against the rule first, which
    was a second copy of the rule with its own unpinned guards; owner ruling
    2026-09-04 ("long paragraphs are just a waste of space") retired them.
    """
    music = tmp_path / "music"
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=music,
            beets=tmp_path / "data",
            trash=music,
            origins=tmp_path / "records",
        )
    message = str(exc.value)
    assert TRASH_SETTING in message  # which setting is wrong
    assert MUSIC_SETTING in message  # ...and what it was compared against
    assert str(music) in message  # both resolved paths, so no guessing
    assert "would delete the music library" in message  # what it would have cost
    assert message.endswith("Set MUSICDROP_TRASH_DIR to its own folder.")  # what to do


def test_the_origin_store_message_names_its_own_setting(tmp_path: Path) -> None:
    """The trash message must not be reused for the store: different var, different fix."""
    music = tmp_path / "music"
    with pytest.raises(StoreLayoutError) as exc:
        _check(
            music=music,
            beets=tmp_path / "data",
            trash=tmp_path / "data" / "trash",
            origins=music / "records",
        )
    message = str(exc.value)
    assert ORIGINS_SETTING in message
    assert message.endswith("Set MUSICDROP_TRASH_ORIGINS_DIR to its own folder.")
    # ...and NOT the Trash's fix, which the row above gets.
    assert _FIX_TRASH not in message


def test_a_relative_directory_resolves_against_the_beets_dir_not_the_cwd(
    tmp_path: Path,
) -> None:
    """confuse joins a relative ``directory:`` to ``config_dir()`` = ``BEETSDIR``.

    (``confuse/templates.py``, ``Filename.value``: expanduser, then — with the
    default ``in_source_dir=False`` and no ``base_for_paths`` on beets' source —
    ``os.path.join(view.root().config_dir(), path_str)``, then ``abspath``. The
    starter config says the same in its own words: "Paths are relative to this
    file's directory", shipping ``directory: ../music``.)

    Resolving against the CWD instead would compare the WRONG directory against
    Trash, which is the whole reason this helper is not ``Path(raw).resolve()``.
    """
    beets = tmp_path / "data" / "beets"
    beets.mkdir(parents=True)
    assert resolve_configured_path("../music", beets) == (tmp_path / "data" / "music")
    assert resolve_configured_path("inner", beets) == (beets / "inner")


def test_an_absolute_directory_ignores_the_beets_dir(tmp_path: Path) -> None:
    absolute = tmp_path / "elsewhere" / "music"
    assert resolve_configured_path(str(absolute), tmp_path / "data") == absolute
