"""The python-plexapi seam.

``connect`` is the SOLE place a ``PlexServer`` is constructed, so unit tests
monkeypatch it to return a fake server and never touch the network. All
plexapi access for the app lives under ``app/plex/``.
"""

from __future__ import annotations

from typing import Any

from plexapi.server import PlexServer


def connect(base_url: str, token: str) -> Any:
    """Construct a PlexServer (network call in production; patched in tests)."""
    return PlexServer(base_url, token)


def music_section(server: Any) -> Any | None:
    """Return the music library section (``TYPE == 'artist'``), or None."""
    for section in server.library.sections():
        if getattr(section, "TYPE", None) == "artist":
            return section
    return None
