"""Pins: no test may read or write the developer's PERSONAL beets config dir.

The mechanism under test is the ``BEETSDIR`` floor in ``backend/conftest.py``;
the history and the measured blast radius are in that file's docstring. What
matters here is that the pin is not vacuous — each test below fails if the floor
is removed:

* :func:`test_beetsdir_is_pinned_to_the_suite_sandbox` fails at the env var, and
  also pins the derivation of ``PLATFORM_BEETS_DIRS`` itself, so the candidate
  list cannot silently become empty;
* :func:`test_the_importer_statefile_resolves_inside_the_sandbox` fails at the
  exact expression ``beets.importer.state.ImportState`` uses to pick its path;
* :func:`test_a_real_import_state_write_lands_in_the_sandbox` performs a real
  write through the production sink and fails on where the bytes land.

The write test redirects ``HOME``/``XDG_CONFIG_HOME`` at a decoy under
``tmp_path`` first, so that when the floor IS removed (mutation-testing this
file) the escaping write lands in the decoy rather than in the real
``~/.config/beets``. Removing a data-safety fix to prove its test is honest must
not itself cost data.
"""

import os
from pathlib import Path

import beets
import pytest
from beets.importer.state import ImportState

from conftest import PLATFORM_BEETS_DIRS, SUITE_BEETSDIR


def test_beetsdir_is_pinned_to_the_suite_sandbox() -> None:
    """The floor is set, and points somewhere that is not a real config dir.

    The ``PLATFORM_BEETS_DIRS`` assertion is deliberate: without it, that tuple
    could be emptied and every "not a platform dir" check in this file would
    pass trivially. No test monkeypatches it, so this reads its real derivation.
    """
    assert Path.home() / ".config" / "beets" in PLATFORM_BEETS_DIRS

    beetsdir = os.environ.get("BEETSDIR")
    assert beetsdir is not None
    assert Path(beetsdir) == SUITE_BEETSDIR
    assert SUITE_BEETSDIR.is_dir()
    assert SUITE_BEETSDIR not in PLATFORM_BEETS_DIRS


def test_the_importer_statefile_resolves_inside_the_sandbox() -> None:
    """``config["statefile"].as_filename()`` is what ``ImportState`` calls.

    Asserting on the resolved path rather than on ``ImportState`` internals
    keeps this pinned to the one expression that decides where the file goes;
    ``as_filename`` joins the relative default onto ``config_dir()`` at call
    time, so this reads the live ``BEETSDIR``.
    """
    statefile = Path(beets.config["statefile"].as_filename())

    assert statefile.parent == SUITE_BEETSDIR
    assert statefile.name == "state.pickle"


def test_a_real_import_state_write_lands_in_the_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The end-to-end pin: a real ``ImportState`` write, through beets' own code.

    ``history_add`` is the call the attended-import pipeline makes; it opens the
    state file and pickles the new history back out. This is the exact write
    that used to rewrite the developer's ``taghistory``.
    """
    decoy = tmp_path / "decoy-home"
    monkeypatch.setenv("HOME", str(decoy))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(decoy / ".config"))

    state = ImportState()
    state.history_add([b"/nowhere/an-album"])

    written = Path(os.fsdecode(state.path))
    assert written.parent == SUITE_BEETSDIR
    assert written.is_file()
    # Nothing was resolved against HOME/XDG_CONFIG_HOME at all. Without the
    # floor, confuse's config_dir() would have created decoy/.config/beets.
    assert not decoy.exists()
