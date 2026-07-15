"""The python-plexapi seam.

``connect`` is the SOLE place a ``PlexServer`` is constructed, so unit tests
monkeypatch it to return a fake server and never touch the network. All
plexapi access for the app lives under ``app/plex/``.
"""

from __future__ import annotations

from typing import Any

from plexapi.server import PlexServer

from app.plex.errors import PlexConnectionError


def connect(base_url: str, token: str) -> Any:
    """Construct a PlexServer (network call in production; patched in tests)."""
    return PlexServer(base_url, token)


def music_section(server: Any, title: str = "") -> Any | None:
    """The music library section: the artist-type section titled ``title``
    (case-insensitive). When ``title`` is empty we fall back to the sole
    artist-type section, but REFUSE (``PlexConnectionError``) when more than one
    exists — silently taking the first would land on the wrong library in a
    two-library setup. None when nothing matches — the caller owns that message."""
    wanted = title.strip().casefold()
    artist_sections = [
        section
        for section in server.library.sections()
        if getattr(section, "TYPE", None) == "artist"
    ]
    if wanted:
        for section in artist_sections:
            if str(getattr(section, "title", "")).casefold() == wanted:
                return section
        return None
    if len(artist_sections) > 1:
        raise PlexConnectionError(
            "Multiple Plex music libraries found. Set the library section in Settings."
        )
    return artist_sections[0] if artist_sections else None
