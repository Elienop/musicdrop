"""What counts as an audio file — beets' own answer, isolated here (rule 3).

The bank fingerprint hashes only audio so stray sidecars (cover art, .DS_Store,
Thumbs.db, .lrc/.txt) written into a banked folder after banking never stale a
row. "Audio" is beets' call: it imports through ``mediafile``, whose ``TYPES``
names every format it reads. Several of those live in a differently-named
container on disk (AAC/ALAC in .m4a/.m4b/.mp4, ASF in .wma, Ogg in .oga, DSD in
.dff, AIFF in .aif/.aifc), so the container suffixes are added explicitly.
Suffixes are compared case-insensitively.
"""

from __future__ import annotations

import os

from mediafile import TYPES

# Formats whose mediafile name IS the on-disk extension (mp3, flac, ogg, ...).
_FORMAT_EXTENSIONS = {f".{name}" for name in TYPES}
# Containers mediafile/mutagen read that TYPES names by codec, not by suffix.
_CONTAINER_EXTENSIONS = {".m4a", ".m4b", ".mp4", ".wma", ".oga", ".dff", ".aif", ".aifc"}

AUDIO_EXTENSIONS: frozenset[str] = frozenset(_FORMAT_EXTENSIONS | _CONTAINER_EXTENSIONS)


def is_audio_file(name: str) -> bool:
    """True when ``name``'s suffix is an audio extension beets would import."""
    return os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS
