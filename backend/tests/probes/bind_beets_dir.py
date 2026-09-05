"""``-v /srv/music/musicdrop:/data/beets``: the beets dir reached from inside M.

Four measurements for ``test_a_bind_mounted_beets_dir_is_refused_where_the_spelled_rule_allows_it``.
Run under ``unshare -Urm`` by :func:`tests._mountns.run_probe`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app.beets.protected import (
    ProtectedTreeError,
    ProtectedTrees,
    protected_trees,
    refuse_protected_tree,
)
from app.beets.store_layout import StoreLayoutError, check_store_layout
from app.beets.trash_manage import empty_all
from app.config import Settings

base = Path(sys.argv[1])
src = base / "src"
(src / "music" / "Album").mkdir(parents=True)
(src / "music" / "Album" / "01.flac").write_bytes(b"x")
(src / "data").mkdir()
(src / "data" / "library.db").write_bytes(b"db")
M, B = src / "music", src / "data"
T, O = base / "trash", base / "origins"  # noqa: E741 -- O is the origin store everywhere here
T.mkdir()
O.mkdir()

inside = M / "musicdrop"
inside.mkdir()
subprocess.run(["mount", "--bind", str(B), str(inside)], check=True)
assert inside.samefile(B), "the fixture did not alias the beets dir"


def layout() -> str:
    try:
        check_store_layout(
            music_dir=M,
            beets_dir=B,
            trash_dir=T,
            origins_dir=O,
            library_path=B / "library.db",
            settings=Settings(),
        )
    except StoreLayoutError:
        return "REFUSED"
    return "ALLOWED"


def trees() -> ProtectedTrees:
    return protected_trees(
        settings=Settings(),
        music_dir=M,
        beets_dir=B,
        trash_dir=T,
        origins_dir=O,
        library_path=B / "library.db",
    )


def bare() -> ProtectedTrees:
    """No protected identities, but a real Trash: this tree before the guard."""
    st = os.stat(T)
    return ProtectedTrees(ids={}, trash=(st.st_dev, st.st_ino), trash_alias=None)


print("spelled-rule", layout())
try:
    refuse_protected_tree(inside, trees(), action="moved")
    print("mover ALLOWED")
except ProtectedTreeError:
    print("mover REFUSED")

# The same alias as a Trash entry, so Empty Trash is what walks it.
entry = T / "Looks Like An Album"
entry.mkdir()
subprocess.run(["mount", "--bind", str(B), str(entry)], check=True)
try:
    empty_all(T, origins_dir=O, protected=bare())
    print("control-empty-all RAN")
except ProtectedTreeError:
    print("control-empty-all REFUSED")
except Exception as exc:
    # rmtree unlinks the CONTENTS and then fails to rmdir the mount point
    # itself (EBUSY), so the loss lands before the error does.
    print("control-empty-all", type(exc).__name__)
print("control-lost-the-db", not (B / "library.db").exists())

(B / "library.db").write_bytes(b"db")
try:
    empty_all(T, origins_dir=O, protected=trees())
    print("guarded-empty-all RAN")
except ProtectedTreeError:
    print("guarded-empty-all REFUSED")
print("db-survives", (B / "library.db").exists())
