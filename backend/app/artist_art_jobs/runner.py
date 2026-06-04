"""Artist-art backfill worker — sequential sweep over async image fetches.

The poster/background fetch is async (httpx), so the daemon thread runs its OWN
event loop via asyncio.run(). sweep_async builds its OWN httpx.AsyncClient +
ArtistImageService over the SHARED disk cache dir (so the manual override +
cached positives are seen) + a fanart background source. The app.state
service/client are bound to the MAIN loop and must NOT be reused here.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.artist_art_jobs.registry import ArtistArtBackfillRegistry
from app.artwork.cache import ArtistImageCache
from app.artwork.factory import build_fanart_background_source, build_source_chain
from app.artwork.rate_limit import TokenBucketLimiter
from app.artwork.service import ArtistImageService
from app.artwork.source import TransientSourceError
from app.beets.artist_art import write_artist_art
from app.beets.library import get_artist_mbid, list_artists
from app.models.artist_art import ArtistArtOutcome


async def _default_fetch_one(
    service: ArtistImageService, bg_source: Any, lib: Any, name: str, *, force: bool
) -> ArtistArtOutcome:
    mbid = await asyncio.to_thread(get_artist_mbid, lib, name)
    poster = await service.get_artist_image(name, get_mbid=lambda: mbid)
    background: tuple[bytes, str] | None = None
    if bg_source is not None and mbid:
        try:
            bg = await bg_source.resolve_background(mbid)
        except TransientSourceError:
            bg = None
        if bg is not None:
            background = (bg.data, bg.content_type)
    return await asyncio.to_thread(
        write_artist_art, lib, name, poster=poster, background=background, force=force
    )


async def sweep_async(
    reg: ArtistArtBackfillRegistry,
    lib: Any,
    *,
    cache_dir: Any,
    settings: Any,
    delay: float,
    force: bool,
    artist: str | None = None,
    names: list[str] | None = None,
    fetch_one: Callable[[str], Awaitable[ArtistArtOutcome]] | None = None,
) -> None:
    """Run the (library- or single-artist-scoped) sweep to completion. Never raises."""
    try:
        if names is None:
            names = (
                [artist]
                if artist is not None
                else [a.name for a in await asyncio.to_thread(list_artists, lib)]
            )
        reg.set_total(len(names))
        if fetch_one is not None:
            await _run_loop(reg, names, fetch_one, delay)
            return
        async with httpx.AsyncClient(
            timeout=10.0,
            headers={
                "User-Agent": f"{settings.app_name}/{settings.version} (+https://github.com/Elienop/musicdrop)"
            },
        ) as client:
            service = ArtistImageService(
                source=build_source_chain(client, settings),
                cache=ArtistImageCache(cache_dir),
                limiter=TokenBucketLimiter(
                    rate_per_sec=settings.artist_image_rate_per_sec,
                    max_concurrency=settings.artist_image_max_concurrency,
                ),
                is_enabled=lambda: True,  # the write-gate already passed at the endpoint
                negative_ttl_seconds=settings.artist_image_negative_ttl_seconds,
                transient_ttl_seconds=settings.artist_image_transient_ttl_seconds,
            )
            bg_source = build_fanart_background_source(client, settings)

            async def real(name: str) -> ArtistArtOutcome:
                return await _default_fetch_one(service, bg_source, lib, name, force=force)

            await _run_loop(reg, names, real, delay)
    except Exception as exc:  # any crash becomes a failed job, never a lost thread
        reg.fail(str(exc) or exc.__class__.__name__)


async def _run_loop(
    reg: ArtistArtBackfillRegistry,
    names: list[str],
    fetch_one: Callable[[str], Awaitable[ArtistArtOutcome]],
    delay: float,
) -> None:
    for name in names:
        if reg.should_stop():
            reg.finish("stopped")
            return
        reg.set_current(name)
        reg.record(await fetch_one(name))
        if delay:
            await asyncio.sleep(delay)
    reg.finish("done")


def start_backfill(
    reg: ArtistArtBackfillRegistry,
    lib: Any,
    *,
    cache_dir: Any,
    settings: Any,
    delay: float,
    force: bool,
    artist: str | None = None,
) -> None:
    """Spawn the sweep on a daemon thread that owns its event loop (non-blocking)."""
    threading.Thread(
        target=lambda: asyncio.run(
            sweep_async(
                reg,
                lib,
                cache_dir=cache_dir,
                settings=settings,
                delay=delay,
                force=force,
                artist=artist,
            )
        ),
        name="musicdrop-artist-art-backfill",
        daemon=True,
    ).start()
