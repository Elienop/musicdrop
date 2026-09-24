# backend/app/beets/sidecars.py
"""The lyric-sidecar naming contract, and carrying sidecars when audio moves.

MusicDrop writes Plex-readable lyric sidecars NEXT TO the audio file, named after
the audio file's stem (``01 Song.flac`` -> ``01 Song.lrc``). Three features
depend on that rule — :mod:`app.beets.lyrics` writes and removes them,
:mod:`app.beets.reorganize` and :mod:`app.beets.delete` carry them when beets
moves the audio — so the extension pair, the stem derivation and the carry live
HERE. The failure mode of a second copy is silent data loss: beets moves audio +
album art only, so a sidecar left behind sits in a now audio-empty folder that
the post-reorganize orphan sweep moves to Trash.

:func:`carry_sidecars` lives here rather than in ``reorganize`` so that
``delete`` does not import it: this module stays a leaf in the APP graph (it
imports no ``app.beets`` module), which is what its callers depend on.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
from typing import Any

import beets
from beets.util import prune_dirs

_log = logging.getLogger(__name__)

#: Timestamped lyrics (LRCLib synced) — what Plex prefers.
SYNCED_EXT = ".lrc"
#: Plain lyrics, timestamps stripped.
PLAIN_EXT = ".txt"
#: Every extension MusicDrop may have written beside a track. The ONE definition.
SIDECAR_EXTS: tuple[str, ...] = (SYNCED_EXT, PLAIN_EXT)


def sidecar_base(audio_path: str | bytes | None) -> str | None:
    """An audio path minus its extension — the stem sidecars are named after.

    Accepts beets' bytes paths. An absent/empty path yields None so callers
    no-op instead of building a garbage path out of ``""``.
    """
    if not audio_path:
        return None
    base, _ext = os.path.splitext(os.fsdecode(audio_path))
    return base or None


def move_sidecars(old_audio: str | bytes | None, new_audio: str | bytes | None) -> list[str]:
    """Carry a track's lyric sidecars from beside ``old_audio`` to beside
    ``new_audio``; return the destination paths written.

    Best-effort and never raises. The audio file has ALREADY moved by the time
    this runs, so no sidecar problem may rewrite that outcome — every skip and
    every error is logged instead, at WARNING with the traceback.

    Never clobbers: a file already at the destination wins and the source stays
    where it is — in a vacated folder that makes it the orphan sweep's, so the
    kept sidecar ends up in Trash.

    A source that is not a REGULAR file is skipped: measured, a DIRECTORY named
    ``01 Song.lrc`` was otherwise moved wholesale with its contents
    (``test_only_a_regular_file_at_the_sidecar_name_is_carried``).
    """
    old_base = sidecar_base(old_audio)
    new_base = sidecar_base(new_audio)
    # Same stem = the audio did not actually move; without this the sidecar would
    # be seen sitting at its own destination and reported as a collision.
    if old_base is None or new_base is None or old_base == new_base:
        return []
    moved: list[str] = []
    for ext in SIDECAR_EXTS:
        src, dst = old_base + ext, new_base + ext
        # lstat, not exists: a broken symlink still occupies the name, and the
        # mode is what says whether this is a sidecar at all.
        try:
            entry = os.lstat(src)
        except OSError:
            continue
        if not stat.S_ISREG(entry.st_mode):
            _log.warning("lyric sidecar kept, not a regular file: %s", src)
            continue
        if os.path.lexists(dst):
            _log.warning("lyric sidecar kept, destination exists: %s -> %s", src, dst)
            continue
        try:
            # shutil.move, not os.replace: a library can span filesystems.
            shutil.move(src, dst)
        except OSError:
            _log.warning("lyric sidecar move failed: %s -> %s", src, dst, exc_info=True)
            continue
        moved.append(dst)
    return moved


def carry_sidecars(lib: Any, old_path: bytes | str, new_path: bytes | str) -> None:
    """Move ONE relocated item's lyric sidecars to its new location, then re-prune.

    beets moves audio + album art and nothing else, so MusicDrop's own
    ``.lrc``/``.txt`` sidecars (the files Plex reads) stay in the vacated folder —
    which the post-run orphan sweep classifies as an audio-empty husk and moves
    to Trash, losing the lyrics while the job reports success. An in-place rename
    strands them the same way, with no husk at all.

    Keyed off the ACTUAL landing path rather than the computed destination, so a
    collision-diverted ``.1`` file keeps its lyrics.

    The re-prune is not cosmetic: beets prunes the vacated dir DURING the move,
    while the sidecars are still in it, so that prune is a no-op and the now-empty
    dir would outlive every future sweep (``find_orphan_folders`` ignores empty
    dirs). Pruned again with beets' own arguments, including the user's
    ``clutter:`` list, which the function default does not match (``Thumbs.db``
    against the config default's ``Thumbs.DB``, case-sensitive on Linux).

    Never raises: ``prune_dirs`` wraps only its ``rmtree`` (beets
    ``util/__init__.py:343``), so its ``os.listdir`` of an unreadable ancestor
    propagates, and the audio has already moved by the time this runs — pinned by
    ``tests/test_delete.py::test_a_prune_that_raises_does_not_fail_the_delete``.
    """
    if not move_sidecars(old_path, new_path):
        return
    directory = os.path.dirname(os.fsencode(old_path))
    try:
        prune_dirs(directory, lib.directory, clutter=beets.config["clutter"].as_str_seq())
    except OSError:
        _log.warning("pruning vacated dir failed: %r", directory, exc_info=True)
