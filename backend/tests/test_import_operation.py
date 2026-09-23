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
    forced_file_operation,
)

_REFLINK_VALUES: tuple[bool | str | None, ...] = (False, True, "auto", None)


def _beets_operation(imp: Any) -> str:
    """What beets runs after ``set_config``: the files stage's order
    (importer/stages.py:367-380) — deliberately NOT the same order as
    ``file_operation``, which models ``set_config``'s — then a copy with
    ``delete`` removing the originals (importer/tasks.py:527-534).

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


@pytest.mark.parametrize("op", ["move", "copy", "in_place"])
def test_file_flags_turn_on_one_flag_and_delete_off(op: ForcedOperation) -> None:
    """``hardlink`` is not a forceable operation: the keep-downloads setting
    writes it into the user's own config and the app adds no per-import choice
    (``decisions`` #53). ``file_flags`` still turns the flag OFF for every op it
    does take — a user's ``hardlink: yes`` beats a lone ``copy: yes``."""
    flags = file_flags(op)
    assert flags == {
        "move": op == "move",
        "copy": op == "copy",
        "link": False,
        "hardlink": False,
        "reflink": False,
        "delete": False,
    }
    assert file_operation(**flags) == op


# --- forced_file_operation: the operation a run will ACTUALLY resolve to -----


def test_forced_file_operation_lets_the_overlay_win_over_the_config() -> None:
    """The keys a caller is about to force win; the rest fall through.

    This is what makes "the run keeps the files" answerable BEFORE the overlay
    is installed — the import worker needs the answer while it is still
    building the dict it will install, and a second ``config.set`` to ask
    afterwards would double the confuse sources every import costs.
    """
    config["import"]["hardlink"] = True  # the user's config: keep downloads
    assert configured_file_operation() == "hardlink"

    # An inbox import forces a move over that: the download does NOT survive.
    assert forced_file_operation(file_flags("move")) == "move"
    # A Trash restore files nothing at all.
    assert forced_file_operation(file_flags("in_place")) == "in_place"
    # An empty overlay is the user's own config, unchanged.
    assert forced_file_operation({}) == "hardlink"


def test_forced_file_operation_keeps_reflink_auto_distinguishable() -> None:
    """``reflink: auto`` is its own operation (it copies where the filesystem
    cannot clone), so the string has to survive the merge rather than being
    read as a bool."""
    config["import"]["reflink"] = "auto"
    config["import"]["copy"] = False
    assert forced_file_operation({}) == "reflink_auto"
    assert forced_file_operation({"reflink": "auto"}) == "reflink_auto"
    assert forced_file_operation({"reflink": True}) == "reflink"
    assert forced_file_operation({"reflink": False, "copy": True}) == "copy"


def test_forced_file_operation_reads_the_forced_delete_not_the_users() -> None:
    """A copy with ``delete`` is a move (beets removes the originals), but
    MusicDrop forces ``delete`` off on every run — so the merged answer for a
    ``copy: yes, delete: yes`` config is a copy, which is what beets will do."""
    config["import"]["copy"] = True
    config["import"]["delete"] = True
    assert configured_file_operation() == "move"  # the user's config alone
    assert forced_file_operation({"delete": False}) == "copy"  # what the run does
