"""High-level slskd operations: the connection self-test + mapping slskd's folder.

The boundary that turns httpx (exception-throwing) into a typed
``SlskdConnection`` result. Makes HTTP requests ONLY through ``client.check``
(the patchable seam), so it is unit-tested with a stub and no network.
``test_connection`` NEVER raises — every failure becomes a friendly
``ok=False`` result the Settings panel can render.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

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


def remap_to_inbox(local_dir: str, downloads_prefix: str, inbox_dir: Path) -> Path | None:
    """slskd's reported folder -> the same folder as MusicDrop sees it, or ``None``.

    ``downloads_prefix`` is "Path in slskd": slskd's download folder as slskd
    sees it. Empty means both containers see the same path, so the reported
    folder is used as it is. Set, the reported folder must sit inside it by
    WHOLE folder names (``PurePosixPath.relative_to`` compares parts and raises
    on a miss), and the part inside is put under ``inbox_dir``. A miss is
    ``None``, never re-rooted: ``/app/downloads2/X`` is not inside
    ``/app/downloads``. Purely syntactic: ``contain`` stays the one check that
    the result sits strictly inside the inbox once ``..`` and symlinks resolve,
    which refuses the prefix itself (``.``), a ``..`` part and a symlink out.
    """
    if not downloads_prefix:
        return Path(local_dir)
    try:
        inside = PurePosixPath(local_dir).relative_to(PurePosixPath(downloads_prefix))
    except ValueError:
        return None
    return inbox_dir / inside
