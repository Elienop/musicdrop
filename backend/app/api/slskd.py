"""slskd settings + connection test + the hardened inbound completion webhook.

The slskd config store is resolved from ``settings`` per request (like the Plex
panel) so it works under the lifespan-less test client. The API key + webhook
secret are write-only over the API — ``GET`` returns only ``has_token``, never
the values.

The webhook authenticates with a CONSTANT-TIME secret compare (``401`` is the
ONLY non-2xx it ever returns); everything else — an ignored event type, a
missing/out-of-inbox path, auto-import disabled — is acknowledged with ``200`` so
slskd (fire-and-forget) never retry-storms.
"""

from __future__ import annotations

import hmac
import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.acquisition.inbox import coalesce_album_root, contain
from app.config import settings
from app.models.slskd import (
    SlskdConnection,
    SlskdSettings,
    SlskdSettingsUpdate,
    SlskdWebhookEvent,
    WebhookAck,
)
from app.slskd import service
from app.slskd.config import SlskdConfig, SlskdConfigStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["slskd"])


def get_slskd_store() -> SlskdConfigStore:
    base = settings.slskd_settings_dir.strip()
    directory = Path(base) if base else Path(settings.beets_dir) / "slskd"
    env = SlskdConfig(
        base_url=settings.slskd_url,
        token=settings.slskd_token,
        downloads_prefix=settings.slskd_downloads_prefix,
        webhook_secret=settings.slskd_webhook_secret,
        auto_import=settings.slskd_auto_import,
    )
    return SlskdConfigStore(directory / "slskd.json", env_defaults=env)


def _to_settings(config: SlskdConfig) -> SlskdSettings:
    return SlskdSettings(
        base_url=config.base_url,
        downloads_prefix=config.downloads_prefix,
        auto_import=config.auto_import,
        has_token=bool(config.token),
    )


@router.get("/slskd/settings", response_model=SlskdSettings)
async def get_slskd_settings(
    store: Annotated[SlskdConfigStore, Depends(get_slskd_store)],
) -> SlskdSettings:
    return _to_settings(store.get())


@router.put("/slskd/settings", response_model=SlskdSettings)
async def put_slskd_settings(
    body: SlskdSettingsUpdate,
    store: Annotated[SlskdConfigStore, Depends(get_slskd_store)],
) -> SlskdSettings:
    config = await run_in_threadpool(
        store.update,
        base_url=body.base_url,
        token=body.token,
        downloads_prefix=body.downloads_prefix,
        webhook_secret=body.webhook_secret,
        auto_import=body.auto_import,
    )
    return _to_settings(config)


@router.post("/slskd/test", response_model=SlskdConnection)
async def test_slskd(
    store: Annotated[SlskdConfigStore, Depends(get_slskd_store)],
) -> SlskdConnection:
    return await run_in_threadpool(service.test_connection, store.get())


def _remap_to_inbox(local_dir: str, downloads_prefix: str, inbox_dir: Path) -> str:
    """slskd's container path -> a host path rooted under ``inbox_dir``.

    Strips the configured downloads prefix (slskd's namespace) and re-roots the
    remainder under the inbox. Purely syntactic — ``contain`` remains the
    authority that rejects a ``../`` escape after the remap, and ``lstrip('/')``
    keeps the join from producing an absolute path that ignores the inbox root.
    """
    remainder = local_dir.removeprefix(downloads_prefix) if downloads_prefix else local_dir
    return str(inbox_dir / remainder.lstrip("/"))


@router.post("/slskd/webhook", response_model=WebhookAck)
async def slskd_webhook(
    event: SlskdWebhookEvent,
    request: Request,
    store: Annotated[SlskdConfigStore, Depends(get_slskd_store)],
) -> WebhookAck:
    config = store.get()
    provided = request.headers.get("X-API-Key") or request.headers.get("X-Webhook-Token")
    secret = config.webhook_secret
    # Constant-time compare; a webhook with no secret configured ALWAYS rejects
    # (the user must set one). 401 is the only non-2xx this endpoint returns.
    if not secret or not hmac.compare_digest(provided or "", secret):
        raise HTTPException(status_code=401, detail="invalid webhook token")

    if event.type != "DownloadDirectoryComplete" or not event.localDirectoryName:
        return WebhookAck(status="ignored")
    if not config.auto_import:
        # The signal is acknowledged, but auto-import is off -> the user imports
        # this drop manually. (200, never 4xx, so slskd does not retry-storm.)
        return WebhookAck(status="ignored")

    inbox_dir: Path = request.app.state.inbox_dir
    remapped = _remap_to_inbox(event.localDirectoryName, config.downloads_prefix, inbox_dir)
    contained = contain(remapped, inbox_dir)
    if contained is None:
        logger.warning(
            "slskd webhook: %r maps outside the inbox; ignored", event.localDirectoryName
        )
        return WebhookAck(status="ignored")

    album = coalesce_album_root(contained, inbox_dir)
    request.app.state.acquisition_queue.enqueue(album)
    return WebhookAck(status="queued")
