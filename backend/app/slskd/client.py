"""The slskd HTTP seam.

``check`` is the SOLE place an slskd HTTP request is made, so unit tests
monkeypatch it (``service.client.check``) — or stub ``httpx.get`` — and never
touch the network. All slskd HTTP access for the app lives under ``app/slskd/``.
"""

from __future__ import annotations

import httpx


def check(base_url: str, token: str) -> str:
    """GET the slskd version (a network call in production; patched in tests).

    Hits ``/api/v0/application/version`` with the ``X-API-Key`` header and returns
    the reported version string. Raises ``httpx.HTTPStatusError`` on a non-2xx
    (401/403 = a bad API key) and ``httpx.RequestError`` when the server is
    unreachable — the service layer turns both into a friendly result.
    """
    response = httpx.get(
        f"{base_url}/api/v0/application/version",
        headers={"X-API-Key": token},
        timeout=10.0,
    )
    response.raise_for_status()
    # slskd returns the version as a JSON string (``Ok(SemanticVersion)``); fall
    # back to the raw body if a future build sends plain text.
    try:
        return str(response.json()).strip()
    except ValueError:
        return response.text.strip()
