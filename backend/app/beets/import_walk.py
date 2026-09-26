"""What beets' import walk does with a folder's names, for the folder browser.

beets has no listing API, and its walker ``util.sorted_walk``
(``beets/util/__init__.py:209-263``) calls ``os.path.isdir`` on every entry, so
the browser lists with ``os.scandir`` instead. What it takes from beets is what
decides which folders an import skips, so the browser hides the same ones:

* the two keys ``albums_in_dir`` hands the walk, ``ignore`` and
  ``ignore_hidden`` (``beets/importer/tasks.py:1529-1535``), read here the way it
  reads them, with the patterns encoded because the walk runs on bytes;
* the walk's plain ``fnmatch.fnmatch`` on each name (``util/__init__.py:236-237``);
* beets' own hidden test, ``util.hidden.is_hidden``, which on Linux is a dot-name
  and takes no ``stat`` (``beets/util/hidden.py:49-51``);
* the order the walk sorts a level in, ``bytes.lower`` (``util/__init__.py:256-258``);
* and ``util.normpath``, which the import session maps over every source
  (``beets/importer/session.py:79``), so a typed path lists the folder a start
  on it would walk.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass

import beets
from beets.util import hidden
from beets.util import normpath as beets_normpath


@dataclass(frozen=True)
class WalkRules:
    """beets' ``ignore`` globs, as bytes, and its ``ignore_hidden`` switch."""

    ignore: tuple[bytes, ...]
    ignore_hidden: bool


def walk_rules() -> WalkRules:
    """The live config's two keys, read as ``albums_in_dir`` reads them."""
    return WalkRules(
        ignore=tuple(os.fsencode(pattern) for pattern in beets.config["ignore"].as_str_seq()),
        ignore_hidden=bool(beets.config["ignore_hidden"].get(bool)),
    )


def skipped_by_the_walk(folder: bytes, name: bytes, rules: WalkRules) -> bool:
    """Whether beets' walk leaves ``name`` in ``folder`` out, as ``sorted_walk`` decides."""
    if any(fnmatch.fnmatch(name, pattern) for pattern in rules.ignore):
        return True
    return rules.ignore_hidden and bool(hidden.is_hidden(os.path.join(folder, name)))


def walk_order(name: bytes) -> bytes:
    """The key ``sorted_walk`` sorts a level's names by."""
    return name.lower()


def walked_path(path: str) -> bytes:
    """The folder a start on ``path`` walks: beets' ``normpath`` of it."""
    walked: bytes = beets_normpath(path)
    return walked
