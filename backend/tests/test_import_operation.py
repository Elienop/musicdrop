"""``file_operation`` against beets' own resolution, and ``file_flags``.

The comparison drives beets' ``ImportSession.set_config`` in this test only
(undocumented; product code never calls it), so a beets bump that reorders the
flags or changes when ``delete`` survives fails here.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

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
    (importer/stages.py:278-291), then a copy with ``delete`` removing the
    originals (importer/tasks.py:326-333)."""
    if imp["move"]:
        return "move"
    if imp["copy"]:
        return "move" if imp["delete"] else "copy"
    if imp["link"]:
        return "link"
    if imp["hardlink"]:
        return "hardlink"
    if imp["reflink"]:
        return "reflink"
    return "in_place"


def test_file_operation_matches_beets_for_every_flag_combination(tmp_path: Path) -> None:
    session = ImportSession(Library(str(tmp_path / "l.db"), str(tmp_path)), None, [], None)
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
    assert len(combos) == 128
    assert mismatches == []


@pytest.mark.parametrize("op", ["move", "copy", "hardlink"])
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
