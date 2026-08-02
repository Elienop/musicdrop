# backend/app/beets/sidecars.py
"""The lyric-sidecar naming contract, and carrying sidecars when audio moves.

MusicDrop writes Plex-readable lyric sidecars NEXT TO the audio file, named after
the audio file's stem (``01 Song.flac`` -> ``01 Song.lrc``). Two features depend
on that one rule — :mod:`app.beets.lyrics` writes and removes them,
:mod:`app.beets.reorganize` carries them along when beets moves the audio — so
the extension pair and the stem derivation live HERE and nowhere else. A second
copy would drift, and the failure mode is silent data loss: beets moves audio +
album art only, so a sidecar left behind sits in a now audio-empty folder that
the post-reorganize orphan sweep moves to Trash.

Leaf module by design: pure path/filesystem work, no beets import, so every
caller inside the adapter can depend on it without a cycle.
"""

from __future__ import annotations

import logging
import os
import shutil

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

    Never clobbers: a file already at the destination sidecar path wins and the
    source is left where it is. The two are different lyrics for what is now the
    same track name, and keeping one recoverable beats silently destroying
    either — note "recoverable", not "in place": when the skip leaves the source
    in a fully vacated folder, the post-run orphan sweep still moves that folder
    to Trash, so the kept sidecar survives in Trash rather than beside the track.
    The exists-check is a guard, not a lock — reorganize holds the single-slot
    library mutex, so nothing else is writing sidecars concurrently.
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
        # lexists, not exists: a broken symlink still occupies the name.
        if not os.path.lexists(src):
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
