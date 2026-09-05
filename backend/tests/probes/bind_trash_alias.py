"""``-v /srv/music:/music`` plus ``-v /srv/music/bin:/trash``: one dir, two paths.

The alias that makes the orphan finder's exclusion an IDENTITY question rather
than a string one. Run under ``unshare -Urm`` by :func:`tests._mountns.run_probe`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app.beets.orphans import find_orphan_folders

base = Path(sys.argv[1])
src = base / "srv" / "music"
(src / "Real").mkdir(parents=True)
(src / "Real" / "01.flac").write_bytes(b"x")
(src / "Old Artist").mkdir()
(src / "Old Artist" / "poster.jpg").write_bytes(b"x")
(src / "bin").mkdir()
(src / "bin" / "note.txt").write_bytes(b"x")
mnt = base / "music"
mnt.mkdir()
trash = base / "trash"
trash.mkdir()
subprocess.run(["mount", "--bind", str(src), str(mnt)], check=True)
subprocess.run(["mount", "--bind", str(src / "bin"), str(trash)], check=True)
assert (mnt / "bin").samefile(trash), "the fixture did not alias the Trash"
print(sorted(p.name for p in find_orphan_folders(mnt, seeds=None, trash_dir=trash)))
