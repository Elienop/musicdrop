"""Pydantic contract for the cover-art install endpoint."""

from __future__ import annotations

from pydantic import BaseModel


class CoverInstallResult(BaseModel):
    """Outcome of installing an album cover (set artpath + optional embed).

    ``embedded`` is True only when the user's config enables ``embedart`` and
    the image is jpeg/png; ``embed_detail`` explains why it was skipped.
    """

    ok: bool
    embedded: bool
    embed_detail: str | None = None
    message: str | None = None
