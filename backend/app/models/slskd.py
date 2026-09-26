"""slskd settings + connection + inbound-webhook contract.

The ``SlskdWebhookEvent`` fields are camelCase to mirror slskd's webhook payload
wire format (``localDirectoryName`` etc.) — accepted as-is so no alias mapping is
needed; extra payload keys (``id``/``timestamp``/``version``) are ignored by
Pydantic's default ``extra="ignore"``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, StringConstraints


class SlskdSettings(BaseModel):
    """GET /slskd/settings — secrets are never returned, only ``has_*`` flags.

    ``has_token`` / ``has_webhook_secret`` let the panel show a "saved — enter to
    replace" placeholder for each write-only secret without ever exposing the
    value.

    ``downloads_prefix`` is "Path in slskd": slskd's download folder as slskd
    sees it. A webhook folder inside it, by whole folder names, is read under
    slskd's folder as MusicDrop sees it; empty means both see the same path. A
    folder outside it is refused, not re-rooted.

    ``last_download_missed`` is true when the last slskd webhook that reached the
    mapping was refused because its folder did not map into slskd's folder, and
    false once one maps. Other event types, and deliveries while auto-import is
    off, do not count. It lives in memory: false after a restart until the next
    miss.

    ``folder`` is slskd's folder as MusicDrop sees it (``MUSICDROP_INBOX_DIR``),
    read-only, and ``folder_exists`` is whether it is a folder right now.
    """

    base_url: str
    folder: str
    folder_exists: bool
    downloads_prefix: str
    auto_import: bool
    has_token: bool
    has_webhook_secret: bool
    last_download_missed: bool


class SlskdSettingsUpdate(BaseModel):
    """PUT body — any omitted field is left unchanged; token/secret are write-only."""

    base_url: str | None = None
    downloads_prefix: str | None = None
    auto_import: bool | None = None
    token: str | None = None
    webhook_secret: str | None = None


class SlskdConnection(BaseModel):
    """POST /slskd/test — the reported version on success, a friendly error otherwise."""

    ok: bool
    version: str | None = None
    error: str | None = None


class SlskdWebhookEvent(BaseModel):
    """The inbound slskd completion webhook payload (camelCase wire fields)."""

    type: str
    # 4096 characters is PATH_MAX and admits every folder slskd can report (a
    # path has no more characters than bytes). Unbounded, the remap's
    # ``relative_to`` is quadratic in the part count and runs on the event
    # loop: 7.0 s at 128 KB, 9.6 ms at this cap (measured 2026-09-26).
    localDirectoryName: Annotated[str, StringConstraints(max_length=4096)] | None = None
    remoteDirectoryName: str | None = None
    username: str | None = None


class WebhookAck(BaseModel):
    """The webhook's typed 2xx body. ``401`` is the only non-2xx it ever returns."""

    status: Literal["queued", "ignored"]
