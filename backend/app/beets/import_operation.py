"""The file operation beets' import config resolves to, and the flags that force one.

beets 2.13.1 resolves the flags in two places. ``ImportSession.set_config`` keeps
one of move > link > hardlink > reflink, each clearing ``copy``, and clears
``delete`` unless ``copy`` survives (``importer/session.py:114-138``). The files
stage then takes ``copy`` when it is left, and tells reflink apart from
``reflink: auto`` (``importer/stages.py:278-291``); a copy with ``delete``
removes the originals (``importer/tasks.py:326-333``), which is a move.
``tests/test_import_operation.py`` compares :func:`file_operation` with
``set_config`` for every combination and asserts the ``delete`` clear directly,
so a beets bump that changes either fails there.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from beets import config

FileOperation = Literal["move", "copy", "link", "hardlink", "reflink", "reflink_auto", "in_place"]
#: ``hardlink`` has no production caller yet — the hardlink arm is still a
#: BACKLOG item (download providers). Declared here so ``file_flags`` needs
#: no change when it lands, and exercised by the parametrized test.
ForcedOperation = Literal["move", "copy", "hardlink", "in_place"]

_FILE_FLAGS = ("move", "copy", "link", "hardlink", "reflink")


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
    where ``REFLINK`` raises (``util/__init__.py:596-609``).
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


def configured_file_operation() -> FileOperation:
    """:func:`file_operation` of the live ``config["import"]``.

    Truthiness, not ``get(bool)``: ``set_config`` tests each flag with ``if``,
    and ``delete: 1`` must not raise where beets would simply accept it.
    """
    imp = config["import"]
    return file_operation(
        move=bool(imp["move"]),
        copy=bool(imp["copy"]),
        link=bool(imp["link"]),
        hardlink=bool(imp["hardlink"]),
        reflink=imp["reflink"].get(),
        delete=bool(imp["delete"]),
    )


def forced_file_operation(forced: Mapping[str, object]) -> FileOperation:
    """:func:`file_operation` of the live config with ``forced`` merged over it.

    What beets will resolve for a run whose ``import`` overlay is ``forced``:
    the keys ``forced`` names win, the rest fall through to the user's config.
    Read BEFORE the overlay is installed, so a caller can decide on the
    operation while its own ``forced`` dict is still being built.
    """
    imp = config["import"]

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


def file_flags(op: ForcedOperation) -> dict[str, bool]:
    """The five file flags with only ``op`` on, plus ``delete`` off.

    All five, because a user ``hardlink: yes`` beats a lone ``copy: yes``.
    ``in_place`` turns all five off. ``delete`` is off in every case: a copy
    with ``delete`` removes the download.
    """
    return {**{flag: flag == op for flag in _FILE_FLAGS}, "delete": False}
