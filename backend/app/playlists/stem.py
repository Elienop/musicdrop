"""The filename-stem helper shared by m3u import parsing and library matching.

Both sides derive a lookup/title key from a file path's basename, and the
filename tier of the match depends on them agreeing. Keeping the logic in one
beets-free module (importable from both ``app.playlists`` and ``app.beets``)
means the two can't drift: the only intentional difference — whether a leading
track number is stripped — is an explicit flag, not a duplicated regex.
"""

from __future__ import annotations

import re

# A leading "07 - " / "07." / "07_" / "07 " track number, kept to <=3 digits so a
# long numeric title isn't mistaken for one.
_LEADING_TRACK_NO = re.compile(r"\A\d{1,3}\s*[-._ ]\s*")


def filename_stem(path: str, *, strip_track_number: bool = False) -> str:
    """The basename of ``path`` with its extension dropped.

    Handles Windows separators/drive letters (``\\`` -> ``/``). With
    ``strip_track_number`` a leading track number and surrounding whitespace are
    removed too (the parse side derives a title); the match side leaves them in
    so the raw stem stays the key. Returns ``""`` when nothing remains.
    """
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    name = name.rsplit(".", 1)[0] if "." in name else name
    if strip_track_number:
        name = _LEADING_TRACK_NO.sub("", name).strip()
    return name
