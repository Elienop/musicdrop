"""Empty an entry whose library row spells it in a DIFFERENT CASE.

On casefolding storage ``<trash>/ART - ALB`` and ``<trash>/Art - Alb`` are one
directory and one inode, so a row holding either spelling names the album's only
copy. A byte-prefix check could not see that and one Empty destroyed it (the
security seat measured exactly this, on a real ``casefold=utf8-12.1.0`` tmpfs).

The two doors into a divergent case spelling are the ones
``tests/probes/casefold_delete.py`` documents; this probe builds the end state by
hand, which is the row/disk shape those measurements printed.

Needs a mount namespace AND a casefolding tmpfs, so it cannot run in-process.
Prints ``CASE-INSENSITIVE False`` when the kernel or tmpfs will not give one,
which is what lets the caller skip honestly instead of passing on a control.

Run under ``unshare -Urm`` by :func:`tests._mountns.run_probe`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from beets.library import Item, Library

from app.beets.protected import ProtectedTreeError, protected_trees
from app.beets.trash_manage import empty_all, empty_one
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

music = root / "music"
music.mkdir()
trash = root / "trash"
origins = root / "trash-origins"
entry = trash / "Art - Alb"
entry.mkdir(parents=True)
only_copy = entry / "01 T1.mp3"
only_copy.write_bytes(b"\x00")

other = trash / "Art - Alb2"
other.mkdir()
other_copy = other / "01 T2.mp3"
other_copy.write_bytes(b"\x00")

lib = Library(str(root / "library.db"), directory=str(music))
item = Item(album="Alb", albumartist="Art", artist="Art", title="T1", track=1)
# The OTHER case, which on this filesystem is the same file.
item.path = os.fsencode(str(trash / "ART - ALB" / "01 T1.mp3"))
lib.add_album([item]).store()
print("SAME FILE", os.path.samefile(str(only_copy), os.fsdecode(item.path)))

# The same divergence one level up: the ROOT spelled in another case.
second = Item(album="Alb2", albumartist="Art", artist="Art", title="T2", track=1)
second.path = os.fsencode(str(trash.parent / trash.name.upper() / "Art - Alb2" / "01 T2.mp3"))
lib.add_album([second]).store()
print("SAME FILE ROOT", os.path.samefile(str(other_copy), os.fsdecode(second.path)))

trees = protected_trees(
    settings=Settings(),
    music_dir=music,
    beets_dir=root,
    trash_dir=trash,
    origins_dir=origins,
    library_path=root / "library.db",
)

try:
    empty_one(str(entry), origins_dir=origins, protected=trees, lib=lib)
    print("EMPTY ONE RAN")
except ProtectedTreeError:
    print("EMPTY ONE REFUSED")
print("ONLY COPY SURVIVED ONE", only_copy.exists())

try:
    empty_one(str(other), origins_dir=origins, protected=trees, lib=lib)
    print("EMPTY ROOT-CASE RAN")
except ProtectedTreeError:
    print("EMPTY ROOT-CASE REFUSED")
print("ONLY COPY SURVIVED ROOT-CASE", other_copy.exists())

try:
    empty_all(trash, origins_dir=origins, protected=trees, lib=lib)
    print("EMPTY ALL RAN")
except ProtectedTreeError:
    print("EMPTY ALL REFUSED")
print("ONLY COPY SURVIVED ALL", only_copy.exists())
print("ALBUM STILL LISTED", len(list(lib.albums())))
