"""slskd settings + connection test + the hardened inbound completion webhook.

The slskd config store is resolved from ``settings`` per request (like the Plex
panel) so it works under the lifespan-less test client. The API key + webhook
secret are write-only over the API — ``GET`` returns only ``has_token``, never
the values.

The webhook authenticates with a CONSTANT-TIME secret compare, in a DEPENDENCY
so it runs before FastAPI validates the body: an anonymous caller must not be
able to read the request contract off this endpoint by posting a wrong-shaped
body and reading the 422's field list. That is load-bearing beyond tidiness —
this path is exempt from the session gate (``app/auth/gate.py``), so its own
secret is the only thing standing in front of it.

``401`` is the only non-2xx an unauthenticated caller can obtain, with one
measured exception: a body that is not valid JSON at all still returns FastAPI's
generic ``json_invalid`` 422, because the body is parsed before dependencies are
solved. That 422 names no field of this schema — it says only "your JSON did not
parse" — so the contract stays closed. Past authentication, everything else — an
ignored event type, a missing/out-of-inbox path, auto-import disabled — is
acknowledged with ``200`` so slskd (fire-and-forget) never retry-storms; a
well-formed request body that fails validation is the remaining 422.
"""

from __future__ import annotations

import hmac
import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.acquisition.inbox import coalesce_album_root, contain
from app.config import SLSKD_STORE, settings, store_dir
from app.models.errors import ErrorDetail
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
    directory = store_dir(settings, SLSKD_STORE, Path(settings.beets_dir))
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
        has_webhook_secret=bool(config.webhook_secret),
    )


@router.get("/slskd/settings")
async def get_slskd_settings(
    store: Annotated[SlskdConfigStore, Depends(get_slskd_store)],
) -> SlskdSettings:
    return _to_settings(store.get())


@router.put("/slskd/settings")
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


@router.post("/slskd/test")
async def test_slskd(
    store: Annotated[SlskdConfigStore, Depends(get_slskd_store)],
) -> SlskdConnection:
    return await run_in_threadpool(service.test_connection, store.get())


def require_webhook_secret(
    request: Request,
    store: Annotated[SlskdConfigStore, Depends(get_slskd_store)],
) -> None:
    """Reject the request unless it carries the configured shared secret.

    A DEPENDENCY rather than the handler's first statement: FastAPI solves
    dependencies before it validates the request body, so this refuses an
    anonymous caller before a 422 could enumerate this webhook's fields for
    them. Load-bearing beyond tidiness — the path is exempt from the session
    gate, so this secret is the only thing in front of it.

    Both sides are compared as BYTES. ``hmac.compare_digest`` on ``str`` raises
    ``TypeError: comparing strings with non-ASCII characters is not supported``
    the moment either side leaves ASCII, and uvicorn decodes header bytes as
    latin-1 — so any client could put one non-ASCII character in ``X-API-Key``
    and turn this guard into an unhandled 500 (which, being synthesised outside
    all user middleware, carries no security headers either).
    ``surrogateescape`` on the caller-supplied side because that string may
    hold undecodable bytes, and a comparison must never be the thing that
    raises.
    """
    secret = store.get().webhook_secret
    provided = request.headers.get("X-API-Key") or request.headers.get("X-Webhook-Token") or ""
    # A webhook with no secret configured ALWAYS rejects (the user must set one).
    if not secret or not hmac.compare_digest(
        provided.encode("utf-8", "surrogateescape"), secret.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="invalid webhook token")


@router.post(
    "/slskd/webhook",
    dependencies=[Depends(require_webhook_secret)],
    responses={
        401: {
            "model": ErrorDetail,
            "description": (
                "The webhook was rejected because no webhook secret is "
                "configured or the provided API key does not match it."
            ),
        }
    },
)
async def slskd_webhook(
    event: SlskdWebhookEvent,
    request: Request,
    store: Annotated[SlskdConfigStore, Depends(get_slskd_store)],
) -> WebhookAck:
    # Checked AGAIN here, having already run as a route dependency above. Not an
    # oversight and not free-floating paranoia — the two placements buy
    # different things:
    #   * the dependency provides ORDERING (it runs before body validation, so a
    #     wrong-shaped body cannot answer 422 to an anonymous caller);
    #   * this call keeps the raise inside the ENDPOINT's call graph, which is
    #     what `tests/test_route_status_declarations.py` walks to check that
    #     every status a route can return is declared in the contract. That scan
    #     starts at the endpoint function and never reads decorator or parameter
    #     defaults, so a 401 living only in the dependency is invisible to it —
    #     measured: moving it out turned that guard red, naming this module.
    # It also means deleting the decorator's `dependencies=[...]` downgrades the
    # ordering without reopening the endpoint. The cost is one extra
    # constant-time compare of a short secret, on a fire-and-forget webhook.
    require_webhook_secret(request, store)

    config = store.get()
    if event.type != "DownloadDirectoryComplete" or not event.localDirectoryName:
        return WebhookAck(status="ignored")
    if not config.auto_import:
        # The signal is acknowledged, but auto-import is off -> the user imports
        # this drop manually. (200, never 4xx, so slskd does not retry-storm.)
        return WebhookAck(status="ignored")

    inbox_dir: Path = request.app.state.inbox_dir
    remapped = service.remap_to_inbox(event.localDirectoryName, config.downloads_prefix, inbox_dir)
    # ``strict=True`` also rejects an EMPTY remainder (localDirectoryName ==
    # downloads_prefix, or "/" under the default empty prefix) that remaps to the
    # inbox ROOT — a whole-inbox MOVE would sweep in unrelated/still-downloading
    # siblings (and the ledger); only a strict descendant is a valid album target.
    # contain (resolve + lstat), coalesce_album_root (parent.iterdir) and enqueue
    # (resolve + ledger stat) all do blocking filesystem I/O — offload them so the
    # webhook handler never stalls the event loop (and the SSE stream) on a slow
    # NAS scan.
    contained = await run_in_threadpool(contain, remapped, inbox_dir, strict=True)
    if contained is None:
        logger.warning(
            "slskd webhook: %r maps outside the inbox (or to its root); ignored",
            event.localDirectoryName,
        )
        return WebhookAck(status="ignored")

    album = await run_in_threadpool(coalesce_album_root, contained, inbox_dir)
    await run_in_threadpool(request.app.state.acquisition_queue.enqueue, album)
    return WebhookAck(status="queued")
