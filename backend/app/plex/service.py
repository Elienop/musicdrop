"""High-level Plex operations: connection test + user discovery.

The boundary that turns python-plexapi (untyped, exception-throwing) into typed
Pydantic results + our ``PlexError`` types. Constructs servers ONLY through
``client.connect`` (the patchable seam), so this module is unit-tested with a
fake server and no network.
"""

from __future__ import annotations

from plexapi.exceptions import PlexApiException, Unauthorized
from requests.exceptions import RequestException

from app.models.plex import PlexConnection, PlexUserInfo
from app.plex import client
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
