"""Pure path translation between the beets library root and Plex's view.

The user configures the music-library path AS PLEX SEES IT (often different from
the app's mount under Docker). A track's beets absolute path is rebased onto the
Plex root by preserving its path relative to the beets library root. An empty
Plex root means the two share a mount, so the path passes through unchanged.
"""

from __future__ import annotations

import os


def translate_path(beets_path: str, beets_root: str, plex_root: str) -> str:
    if not plex_root:
        return beets_path
    rel = os.path.relpath(beets_path, beets_root)
    return os.path.normpath(os.path.join(plex_root, rel))
