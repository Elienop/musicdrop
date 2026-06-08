"""High-level slskd operations: the connection self-test.

The boundary that turns httpx (exception-throwing) into a typed
``SlskdConnection`` result. Makes HTTP requests ONLY through ``client.check``
(the patchable seam), so it is unit-tested with a stub and no network.
``test_connection`` NEVER raises — every failure becomes a friendly
``ok=False`` result the Settings panel can render.
"""

from __future__ import annotations

import httpx

from app.models.slskd import SlskdConnection
from app.slskd import client as client  # explicit re-export: the patchable seam (service.client)
from app.slskd.config import SlskdConfig


def test_connection(config: SlskdConfig) -> SlskdConnection:
    """Fetch the slskd version, or report a friendly error (never raises)."""
    if not (config.base_url and config.token):
        return SlskdConnection(ok=False, error="slskd is not configured.")
    try:
        version = client.check(config.base_url, config.token)
        return SlskdConnection(ok=True, version=version)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            return SlskdConnection(ok=False, error="slskd rejected the API key.")
        return SlskdConnection(ok=False, error="Couldn't reach the slskd server.")
    except httpx.HTTPError:
        # RequestError (connect/read/timeout) + any other transport-level error.
        return SlskdConnection(ok=False, error="Couldn't reach the slskd server.")
