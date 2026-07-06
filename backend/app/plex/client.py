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


def music_section(server: Any, title: str = "") -> Any | None:
    """The music library section: the artist-type section titled ``title``
    (case-insensitive), or the FIRST artist-type section when ``title`` is
    empty. None when nothing matches — the caller owns the error message."""
    wanted = title.strip().casefold()
    for section in server.library.sections():
        if getattr(section, "TYPE", None) != "artist":
            continue
        if not wanted or str(getattr(section, "title", "")).casefold() == wanted:
            return section
    return None
