"""Errors raised by the Plex adapter boundary."""

from __future__ import annotations


class PlexError(Exception):
    """Base for all Plex adapter failures."""


class PlexNotConfigured(PlexError):
    """The Plex base URL or admin token has not been set."""


class PlexConnectionError(PlexError):
    """Plex could not be reached or rejected the request (bad URL/token/network)."""
