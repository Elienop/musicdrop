"""High-level Plex operations: connection test + user discovery.

The boundary that turns python-plexapi (untyped, exception-throwing) into typed
Pydantic results + our ``PlexError`` types. Constructs servers ONLY through
``client.connect`` (the patchable seam), so this module is unit-tested with a
fake server and no network.
"""

from __future__ import annotations

from plexapi.exceptions import PlexApiException, Unauthorized
from requests.exceptions import RequestException

from app.models.plex import PlexConnection, PlexSectionInfo, PlexUserInfo
from app.plex import client as client  # explicit re-export: the patchable seam (service.client)
from app.plex.config import PlexConfig
from app.plex.errors import PlexConnectionError, PlexNotConfigured


def _friendly(exc: Exception) -> str:
    if isinstance(exc, Unauthorized):
        return "Plex rejected the token."
    if isinstance(exc, RequestException):
        return "Couldn't reach the Plex server."
    return "Plex request failed."


def test_connection(config: PlexConfig) -> PlexConnection:
    """Connect and report the server name, or a friendly error (never raises)."""
    if not (config.base_url and config.token):
        return PlexConnection(ok=False, error="Plex is not configured.")
    try:
        server = client.connect(config.base_url, config.token)
        name = str(getattr(server, "friendlyName", "") or "Plex")
        return PlexConnection(ok=True, server_name=name)
    except (PlexApiException, RequestException) as exc:
        return PlexConnection(ok=False, error=_friendly(exc))


def discover_users(config: PlexConfig) -> list[PlexUserInfo]:
    """List the Plex accounts (for the per-playlist target picker)."""
    if not (config.base_url and config.token):
        raise PlexNotConfigured("Plex is not configured.")
    try:
        server = client.connect(config.base_url, config.token)
        account = server.myPlexAccount()
        return [
            PlexUserInfo(
                id=str(user.id),
                name=str(
                    getattr(user, "title", "")
                    or getattr(user, "username", "")
                    or getattr(user, "email", "")
                    or user.id
                ),
                home=bool(getattr(user, "home", False)),
            )
            for user in account.users()
        ]
    except (PlexApiException, RequestException) as exc:
        raise PlexConnectionError(_friendly(exc)) from exc


def _section_locations(section: object) -> list[str]:
    """The folder paths Plex reports for a section, defensively coerced.

    ``LibrarySection.locations`` is built from the ``<Location>`` children the
    server already sent with the section listing, so reading it costs no extra
    request. It is still read off whatever arrived: a section listed without any
    degrades to "no folders known" rather than breaking the whole listing.
    """
    raw = getattr(section, "locations", None)
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(path) for path in raw if str(path)]


def list_music_sections(config: PlexConfig) -> list[PlexSectionInfo]:
    """The server's music (artist-type) sections with their folders, in server order."""
    if not (config.base_url and config.token):
        raise PlexNotConfigured("Plex is not configured.")
    try:
        server = client.connect(config.base_url, config.token)
        return [
            PlexSectionInfo(
                title=str(getattr(section, "title", "")),
                locations=_section_locations(section),
            )
            for section in server.library.sections()
            if getattr(section, "TYPE", None) == "artist"
        ]
    except (PlexApiException, RequestException) as exc:
        raise PlexConnectionError(_friendly(exc)) from exc
