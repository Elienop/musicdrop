"""Inbox-dir resolution + path-containment guard (the acquisition seam's gate).

``resolve_inbox_dir`` mirrors ``resolve_trash_dir``; ``contain`` is the security
boundary that keeps an attacker-influenced download folder name (``../`` escape,
absolute path, or a symlink pointing outside) from reaching the importer.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from app.acquisition.inbox import coalesce_album_root, contain, resolve_inbox_dir
from app.config import Settings
from tests.conftest import make_test_handle

if TYPE_CHECKING:
    from beets.library import Library


def test_inbox_defaults_under_handle_beets_dir(empty_lib: Library, tmp_path: Path) -> None:
    handle = make_test_handle(empty_lib, tmp_path)
    assert resolve_inbox_dir(Settings(inbox_dir=""), handle) == tmp_path.resolve() / "inbox"


def test_inbox_override_is_absolute(empty_lib: Library, tmp_path: Path) -> None:
    settings = Settings(inbox_dir=str(tmp_path / "custom"))
    resolved = resolve_inbox_dir(settings, make_test_handle(empty_lib, tmp_path))
    assert resolved.is_absolute()
    assert resolved == (tmp_path / "custom").resolve()


def test_contain_accepts_path_under_inbox(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    (inbox / "Artist" / "Album").mkdir(parents=True)
    target = inbox / "Artist" / "Album"
    assert contain(str(target), inbox) == target.resolve()


def test_contain_accepts_inbox_root_itself(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    assert contain(str(inbox), inbox) == inbox.resolve()


def test_contain_strict_rejects_inbox_root(tmp_path: Path) -> None:
    # The inbox root passes the default (root-inclusive) contain but is rejected
    # under strict=True: a MOVE target must be a strict descendant, never the root
    # (importing the root would sweep the whole inbox).
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    assert contain(str(inbox), inbox) == inbox.resolve()
    assert contain(str(inbox), inbox, strict=True) is None


def test_contain_strict_accepts_descendant(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    target = inbox / "Artist" / "Album"
    target.mkdir(parents=True)
    assert contain(str(target), inbox, strict=True) == target.resolve()


def test_contain_rejects_dotdot_escape(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    assert contain(str(inbox / ".." / "etc"), inbox) is None


def test_contain_rejects_absolute_outside(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    assert contain("/etc", inbox) is None


def test_contain_rejects_embedded_null_byte(tmp_path: Path) -> None:
    # A malformed path (embedded NUL) makes Path.resolve raise ValueError, not
    # OSError. contain() must swallow it and return None so a hostile webhook
    # path can never surface as an unhandled 500 (keeps "only 401 is non-2xx").
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    assert contain(str(inbox / "evil\x00album"), inbox) is None


def test_contain_rejects_symlink_escape(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = inbox / "evil"
    link.symlink_to(outside)
    # realpath resolves the symlink to ``outside`` (NOT under inbox) -> rejected.
    assert contain(str(link), inbox) is None


def test_coalesce_disc_dir_returns_album_parent(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    album = inbox / "Artist" / "Album"
    folder = album / "CD1"
    folder.mkdir(parents=True)
    (album / "CD2").mkdir()  # a real multi-disc album has a second disc sibling
    assert coalesce_album_root(folder, inbox) == album


def test_coalesce_non_disc_dir_returns_itself(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    folder = inbox / "Artist" / "Album"
    folder.mkdir(parents=True)
    assert coalesce_album_root(folder, inbox) == folder


def test_coalesce_matches_disc_variants(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    album = inbox / "Artist" / "Album"
    names = ("Disc 2", "disk_3", "CD-04", "cd5")
    for name in names:
        (album / name).mkdir(parents=True, exist_ok=True)
    # with disc siblings present, every disc-named variant coalesces to the album
    for name in names:
        assert coalesce_album_root(album / name, inbox) == album


def test_coalesce_lone_disc_named_album_imports_itself(tmp_path: Path) -> None:
    # An album whose OWN folder merely looks like a disc dir (no disc siblings in
    # the parent) must NOT walk up and MOVE-import the whole parent directory.
    inbox = tmp_path / "inbox"
    album = inbox / "Some Artist" / "CD1"  # a lone album literally named "CD1"
    album.mkdir(parents=True)
    (inbox / "Some Artist" / "Another Album").mkdir()  # a non-disc sibling only
    assert coalesce_album_root(album, inbox) == album


def test_coalesce_disc_dir_at_inbox_root_does_not_escape(tmp_path: Path) -> None:
    # A disc-named dir whose parent IS the inbox root must NOT coalesce up to the
    # inbox itself (that would import the whole inbox).
    inbox = tmp_path / "inbox"
    folder = inbox / "CD1"
    folder.mkdir(parents=True)
    assert coalesce_album_root(folder, inbox) == folder


def test_coalesce_two_disc_siblings_collapse_to_same_album(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    album = inbox / "Artist" / "Album"
    cd1 = album / "CD1"
    cd2 = album / "CD2"
    cd1.mkdir(parents=True)
    cd2.mkdir(parents=True)
    assert coalesce_album_root(cd1, inbox) == coalesce_album_root(cd2, inbox) == album
