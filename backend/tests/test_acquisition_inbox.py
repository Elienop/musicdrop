"""Inbox-dir resolution + path-containment guard (the acquisition seam's gate).

``resolve_inbox_dir`` mirrors ``resolve_trash_dir``; ``contain`` is the security
boundary that keeps an attacker-influenced download folder name (``../`` escape,
absolute path, or a symlink pointing outside) from reaching the importer.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from app.acquisition.inbox import contain, resolve_inbox_dir
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


def test_contain_rejects_dotdot_escape(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    assert contain(str(inbox / ".." / "etc"), inbox) is None


def test_contain_rejects_absolute_outside(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    assert contain("/etc", inbox) is None


def test_contain_rejects_symlink_escape(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = inbox / "evil"
    link.symlink_to(outside)
    # realpath resolves the symlink to ``outside`` (NOT under inbox) -> rejected.
    assert contain(str(link), inbox) is None
