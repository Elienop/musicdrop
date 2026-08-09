from typing import Annotated, Literal, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.api.albums import get_library
from app.api.csrf import verify_upload_origin
from app.api.http_cache import (
    if_none_match_hit,
    image_response,
    not_modified,
    revalidating_image_response,
)
from app.artist_art_jobs.registry import (
    ArtistArtBackfillRegistry,
    get_artist_art_backfill,
)
from app.artist_art_jobs.runner import start_backfill as start_art_backfill
from app.artwork.cache import ArtistImageCache
from app.artwork.degrade import derive_thumb_or_degrade
from app.artwork.download import fetch_image_bytes
from app.artwork.factory import (
    FANARTTV,
    ArtistImageSources,
    build_artist_image_sources,
    label_for,
)
from app.artwork.images import MAX_IMAGE_BYTES, sniff_image_mime
from app.artwork.service import ArtistImageService
from app.artwork.toggle import ArtistArtWriteToggle, ArtistImageToggle
from app.beets import library as beets_library
from app.beets.delete import delete_artist_op
from app.beets.library import LibraryHandle, list_artists
from app.config import resolve_artist_image_cache_dir
from app.config import settings as _module_settings
from app.events.emit import emit_art_changed, emit_library_changed
from app.library_busy import raise_if_library_busy
from app.models.artist import (
    Artist,
    ArtistImageOverrideResult,
    ArtistImageSettings,
    ArtistImageSourceId,
    ArtistImageSourceList,
    ArtistImageSourceOption,
    ArtistImageUrlOverride,
)
from app.models.artist_art import ArtistArtBackfillStatus, ArtistArtWriteSettings
from app.models.delete import DeleteResult

router = APIRouter(tags=["artists"])


def get_artist_image_service(request: Request) -> ArtistImageService:
    """Return the process-wide artist-image service built at startup.

    Constructed once in the app lifespan and stored on
    ``app.state.artist_image_service``. Overridden in tests to inject a stub so
    no real Deezer call is made.
    """
    service: ArtistImageService = request.app.state.artist_image_service
    return service


def get_artist_image_toggle(request: Request) -> ArtistImageToggle:
    """The process-wide persisted enabled toggle, built in the app lifespan."""
    toggle: ArtistImageToggle = request.app.state.artist_image_toggle
    return toggle


def get_artist_image_cache(request: Request) -> ArtistImageCache:
    """The process-wide artist-image disk cache, built in the app lifespan."""
    cache: ArtistImageCache = request.app.state.artist_image_cache
    return cache


def get_artist_image_http_client(request: Request) -> httpx.AsyncClient:
    """The shared artist-image httpx client (built in the lifespan). Overridable in tests."""
    client: httpx.AsyncClient = request.app.state.artist_image_http_client
    return client


def get_artist_image_sources(request: Request) -> ArtistImageSources:
    """The process-wide artist-image source registry, built in the lifespan.

    Falls back to a fresh registry over the module settings when unset:
    ``TestClient(app)`` skips the lifespan, so a hard ``request.app.state``
    read would make every test that touches these routes lifespan-dependent
    (the same reason ``get_cover_thumb_cache`` has a fallback). The fallback
    builds its own httpx client, which is correct for a test and never reached
    in production - the lifespan always sets this attribute.
    """
    sources: ArtistImageSources | None = getattr(request.app.state, "artist_image_sources", None)
    if sources is None:
        sources = build_artist_image_sources(httpx.AsyncClient(), _module_settings)
    return sources


def get_artist_art_write_toggle(request: Request) -> ArtistArtWriteToggle:
    """The process-wide persisted write-to-library toggle, built in the lifespan."""
    toggle: ArtistArtWriteToggle = request.app.state.artist_art_write_toggle
    return toggle


@router.get("/artists", response_model=list[Artist])
async def list_artists_endpoint(
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> list[Artist]:
    return await run_in_threadpool(list_artists, handle.lib)


@router.get(
    "/artists/image",
    responses={
        200: {"content": {"image/*": {}}, "description": "The artist portrait."},
        404: {"description": "Feature disabled, no verified match, or transient error."},
    },
)
async def get_artist_image_endpoint(
    request: Request,
    name: Annotated[str, Query(min_length=1)],
    service: Annotated[ArtistImageService, Depends(get_artist_image_service)],
    handle: Annotated[LibraryHandle, Depends(get_library)],
    cache: Annotated[ArtistImageCache, Depends(get_artist_image_cache)],
    size: Annotated[Literal["full", "thumb"], Query()] = "full",
) -> Response:
    # Validate off a CHEAP stat-based ETag first: the roster revalidates every
    # portrait on every paint (no ?v= buster, no max-age), and the old path
    # re-read the cached bytes and re-hashed them just to answer a 304.
    # cache.validator stats the winning slot (override > positive) instead, so
    # an unchanged portrait answers 304 without touching the bytes at all.
    validator = await run_in_threadpool(cache.validator, name)
    # The thumb is a DIFFERENT entity than the full image (distinct bytes), so
    # it needs its own ETag under the same URL family — splice a "-t" marker
    # inside the closing quote so the tag stays one opaque quoted string. A
    # shared tag would let a cache/proxy serve a thumb response for a full
    # request (or vice versa) on a matching If-None-Match.
    etag = validator if size == "full" else (f'{validator[:-1]}-t"' if validator else None)
    if etag is not None and if_none_match_hit(request, etag):
        return not_modified(etag)

    if size == "thumb":
        thumb = await run_in_threadpool(cache.get_thumb, name)
        if thumb is None:
            # Nothing cached yet — resolve (network) once, then derive from it.
            resolved = await service.get_artist_image(
                name, get_mbid=lambda: beets_library.get_artist_mbid(handle.lib, name)
            )
            if resolved is None:
                raise HTTPException(status_code=404, detail="Artist image not found")
            thumb = await run_in_threadpool(cache.get_thumb, name)
            if thumb is None:
                # The cache could not produce a thumb even after a successful
                # resolve: an unwritable cache dir (nothing to stat, so
                # get_thumb bails before it can help), or an override that raced
                # away. Derive from the bytes in hand instead of serving the
                # 1000px+ original under a ?size=thumb URL — losing the cache
                # must cost cache HITS, not the feature. Off-loop: this is a
                # decode+resize of up to 10 MB.
                data, mime = await run_in_threadpool(
                    derive_thumb_or_degrade, *resolved, subject=f"artist {name!r}"
                )
                return await run_in_threadpool(revalidating_image_response, request, data, mime)
            validator = await run_in_threadpool(cache.validator, name)
            etag = f'{validator[:-1]}-t"' if validator else None
        if etag is not None:
            return image_response(thumb.data, thumb.content_type, etag)
        # No file to stat — fall back to the content-hash ETag, off-loop.
        return await run_in_threadpool(
            revalidating_image_response, request, thumb.data, thumb.content_type
        )

    # size == "full": query param (not path) so "AC/DC" works. The MBID is
    # resolved lazily — the service only invokes get_mbid on a cache miss
    # (fanart.tv is MBID-keyed).
    result = await service.get_artist_image(
        name, get_mbid=lambda: beets_library.get_artist_mbid(handle.lib, name)
    )
    if result is None:
        # Covers disabled / no verified match / transient error; the frontend
        # falls back to the person glyph on 404.
        raise HTTPException(status_code=404, detail="Artist image not found")

    image_bytes, mime = result
    if validator is None:
        # First serve after a fresh network resolve — IF the positive slot was
        # written, a re-stat yields the tag the NEXT request will validate on.
        # It may not have been (unwritable cache dir): the store is best-effort
        # by design, so this can still come back None.
        validator = await run_in_threadpool(cache.validator, name)
    if validator is not None:
        return image_response(image_bytes, mime, validator)
    # Nothing to stat — an override that raced away, or a cache dir that took
    # no write. Fall back to the content-hash ETag, computed OFF the event loop
    # (sha256 of up to 10 MB). While a cache dir stays broken that hash is paid
    # per request; the in-memory fallback bounds the network cost, not this one.
    return await run_in_threadpool(revalidating_image_response, request, image_bytes, mime)


@router.get("/artists/image/settings", response_model=ArtistImageSettings)
async def get_artist_image_settings_endpoint(
    toggle: Annotated[ArtistImageToggle, Depends(get_artist_image_toggle)],
) -> ArtistImageSettings:
    return ArtistImageSettings(enabled=toggle.is_enabled())


@router.put("/artists/image/settings", response_model=ArtistImageSettings)
async def set_artist_image_settings_endpoint(
    body: ArtistImageSettings,
    toggle: Annotated[ArtistImageToggle, Depends(get_artist_image_toggle)],
) -> ArtistImageSettings:
    return ArtistImageSettings(enabled=toggle.set_enabled(body.enabled))


#: fanart.tv answers only by MusicBrainz id, so it is offered-but-blocked rather
#: than hidden when the artist has none - hiding it would look like a missing
#: feature, and an empty failure after the user picks it would look like a bug.
_NO_MBID_REASON = "No MusicBrainz ID for this artist, so fanart.tv cannot be searched"


def _source_option(source_id: str, blocked_because: str | None) -> ArtistImageSourceOption:
    """One entry of the per-artist source list.

    ``available`` is DERIVED from the reason rather than passed alongside it.
    The model carries no validator tying the two (one was rejected: it would not
    survive into the generated TypeScript), so ``available=False, reason=None``
    is a representable response that would render an empty explanation exactly
    where the user needs a sentence. This is the only place these options are
    built, so deriving the flag here makes that pair unrepresentable.
    """
    return ArtistImageSourceOption(
        # The factory's ids and the model's Literal are the same three strings,
        # pinned by test_source_ids_match_what_the_factory_actually_builds; the
        # factory deliberately keeps plain `str` so app/artwork/ has no
        # dependency on app/models/.
        id=cast(ArtistImageSourceId, source_id),
        label=label_for(source_id),
        available=blocked_because is None,
        reason=blocked_because,
    )


@router.get("/artists/image/sources", response_model=ArtistImageSourceList)
async def list_artist_image_sources_endpoint(
    name: Annotated[str, Query(min_length=1)],
    sources: Annotated[ArtistImageSources, Depends(get_artist_image_sources)],
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> ArtistImageSourceList:
    """The sources a portrait for ``name`` may be fetched from, in chain order.

    Only CONFIGURED sources are listed (an unset API key is an install-level
    fact the user cannot act on from this panel). Of those, fanart.tv reports
    ``available=false`` plus a reason when this artist has no MusicBrainz id.
    ``name`` is a query param, not a path segment, so "AC/DC" survives routing.
    """
    ids = sources.ids()
    # Only pay the beets query when the answer can change something, and never
    # on the event loop - it is a blocking sqlite read over every album of the
    # artist.
    mbid = (
        await run_in_threadpool(beets_library.get_artist_mbid, handle.lib, name)
        if FANARTTV in ids
        else None
    )
    return ArtistImageSourceList(
        sources=[
            _source_option(
                source_id,
                _NO_MBID_REASON if source_id == FANARTTV and not mbid else None,
            )
            for source_id in ids
        ]
    )


@router.post(
    "/artists/image/override",
    response_model=ArtistImageOverrideResult,
    dependencies=[Depends(verify_upload_origin)],
)
async def upload_artist_image_override_endpoint(
    request: Request,
    file: UploadFile,
    name: Annotated[str, Query(min_length=1)],
    cache: Annotated[ArtistImageCache, Depends(get_artist_image_cache)],
) -> ArtistImageOverrideResult:
    # Reject an oversize body before materializing it when the client declares
    # its size; the post-read length check below is the authoritative guard.
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=422, detail="Image too large (max 10 MB)")
    # Bounded read: never buffer more than the cap (+1 to detect an exact-cap
    # overrun) even when Content-Length is absent or understated.
    image_bytes = await file.read(MAX_IMAGE_BYTES + 1)
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=422, detail="Image too large (max 10 MB)")
    mime = sniff_image_mime(image_bytes)
    if mime is None:
        raise HTTPException(status_code=422, detail="not a supported image (png/jpeg/gif/webp)")
    # Offload the blocking mkdir + up-to-10MB cache write (dir may be on slow
    # HDD/NAS) so it never stalls the event loop, mirroring the cache read.
    await run_in_threadpool(cache.write_override, name, image_bytes, mime)
    # UNSCOPED on purpose: the artist image is served under a NORMALIZED name
    # (NFKD accent-fold + casefold + whitespace-collapse — see
    # artwork/normalize.py), so a raw display name is not a reliable identity
    # for the served asset. Scoping on it would silently fail to refresh a
    # twin spelling of the same artist, and mirroring that normalization in TS
    # would duplicate it across languages (casefold != toLowerCase). Album
    # covers ARE scoped — they key off a stable numeric id.
    emit_art_changed(request.app)
    return ArtistImageOverrideResult(ok=True, content_type=mime)


@router.post("/artists/image/override/from-url", response_model=ArtistImageOverrideResult)
async def set_artist_image_override_from_url_endpoint(
    request: Request,
    body: ArtistImageUrlOverride,
    name: Annotated[str, Query(min_length=1)],
    cache: Annotated[ArtistImageCache, Depends(get_artist_image_cache)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_artist_image_http_client)],
) -> ArtistImageOverrideResult:
    try:
        data = await fetch_image_bytes(http_client, str(body.url))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    mime = sniff_image_mime(data)
    if mime is None:
        raise HTTPException(
            status_code=422,
            detail="that link is not a supported image (png/jpeg/gif/webp)",
        )
    await run_in_threadpool(cache.write_override, name, data, mime)
    # UNSCOPED on purpose: the artist image is served under a NORMALIZED name
    # (NFKD accent-fold + casefold + whitespace-collapse — see
    # artwork/normalize.py), so a raw display name is not a reliable identity
    # for the served asset. Scoping on it would silently fail to refresh a
    # twin spelling of the same artist, and mirroring that normalization in TS
    # would duplicate it across languages (casefold != toLowerCase). Album
    # covers ARE scoped — they key off a stable numeric id.
    emit_art_changed(request.app)
    return ArtistImageOverrideResult(ok=True, content_type=mime)


@router.delete("/artists/image/override", status_code=204)
async def clear_artist_image_override_endpoint(
    request: Request,
    name: Annotated[str, Query(min_length=1)],
    cache: Annotated[ArtistImageCache, Depends(get_artist_image_cache)],
) -> Response:
    await run_in_threadpool(cache.clear_override, name)
    # UNSCOPED on purpose: the artist image is served under a NORMALIZED name
    # (NFKD accent-fold + casefold + whitespace-collapse — see
    # artwork/normalize.py), so a raw display name is not a reliable identity
    # for the served asset. Scoping on it would silently fail to refresh a
    # twin spelling of the same artist, and mirroring that normalization in TS
    # would duplicate it across languages (casefold != toLowerCase). Album
    # covers ARE scoped — they key off a stable numeric id.
    emit_art_changed(request.app)
    return Response(status_code=204)


def _gate_library_busy(app: object) -> None:
    # Full union + swap lock; the default message matches the trash gate's.
    raise_if_library_busy(app)


@router.get("/artists/art/settings", response_model=ArtistArtWriteSettings)
async def get_artist_art_settings(
    toggle: Annotated[ArtistArtWriteToggle, Depends(get_artist_art_write_toggle)],
) -> ArtistArtWriteSettings:
    return ArtistArtWriteSettings(enabled=toggle.is_enabled())


@router.put("/artists/art/settings", response_model=ArtistArtWriteSettings)
async def set_artist_art_settings(
    body: ArtistArtWriteSettings,
    toggle: Annotated[ArtistArtWriteToggle, Depends(get_artist_art_write_toggle)],
) -> ArtistArtWriteSettings:
    return ArtistArtWriteSettings(enabled=toggle.set_enabled(body.enabled))


@router.post("/artists/art/apply", response_model=ArtistArtBackfillStatus)
async def apply_artist_art(
    request: Request,
    name: Annotated[str, Query(min_length=1)],
    toggle: Annotated[ArtistArtWriteToggle, Depends(get_artist_art_write_toggle)],
    reg: Annotated[ArtistArtBackfillRegistry, Depends(get_artist_art_backfill)],
) -> ArtistArtBackfillStatus:
    if not toggle.is_enabled():
        raise HTTPException(status_code=403, detail="Enable 'Write artist art to library' first")
    _gate_library_busy(request.app)
    try:
        reg.start(force=True, artist=name, scope_label=name)
    except RuntimeError:
        raise HTTPException(
            status_code=409, detail="An artist-art job is already running"
        ) from None
    handle = request.app.state.beets_library
    _start(request.app, reg, handle.lib, force=True, artist=name)
    return reg.state()


@router.post("/artists/art/backfill", response_model=ArtistArtBackfillStatus)
async def start_artist_art_backfill(
    request: Request,
    toggle: Annotated[ArtistArtWriteToggle, Depends(get_artist_art_write_toggle)],
    reg: Annotated[ArtistArtBackfillRegistry, Depends(get_artist_art_backfill)],
) -> ArtistArtBackfillStatus:
    if not toggle.is_enabled():
        raise HTTPException(status_code=403, detail="Enable 'Write artist art to library' first")
    _gate_library_busy(request.app)
    try:
        reg.start(force=False)
    except RuntimeError:
        raise HTTPException(
            status_code=409, detail="An artist-art job is already running"
        ) from None
    handle = request.app.state.beets_library
    _start(request.app, reg, handle.lib, force=False, artist=None)
    return reg.state()


@router.get("/artists/art/backfill", response_model=ArtistArtBackfillStatus)
async def get_artist_art_backfill_status(
    reg: Annotated[ArtistArtBackfillRegistry, Depends(get_artist_art_backfill)],
) -> ArtistArtBackfillStatus:
    return reg.state()


@router.post("/artists/art/backfill/stop", response_model=ArtistArtBackfillStatus)
async def stop_artist_art_backfill(
    reg: Annotated[ArtistArtBackfillRegistry, Depends(get_artist_art_backfill)],
) -> ArtistArtBackfillStatus:
    reg.request_stop()
    return reg.state()


def _start(
    app: object,
    reg: ArtistArtBackfillRegistry,
    lib: object,
    *,
    force: bool,
    artist: str | None,
) -> None:
    app_settings = getattr(app.state, "settings", None) or _module_settings  # type: ignore[attr-defined]  # app is duck-typed (object) so tests can pass a stub
    delay = float(getattr(app_settings, "lyrics_backfill_delay_seconds", 0.2))
    start_art_backfill(
        reg,
        lib,
        # The same cache dir the lifespan-built service uses, so the sweep sees
        # the manual overrides + cached positives.
        cache_dir=resolve_artist_image_cache_dir(),
        settings=app_settings,
        delay=delay,
        force=force,
        artist=artist,
        # Repaint open tabs when the run finishes (fired from the daemon thread;
        # the broker hops onto the main loop via call_soon_threadsafe). UNSCOPED:
        # a sweep repaints many artists, and even a single-artist run cannot use a
        # display name as an asset identity (see the override handlers above).
        on_complete=lambda: emit_art_changed(app),
    )


@router.delete("/artists", response_model=DeleteResult)
async def delete_artist_endpoint(
    request: Request,
    name: Annotated[str, Query(min_length=1)],
) -> DeleteResult:
    """Move EVERY album of the named artist to Trash (reversible) and drop them.

    ``name`` is a query param so slashes (e.g. "AC/DC") survive routing. 409
    while a library job is running.
    """
    result = await delete_artist_op(request, name)
    emit_library_changed(request.app)
    return result
