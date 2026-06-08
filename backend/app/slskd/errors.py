"""Errors raised by the slskd adapter boundary."""

from __future__ import annotations


class SlskdError(Exception):
    """Base for all slskd adapter failures."""


class SlskdNotConfigured(SlskdError):
    """The slskd base URL or API key has not been set."""


class SlskdConnectionError(SlskdError):
    """slskd could not be reached or rejected the request (bad URL/key/network)."""
