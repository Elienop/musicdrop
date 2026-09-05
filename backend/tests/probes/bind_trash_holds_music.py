"""``-v /srv/data/trash:/data/trash`` with the music library mounted INSIDE it.

The owner-named shape: the Trash is the host parent of a bind-mounted music
library, so a Trash entry named ``music`` IS the library by inode while every
spelled row in ``store_layout`` allows the layout. Restoring that entry used to
run ``move_no_merge``'s copy branch with the destination inside the source and
``rmtree`` the lot, reporting ``restored=True``.

Two measurements, one per guard, so either alone is enough:

* with the real protected set, ``restore_album`` refuses before it moves;
* with an EMPTY set, ``move_no_merge`` still refuses, because copy-then-delete
  of a source that contains its own destination is deletion for any caller.

Run under ``unshare -Urm`` by :func:`tests._mountns.run_probe`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from beets.library import Library

from app.beets.protected import ProtectedTreeError, ProtectedTrees, protected_trees
from app.beets.store_layout import StoreLayoutError, check_store_layout
from app.beets.trash_manage import restore_album
from app.beets.trash_origins import write_trash_origin
from app.config import Settings
from app.fsutil import move_no_merge

base = Path(sys.argv[1])
# The host side, then the two mounts the compose file makes of it:
# ``-v <host>/data:/data`` and ``-v <host>/data/trash/music:/music``.
host = base / "host"
(host / "data" / "trash" / "music" / "Artist" / "Album").mkdir(parents=True)
(host / "data" / "trash" / "music" / "Artist" / "Album" / "01.flac").write_bytes(b"x")
(host / "data" / "trash-origins").mkdir()
B = base / "data"
M = base / "music"
B.mkdir()
M.mkdir()
subprocess.run(["mount", "--bind", str(host / "data"), str(B)], check=True)
subprocess.run(["mount", "--bind", str(host / "data" / "trash" / "music"), str(M)], check=True)
T = B / "trash"
O = B / "trash-origins"  # noqa: E741 -- O is the origin store everywhere here
entry = T / "music"
assert entry.samefile(M), "the fixture did not alias the music library"

origin = M / "Restored Here"
write_trash_origin(O, "music", origin=str(origin), moved="folder")

lib = Library(str(B / "library.db"), directory=str(M))


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
    """No protected identities, but a real Trash: the guard switched off."""
    st = os.stat(T)
    return ProtectedTrees(ids={}, trash=(st.st_dev, st.st_ino))


def track_survives() -> bool:
    return (M / "Artist" / "Album" / "01.flac").exists()


print("spelled-rule", layout())

try:
    restore_album(lib, str(entry), trash_dir=T, origins_dir=O, protected=trees())
    print("guarded-restore RAN")
except ProtectedTreeError:
    print("guarded-restore REFUSED")
print("guarded-track-survives", track_survives())

# The same move with no protected identity at all, straight at the primitive.
try:
    move_no_merge(entry, origin)
    print("bare-move RAN")
except OSError as exc:
    print("bare-move REFUSED", exc.errno)
print("bare-track-survives", track_survives())
print("bare-origin-absent", not origin.exists())

# And through the whole restore with the guard switched off, so the primitive is
# measured where the loss actually happened.
try:
    restore_album(lib, str(entry), trash_dir=T, origins_dir=O, protected=bare())
    print("unguarded-restore RAN")
except ProtectedTreeError:
    print("unguarded-restore REFUSED")
except Exception as exc:
    print("unguarded-restore", type(exc).__name__)
print("unguarded-track-survives", track_survives())
