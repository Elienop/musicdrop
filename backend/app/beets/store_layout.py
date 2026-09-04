"""Where MusicDrop's own stores may sit relative to the music library.

Two directories in this app delete things wholesale. ``trash_manage.empty_all``
``rmtree``s every child of the Trash dir, and ``trash_origins.clear_trash_origins``
unlinks every ``*.json`` directly inside the origin store. Neither asks what the
directory it was handed actually holds, so where those two resolve decides
whether "Empty Trash" removes a hundred trashed albums or the library.

The rule this module enforces (owner's ruling, 2026-09-04): **a Trash inside the
music library is allowed; one on top of or around it is refused. The origin store
stays out of the music library.** Four paths take part —

``M`` the music library (``directory:`` in ``config.yaml``), ``B`` the beets data
directory (``MUSICDROP_BEETS_DIR``, holding ``library.db`` + ``config.yaml``),
``T`` the Trash (``MUSICDROP_TRASH_DIR``, default ``<B>/trash``) and ``O`` the
origin store (``MUSICDROP_TRASH_ORIGINS_DIR``, default ``<B>/trash-origins``).

Refused, with the loss each one would cause:

===========================  ==================================================
``T`` is / contains ``M``    Empty Trash deletes the music library
``T`` is / contains ``B``    Empty Trash deletes ``library.db`` + ``config.yaml``
``O`` is / contains ``M``    the store sweep unlinks ``*.json`` in the library
``O`` is / contains ``B``    the store sweep unlinks ``*.json`` in the beets dir
``M`` contains ``O``         a folder delete above the store trashes the records
``O`` is / inside ``T``      Empty Trash deletes the records; they also list as
                             Trash entries
``T`` inside ``O``           trashed albums land among the origin records
===========================  ==================================================

Allowed, and each one is a shape somebody really runs: ``T`` strictly inside
``M`` (the ruling — ``/music/.trash`` makes a delete a same-disk rename), ``T``
and ``O`` under ``B`` (the DEFAULT), and ``B`` inside ``M`` provided ``O`` is
moved out of the library with it.

Comparisons run on FULLY RESOLVED absolute paths, both sides. ``is_relative_to``
is lexical — ``<T>/../Sibling`` reads as "under ``<T>``" until ``resolve()``
normalises the ``..`` away, the same trap ``trash_manage.resolve_trash_child``
documents — and ``_music_dir`` is not symlink-resolved, so a library reached
through ``/music -> /mnt/tank/music`` would otherwise compare unequal to a Trash
spelled ``/mnt/tank/music/.trash``. ``Path.resolve()`` runs non-strict here: none
of the four has to exist yet.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.beets.library import LibraryHandle, _music_dir
from app.beets.trash import resolve_trash_dir, resolve_trash_origins_dir
from app.config import Settings

__all__ = [
    "MUSIC_SETTING",
    "ORIGINS_SETTING",
    "TRASH_SETTING",
    "StoreLayoutError",
    "check_store_layout",
    "layout_error_for_music_dir",
    "require_safe_store_layout",
    "resolve_configured_music_dir",
]

#: How each of the three inputs is spelled for the operator who has to change it.
TRASH_SETTING = "MUSICDROP_TRASH_DIR"
ORIGINS_SETTING = "MUSICDROP_TRASH_ORIGINS_DIR"
MUSIC_SETTING = "`directory:` in config.yaml"
_BEETS_SETTING = "MUSICDROP_BEETS_DIR"


class StoreLayoutError(Exception):
    """Trash or the origin store sits where using it would destroy data.

    One exception for all seven refused relationships: every one of them is the
    same operator action (move a directory) and the same remedy (point the
    setting somewhere else), so a caller that wants to branch on WHICH would be
    branching on prose. What varies is the message, which names the setting to
    change, both resolved paths, the loss the layout would cause and the fix.
    """


def _resolved(path: Path) -> Path:
    """Absolute, symlink-free, ``..``-free — the only form this module compares."""
    return Path(os.path.expanduser(str(path))).resolve()


def _relation(container: Path, inner: Path) -> str | None:
    """``"is"`` when the two are the same directory, ``"contains"`` when ``inner``
    is strictly below ``container``, ``None`` when neither holds.

    Both arguments must already be ``_resolved``; ``is_relative_to`` compares
    path components, so ``/data/trash`` does not read as containing
    ``/data/trash-origins`` the way a string prefix would.
    """
    if container == inner:
        return "is"
    if inner.is_relative_to(container):
        return "contains"
    return None


def _refuse(
    *,
    subject: str,
    relation: str,
    other: str,
    setting: str,
    subject_path: Path,
    other_setting: str,
    other_path: Path,
    loss: str,
    fix: str,
) -> StoreLayoutError:
    """Compose the one message shape, so boot, Save, Validate and Apply agree.

    Written for somebody reading ``docker logs`` or the editor's gutter with no
    access to this source: what the layout is, where each side resolved to, what
    it would have cost, and which setting to move.
    """
    return StoreLayoutError(
        f"{subject} {relation} {other}. "
        f"{setting} resolves to {str(subject_path)!r}; "
        f"{other_setting} resolves to {str(other_path)!r}. "
        f"{loss} {fix}"
    )


def check_store_layout(
    *,
    music_dir: Path,
    beets_dir: Path,
    trash_dir: Path,
    origins_dir: Path,
) -> None:
    """Raise :class:`StoreLayoutError` on any refused relationship among the four.

    The four are resolved here rather than by the caller, so no call site can
    compare a lexical path against a resolved one. Order of the checks decides
    only WHICH message a layout that breaks several rules gets; every refused
    layout raises whichever comes first.
    """
    music = _resolved(music_dir)
    beets = _resolved(beets_dir)
    trash = _resolved(trash_dir)
    origins = _resolved(origins_dir)

    trash_fix = (
        f"Point {TRASH_SETTING} at a directory that neither is nor contains it"
        " — a folder INSIDE the music library, such as"
        f" {str(music / '.trash')!r}, is allowed and makes deletes same-disk"
        f" renames — or unset {TRASH_SETTING} for the default"
        f" {str(beets / 'trash')!r}."
    )
    origins_fix = (
        f"Point {ORIGINS_SETTING} at a directory of its own outside the music"
        " library, or unset it for the default"
        f" {str(beets / 'trash-origins')!r}."
    )

    relation = _relation(trash, music)
    if relation is not None:
        raise _refuse(
            subject="The Trash directory",
            relation=relation,
            other="the music library",
            setting=TRASH_SETTING,
            subject_path=trash,
            other_setting=MUSIC_SETTING,
            other_path=music,
            loss=(
                "Emptying the Trash permanently removes every entry under it,"
                " so this layout would delete the music library."
            ),
            fix=trash_fix,
        )

    relation = _relation(trash, beets)
    if relation is not None:
        raise _refuse(
            subject="The Trash directory",
            relation=relation,
            other="the beets data directory",
            setting=TRASH_SETTING,
            subject_path=trash,
            other_setting=_BEETS_SETTING,
            other_path=beets,
            loss=(
                "Emptying the Trash permanently removes every entry under it, so"
                " this layout would delete library.db, config.yaml and the Trash"
                " origin records."
            ),
            fix=trash_fix,
        )

    relation = _relation(origins, music)
    if relation is not None:
        raise _refuse(
            subject="The Trash origin store",
            relation=relation,
            other="the music library",
            setting=ORIGINS_SETTING,
            subject_path=origins,
            other_setting=MUSIC_SETTING,
            other_path=music,
            loss=(
                "Emptying the Trash sweeps the store, unlinking every *.json"
                " file directly inside it, so this layout would delete JSON"
                " files from the music library."
            ),
            fix=origins_fix,
        )

    relation = _relation(origins, beets)
    if relation is not None:
        raise _refuse(
            subject="The Trash origin store",
            relation=relation,
            other="the beets data directory",
            setting=ORIGINS_SETTING,
            subject_path=origins,
            other_setting=_BEETS_SETTING,
            other_path=beets,
            loss=(
                "Emptying the Trash sweeps the store, unlinking every *.json"
                " file directly inside it, so this layout would delete JSON"
                " files from the beets data directory."
            ),
            fix=origins_fix,
        )

    if _relation(music, origins) == "contains":
        raise _refuse(
            subject="The music library",
            relation="contains",
            other="the Trash origin store",
            setting=MUSIC_SETTING,
            subject_path=music,
            other_setting=ORIGINS_SETTING,
            other_path=origins,
            loss=(
                "Deleting any folder above the store moves the records into"
                " Trash with it, and the record is what Restore reads to put a"
                " trashed folder back where it came from."
            ),
            fix=origins_fix,
        )

    relation = _relation(trash, origins)
    if relation is not None:
        raise _refuse(
            subject="The Trash directory",
            relation=relation,
            other="the Trash origin store",
            setting=TRASH_SETTING,
            subject_path=trash,
            other_setting=ORIGINS_SETTING,
            other_path=origins,
            loss=(
                "Emptying the Trash would delete the records it needs, and each"
                " record file would also be listed as a trashed entry of its own."
            ),
            fix=origins_fix,
        )

    if _relation(origins, trash) == "contains":
        raise _refuse(
            subject="The Trash origin store",
            relation="contains",
            other="the Trash directory",
            setting=ORIGINS_SETTING,
            subject_path=origins,
            other_setting=TRASH_SETTING,
            other_path=trash,
            loss=(
                "Trashed albums would land among the origin records, which both"
                " the store sweep and the Trash listing walk."
            ),
            fix=origins_fix,
        )


def require_safe_store_layout(settings: Settings, handle: LibraryHandle) -> None:
    """The boot-time and Apply-time check, from the live settings + handle.

    Resolves the same ``trash_dir`` / ``trash_origins_dir`` the delete paths
    themselves resolve (``app.beets.trash``), so the check cannot pass on a pair
    of paths nothing else uses.
    """
    check_store_layout(
        music_dir=Path(_music_dir(handle.lib)),
        beets_dir=handle.beets_dir,
        trash_dir=resolve_trash_dir(settings, handle),
        origins_dir=resolve_trash_origins_dir(settings, handle),
    )


def resolve_configured_music_dir(raw: str, beets_dir: Path) -> Path:
    """Resolve a ``directory:`` value the way beets will resolve it.

    confuse's ``Filename.value`` (``confuse/templates.py``, the ``value`` body):
    ``expanduser`` first, then — for a value that came from a file, with the
    default ``in_source_dir=False`` and beets' source not setting
    ``base_for_paths`` — a RELATIVE path is joined to ``view.root().config_dir()``,
    which is ``BEETSDIR``, and the result goes through ``abspath``. The starter
    config says the same thing in its own words ("Paths are relative to this
    file's directory") and ships ``directory: ../music``.

    ``resolve()`` is ours, not confuse's: this module compares symlink-free
    paths on both sides, and confuse stops at ``abspath``.

    NOTE the pre-existing disagreement this does NOT change: the
    ``WritablePath`` validator in ``app/models/config_editor.py`` resolves the
    same value against the process CWD instead. Both run on a Save; they can
    disagree only for a relative ``directory:``, and only about which directory
    is checked for writability.
    """
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        expanded = os.path.join(str(beets_dir), expanded)
    return Path(os.path.abspath(expanded)).resolve()


def layout_error_for_music_dir(
    raw_directory: str,
    *,
    settings: Settings,
    handle: LibraryHandle,
) -> StoreLayoutError | None:
    """The refusal a candidate ``directory:`` would cause, or ``None``.

    Returns rather than raises: its callers (Save, Validate, Apply) turn it into
    a response body, and every one of them wants the message rather than a
    traceback. ``B``, ``T`` and ``O`` come from the LIVE settings and handle
    because they are env-derived and fixed for the process lifetime — only ``M``
    moves, and only through this value.
    """
    try:
        check_store_layout(
            music_dir=resolve_configured_music_dir(raw_directory, handle.beets_dir),
            beets_dir=handle.beets_dir,
            trash_dir=resolve_trash_dir(settings, handle),
            origins_dir=resolve_trash_origins_dir(settings, handle),
        )
    except StoreLayoutError as exc:
        return exc
    return None
