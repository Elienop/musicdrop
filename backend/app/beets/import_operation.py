"""The file operation beets' import config resolves to, and the flags that force one.

beets 2.14.0 resolves the flags in two places. ``ImportSession.set_config`` keeps
one of move > link > hardlink > reflink, each clearing ``copy``, and clears
``delete`` unless ``copy`` survives (``importer/session.py:114-138``). The files
stage then takes ``copy`` when it is left, and tells reflink apart from
``reflink: auto`` (``importer/stages.py:367-380``); a copy with ``delete``
removes the originals (``importer/tasks.py:527-534``), which is a move.
``tests/test_import_operation.py`` compares :func:`file_operation` with
``set_config`` for every combination, so a beets bump that changes either fails.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal

import confuse
from beets import config

from app.models.config_api import FileOperation

__all__ = [
    "EVERY_RUN",
    "FILE_FLAGS",
    "FileOperation",
    "ForcedOperation",
    "configured_file_operation",
    "file_flags",
    "file_operation",
    "forced_file_operation",
    "loaded_file_operation",
    "view_file_operation",
]

#: The part of every import's overlay that decides the operation: no run lets
#: beets delete the sources (``run_import_worker``'s ``forced``), so a user
#: ``copy: yes`` + ``delete: yes`` copies in-app where beets alone would move.
EVERY_RUN: Final[Mapping[str, object]] = {"delete": False}

#: The operations a caller may FORCE for one run. No ``hardlink``: keep-downloads
#: is one global switch written into beets' own ``import:`` keys, with no
#: per-import choice (``decisions`` #53).
ForcedOperation = Literal["move", "copy", "in_place"]

#: beets' five filing flags, the keys ``set_config`` makes exclusive.
FILE_FLAGS: Final = ("move", "copy", "link", "hardlink", "reflink")


def file_operation(
    *,
    move: bool,
    copy: bool,
    link: bool,
    hardlink: bool,
    reflink: bool | str | None,
    delete: bool,
) -> FileOperation:
    """The operation beets runs for these ``import`` flags.

    ``reflink_auto`` is its own answer because beets treats it as its own
    operation: ``REFLINK_AUTO`` copies when the filesystem cannot reflink,
    where ``REFLINK`` raises (``util/__init__.py:617-634``).
    """
    if move:
        return "move"
    if link:
        return "link"
    if hardlink:
        return "hardlink"
    if reflink:
        return "reflink_auto" if reflink == "auto" else "reflink"
    if copy:
        return "move" if delete else "copy"
    return "in_place"


def forced_file_operation(forced: Mapping[str, object]) -> FileOperation:
    """:func:`file_operation` of the live config with ``forced`` merged over it.

    The keys ``forced`` names win, the rest fall through to the user's config.
    Read BEFORE the overlay is installed, so a caller can decide on the operation
    while its own ``forced`` dict is still being built.

    Truthiness, not ``get(bool)``: ``set_config`` tests each flag with ``if``,
    and ``delete: 1`` must not raise where beets would accept it.
    """
    return view_file_operation(config["import"], forced)


def view_file_operation(imp: confuse.ConfigView, forced: Mapping[str, object]) -> FileOperation:
    """:func:`forced_file_operation` of any config's ``import`` view.

    For a config that is not the live one: the text a write is about to put on
    disk, with its includes merged (``app/beets/config_editor.py``).
    """

    def flag(name: str) -> object:
        return forced[name] if name in forced else imp[name].get()

    reflink = flag("reflink")
    return file_operation(
        move=bool(flag("move")),
        copy=bool(flag("copy")),
        link=bool(flag("link")),
        hardlink=bool(flag("hardlink")),
        # ``reflink: auto`` is its own operation, so the string has to survive
        # the merge; anything else is read for truth like the other flags.
        reflink=reflink if isinstance(reflink, bool | str) else bool(reflink),
        delete=bool(flag("delete")),
    )


def configured_file_operation() -> FileOperation:
    """:func:`file_operation` of the live ``config["import"]``, no overlay.

    The empty-overlay case of :func:`forced_file_operation` rather than a second
    reader of the same six flags, so the parity test covers both.
    """
    return forced_file_operation({})


def loaded_file_operation() -> FileOperation | None:
    """What a default import runs under the live config: its caller names no operation.

    :data:`EVERY_RUN` over the config, as every run has it; asked once per load
    (boot and Apply), before any import can overlay the live config.

    ``None`` when ``import:`` is not a mapping (``import:`` left empty, say):
    beets loads such a file and fails only when an import reads the key, so
    the load must not fail here either.
    """
    try:
        return forced_file_operation(EVERY_RUN)
    except confuse.ConfigError:
        return None


def file_flags(op: ForcedOperation) -> dict[str, bool]:
    """The five file flags with only ``op`` on, plus ``delete`` off.

    All five, because a user ``hardlink: yes`` beats a lone ``copy: yes``.
    ``in_place`` turns all five off. ``delete`` is off in every case: a copy
    with ``delete`` removes the download.
    """
    return {**{flag: flag == op for flag in FILE_FLAGS}, "delete": False}
