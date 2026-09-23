"""What Empty answers for a row that spells the SAME file by an alias beets cannot see.

Four aliases, each measured through ``empty_one``. None of them is producible by
MusicDrop's own writer — beets stores what its mover was given — so these are
RESIDUALS of "beets has no path identity beyond the string", recorded rather than
guarded: a hand-rolled identity check to catch them is the code three rounds of
review found faults in.

* ``NFD`` / ``NFC`` and ``ss`` / ``SS`` — only aliases on a mount that NORMALISES
  as well as folds (``casefold=utf8-12.1.0`` does both). beets decides case by
  probing the filesystem and then ``str.lower()``s, which is a fold but not a
  normalisation.
* ``bind`` — a bind mount, the one alias no string rewriting collapses.
* ``symlink`` — a second path to the Trash through a link the config never named.

Prints ``<label> REFUSED|REMOVED <only copy survived>`` per arm. Run under
``unshare -Urm`` by :func:`tests._mountns.run_probe`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unicodedata
from pathlib import Path

from beets.library import Item, Library

from app.beets.protected import ProtectedTreeError, protected_trees
from app.beets.trash_manage import empty_one
from app.config import Settings

base = Path(sys.argv[1])
CF = base / "cf"
CF.mkdir(parents=True, exist_ok=True)


def _casefold_root() -> Path | None:
    """A directory on a casefolding tmpfs, or ``None`` if this box has none."""
    mount = subprocess.run(
        ["mount", "-t", "tmpfs", "-o", "casefold=utf8-12.1.0", "none", str(CF)],
        capture_output=True,
        text=True,
        check=False,
    )
    if mount.returncode != 0:
        return None
    root = CF / "t"
    root.mkdir()
    if subprocess.run(["chattr", "+F", str(root)], capture_output=True, check=False).returncode:
        return None
    return root


def _measure(label: str, root: Path, entry_name: str, row_spelling: str) -> None:
    """One arm: an entry, a row naming it by ``row_spelling``, then Empty."""
    work = root / label
    music = work / "music"
    music.mkdir(parents=True)
    trash = work / "trash"
    origins = work / "trash-origins"
    entry = trash / entry_name
    entry.mkdir(parents=True)
    only_copy = entry / "01 T1.mp3"
    only_copy.write_bytes(b"\x00")

    lib = Library(str(work / "library.db"), directory=str(music))
    item = Item(album="Alb", albumartist="Art", artist="Art", title="T1", track=1)
    item.path = os.fsencode(row_spelling.format(trash=trash, entry=entry))
    lib.add_album([item]).store()
    print(f"{label} SAME FILE", os.path.samefile(str(only_copy), os.fsdecode(item.path)))

    trees = protected_trees(
        settings=Settings(),
        music_dir=music,
        beets_dir=work,
        trash_dir=trash,
        origins_dir=origins,
        library_path=work / "library.db",
    )
    try:
        empty_one(str(entry), origins_dir=origins, protected=trees, lib=lib)
        print(f"{label} REMOVED")
    except ProtectedTreeError:
        print(f"{label} REFUSED")
    print(f"{label} ONLY COPY SURVIVED", only_copy.exists())


root = _casefold_root()
if root is None:
    print("CASE-INSENSITIVE False")
    raise SystemExit(0)
probe = root / "probe"
probe.mkdir()
insensitive = (root / "PROBE").is_dir()
print("CASE-INSENSITIVE", insensitive)
if not insensitive:
    raise SystemExit(0)

NFC = unicodedata.normalize("NFC", "Björk - Vesp")
NFD = unicodedata.normalize("NFD", NFC)
_measure("NFD-ROW", root, NFC, "{trash}/" + NFD + "/01 T1.mp3")
_measure("SS-ROW", root, "Straße - Alb", "{trash}/STRASSE - ALB/01 T1.mp3")

# The bind mount: the Trash reachable at a second path the config never named.
bind_work = base / "bind"
bind_alias = bind_work / "alias"
bind_alias.mkdir(parents=True)
bind_trash = bind_work / "root" / "trash"
(bind_trash / "Art - Alb").mkdir(parents=True)
subprocess.run(["mount", "--bind", str(bind_trash), str(bind_alias)], check=True)
_measure_bind = bind_work / "root"
music = _measure_bind / "music"
music.mkdir(parents=True)
origins = _measure_bind / "trash-origins"
entry = bind_trash / "Art - Alb"
only_copy = entry / "01 T1.mp3"
only_copy.write_bytes(b"\x00")
lib = Library(str(_measure_bind / "library.db"), directory=str(music))
item = Item(album="Alb", albumartist="Art", artist="Art", title="T1", track=1)
item.path = os.fsencode(str(bind_alias / "Art - Alb" / "01 T1.mp3"))
lib.add_album([item]).store()
print("BIND SAME FILE", os.path.samefile(str(only_copy), os.fsdecode(item.path)))
trees = protected_trees(
    settings=Settings(),
    music_dir=music,
    beets_dir=_measure_bind,
    trash_dir=bind_trash,
    origins_dir=origins,
    library_path=_measure_bind / "library.db",
)
try:
    empty_one(str(entry), origins_dir=origins, protected=trees, lib=lib)
    print("BIND REMOVED")
except ProtectedTreeError:
    print("BIND REFUSED")
print("BIND ONLY COPY SURVIVED", only_copy.exists())

# A second symlink to the Trash, which the config never named.
link_work = base / "link"
link_trash = link_work / "trash"
(link_trash / "Art - Alb").mkdir(parents=True)
link_alias = link_work / "alias"
link_alias.symlink_to(link_trash)
music = link_work / "music"
music.mkdir(parents=True)
origins = link_work / "trash-origins"
entry = link_trash / "Art - Alb"
only_copy = entry / "01 T1.mp3"
only_copy.write_bytes(b"\x00")
lib = Library(str(link_work / "library.db"), directory=str(music))
item = Item(album="Alb", albumartist="Art", artist="Art", title="T1", track=1)
item.path = os.fsencode(str(link_alias / "Art - Alb" / "01 T1.mp3"))
lib.add_album([item]).store()
print("SYMLINK SAME FILE", os.path.samefile(str(only_copy), os.fsdecode(item.path)))
trees = protected_trees(
    settings=Settings(),
    music_dir=music,
    beets_dir=link_work,
    trash_dir=link_trash,
    origins_dir=origins,
    library_path=link_work / "library.db",
)
try:
    empty_one(str(entry), origins_dir=origins, protected=trees, lib=lib)
    print("SYMLINK REMOVED")
except ProtectedTreeError:
    print("SYMLINK REFUSED")
print("SYMLINK ONLY COPY SURVIVED", only_copy.exists())
