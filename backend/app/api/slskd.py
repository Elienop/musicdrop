"""slskd settings + connection test endpoints (the webhook lands in Task 4.5).

The slskd config store is resolved from ``settings`` per request (like the Plex
panel) so it works under the lifespan-less test client. The API key + webhook
secret are write-only over the API — ``GET`` returns only ``has_token``, never
the values.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from app.config import settings
from app.models.slskd import (
    SlskdConnection,
    SlskdSettings,
    SlskdSettingsUpdate,
)
from app.slskd import service
from app.slskd.config import SlskdConfig, SlskdConfigStore

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
