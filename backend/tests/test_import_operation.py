"""``file_operation`` against beets' own resolution, and ``file_flags``.

The comparison drives beets' ``ImportSession.set_config`` in this test only
(undocumented; product code never calls it), so a beets bump that reorders the
flags fails here.

The ``delete`` clear is asserted SEPARATELY, on the live config after
``set_config``, because the comparison cannot see it: ``_beets_operation``
reads ``delete`` only inside its ``copy`` arm, and ``set_config`` clears
``delete`` only when ``copy`` is off — so 0 of the 128 combinations can
observe that rule through the operation alone.
"""

from __future__ import annotations

import itertools
from typing import Any, cast

import pytest
from beets import config
from beets.importer.session import ImportSession
from beets.library import Library

from app.beets.import_operation import (
    ForcedOperation,
    configured_file_operation,
    file_flags,
    file_operation,
)

_REFLINK_VALUES: tuple[bool | str | None, ...] = (False, True, "auto", None)


def _beets_operation(imp: Any) -> str:
    """What beets runs after ``set_config``: the files stage's order
    (importer/stages.py:278-291) — deliberately NOT the same order as
    ``file_operation``, which models ``set_config``'s — then a copy with
    ``delete`` removing the originals (importer/tasks.py:326-333).

    ``REFLINK_AUTO`` is kept apart from ``REFLINK`` because beets keeps them
    apart: auto falls back to a plain copy where reflink raises."""
    if imp["move"]:
        return "move"
    if imp["copy"]:
        return "move" if imp["delete"] else "copy"
    if imp["link"]:
        return "link"
    if imp["hardlink"]:
        return "hardlink"
    if imp["reflink"].get() == "auto":
        return "reflink_auto"
    if imp["reflink"]:
        return "reflink"
    return "in_place"


def test_file_operation_matches_beets_for_every_flag_combination() -> None:
    # ``set_config`` reads and writes the config view it is handed and touches
    # nothing else on the session, so the library is a stand-in: a real
    # ``Library`` here ran 11 migrations and left its sqlite connection open
    # for a value this test never reads.
    session = ImportSession(cast(Library, object()), None, [], None)
    combos = list(
        itertools.product(
            (False, True),
            (False, True),
            (False, True),
            (False, True),
            _REFLINK_VALUES,
            (False, True),
        )
    )
    mismatches = []
    for move, copy, link, hardlink, reflink, delete in combos:
        flags = {
            "move": move,
            "copy": copy,
            "link": link,
            "hardlink": hardlink,
            "reflink": reflink,
            "delete": delete,
        }
        for key, value in flags.items():
            config["import"][key] = value
        ours = file_operation(
            move=move, copy=copy, link=link, hardlink=hardlink, reflink=reflink, delete=delete
        )
        assert configured_file_operation() == ours, flags
        session.set_config(config["import"])
        theirs = _beets_operation(config["import"])
        if ours != theirs:
            mismatches.append((flags, ours, theirs))
        # The rule the operation comparison is blind to, asserted head-on:
        # "Only delete when copying" (importer/session.py:136-138). ``copy``
        # is re-read AFTER set_config because the precedence chain clears it.
        if not config["import"]["copy"]:
            assert config["import"]["delete"].get(bool) is False, flags
        elif delete:
            assert config["import"]["delete"].get(bool) is True, flags
    assert len(combos) == 128
    assert mismatches == []


@pytest.mark.parametrize("op", ["move", "copy", "hardlink", "in_place"])
def test_file_flags_turn_on_one_flag_and_delete_off(op: ForcedOperation) -> None:
    flags = file_flags(op)
    assert flags == {
        "move": op == "move",
        "copy": op == "copy",
        "link": False,
        "hardlink": op == "hardlink",
        "reflink": False,
        "delete": False,
    }
    assert file_operation(**flags) == op
