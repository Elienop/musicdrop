"""Delete one of two albums that SHARE a real folder under two spellings.

On case-insensitive storage ``Art/ALB`` resolves to the existing ``Art/Alb``, so
a second album whose albumartist/album differs only in case lands inside the
first album's directory while its rows keep the other spelling. Two doors into
that state were measured on 2026-09-15 (``Keep both`` on the duplicate prompt,
and an edit retitling a same-artist album to a case variant); this probe builds
the END state by hand, which is the row/disk shape those measurements printed.

The released whole-folder Delete then lost the OTHER album's tracks: its
shared-folder check (``_folder_is_shared``, deleted along with that mover)
decided "shared" by byte-prefix over the row strings, rows spelled ``Art/ALB/…``
did not prefix-match the root ``Art/Alb``, so it answered "not shared" and one
``shutil.move`` took the single real directory — measured, ``dangling_rows = 2``.
``delete_album`` asks each item where IT lives, so the loss is unreachable
rather than guarded.

For ROWS. A cover BOTH twins track is one real file at one path, and
``Album.move`` carries ``album.artpath`` — so deleting either twin still takes
the other's cover: ``TWIN ART EXISTS False``, measured the same for the released
mover. Printed here as a characterization line, not a pin on a fix.

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

from beets import config
from beets.library import Item, Library

from app.beets.delete import delete_album
from app.beets.protected import protected_trees
from app.config import Settings

base = Path(sys.argv[1])
CF = base / "cf"
CF.mkdir(parents=True, exist_ok=True)


def _casefold_root() -> Path | None:
    """A directory on a casefolding tmpfs, or ``None`` if this box has none.

    The ``casefold=`` mount option needs ``CONFIG_TMPFS_UNICODE``/``CONFIG_UNICODE``
    and a kernel that carries the 12.1.0 tables; ``chattr +F`` is what turns the
    behaviour on per directory. Either can be absent, and both failures look the
    same to the caller: no case-insensitive directory.
    """
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


def _add(lib: Library, *, artist: str, album: str, folder: str, names: list[str]) -> Item:
    """One album, its files written at ``folder`` — the SPELLING is the fixture."""
    music = Path(os.fsdecode(lib.directory))
    at = music / folder
    at.mkdir(parents=True, exist_ok=True)
    items = []
    for n, name in enumerate(names, 1):
        f = at / name
        f.write_bytes(b"\x00")
        item = Item(album=album, albumartist=artist, artist=artist, title=f"T{n}", track=n)
        item.path = os.fsencode(str(f))
        items.append(item)
    lib.add_album(items).store()
    return items[0]


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
config["paths"]["default"] = "$albumartist/$album/$track $title"
lib = Library(str(root / "library.db"), directory=str(music))
# Both spellings, written in this order: the second album's files land INSIDE the
# first album's real directory while its rows keep ``ALB``.
_add(lib, artist="Art", album="Alb", folder="Art/Alb", names=["01 T1.mp3", "02 T2.mp3"])
twin = _add(lib, artist="Art", album="ALB", folder="Art/ALB", names=["01 S1.mp3", "02 S2.mp3"])
real_dirs = {os.path.dirname(os.fsdecode(i.path)) for a in lib.albums() for i in a.items()}
print("SPELLINGS", len(real_dirs))
print("ONE REAL FOLDER", (music / "Art" / "Alb").samefile(music / "Art" / "ALB"))

# ONE cover file, tracked by both albums under their own spelling of the folder.
cover = music / "Art" / "Alb" / "cover.jpg"
cover.write_bytes(b"\xff\xd8\xffcover")
for album in lib.albums():
    album.artpath = os.fsencode(str(music / "Art" / album.album / "cover.jpg"))
    album.store()
arts = [os.fsdecode(a.artpath) for a in lib.albums() if a.artpath]
print("ONE REAL COVER", len(arts) == 2 and os.path.samefile(arts[0], arts[1]))

target = next(a for a in lib.albums() if a.album == "Alb")
target_id = target.id
twin_album_id = twin.album_id
assert target_id is not None
assert twin_album_id is not None
trash = root / "trash"
origins = root / "trash-origins"
trees = protected_trees(
    settings=Settings(),
    music_dir=music,
    beets_dir=root,
    trash_dir=trash,
    origins_dir=origins,
    library_path=root / "library.db",
)
delete_album(lib, int(target_id), trash_dir=trash, origins_dir=origins, protected=trees)

# The loss, counted the way the 2026-09-18 map counted it: rows the library still
# has whose file is not on disk any more.
rows = [os.fsdecode(i.path) for a in lib.albums() for i in a.items()]
dangling = [p for p in rows if not os.path.exists(p)]
print("ROWS LEFT", len(rows))
print("DANGLING ROWS", len(dangling))
print("TWIN ALBUM LISTED", lib.get_album(int(twin_album_id)) is not None)
print("TARGET IN TRASH", len(list(trash.rglob("*.mp3"))))
# The half the per-item fix does NOT close: one file, two artpaths.
twin_album = lib.get_album(int(twin_album_id))
twin_art = os.fsdecode(twin_album.artpath) if twin_album and twin_album.artpath else ""
print("TWIN ART EXISTS", bool(twin_art) and os.path.exists(twin_art))
