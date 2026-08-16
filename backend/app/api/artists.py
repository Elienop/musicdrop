from typing import Annotated, Literal, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.api.albums import get_library
from app.api.csrf import verify_upload_origin
from app.api.http_cache import (
    NO_SNIFF,
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
from app.artwork.filler import ArtistImageFiller
from app.artwork.images import (
    FALLBACK_CONTENT_TYPE,
    MAX_IMAGE_BYTES,
    header_safe_content_type,
    sniff_image_mime,
)
from app.artwork.service import ArtistImageService
from app.artwork.source import TransientSourceError
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
    ArtistImageResetResult,
    ArtistImageSettings,
    ArtistImageSourceId,
    ArtistImageSourceList,
    ArtistImageSourceOption,
    ArtistImageUrlOverride,
)
from app.models.artist_art import ArtistArtBackfillStatus, ArtistArtWriteSettings
from app.models.delete import DeleteResult
from app.models.errors import ErrorDetail

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


def get_artist_image_filler(request: Request) -> ArtistImageFiller:
    """The process-wide background filler, built in the app lifespan.

    Built lazily and CACHED on ``app.state`` when absent: ``TestClient(app)``
    skips the lifespan, and a fresh instance per request would silently lose the
    single-flight the whole design rests on.
    """
    filler: ArtistImageFiller | None = getattr(request.app.state, "artist_image_filler", None)
    if filler is None:
        app = request.app
        filler = ArtistImageFiller(on_filled=lambda: emit_art_changed(app))
        app.state.artist_image_filler = filler
    return filler


def _inline_grace_seconds(app: object) -> float:
    """The inline-wait budget, read from the app's settings (tests monkeypatch
    a stub onto ``app.state``) with the module settings as the fallback - the
    same idiom ``_start`` uses for the backfill delay."""
    app_settings = getattr(app.state, "settings", None) or _module_settings  # type: ignore[attr-defined]  # app is duck-typed (object) so tests can pass a stub
    return float(getattr(app_settings, "artist_image_inline_grace_seconds", 1.5))


@router.get("/artists", response_model=list[Artist])
async def list_artists_endpoint(
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> list[Artist]:
    return await run_in_threadpool(list_artists, handle.lib)


@router.get(
    "/artists/image",
    responses={
        200: {"content": {"image/*": {}}, "description": "The artist portrait."},
        # Names the model for the same reason the fetch route's errors do: a
        # description-only entry REPLACES the generated response, leaving the
        # status with no body schema and generating `content?: never` for a
        # body the client can read.
        404: {
            "model": ErrorDetail,
            "description": "Feature disabled, no verified match, or transient error.",
        },
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
    "/artists/image/fetch",
    dependencies=[Depends(verify_upload_origin)],
    responses={
        200: {
            # FastAPI adds `application/json: {schema: {}}` here as well, from
            # the app-level response class. It cannot be removed except with
            # `response_class=Response`, and that collides with the wire-safety
            # invariant (tests/test_wire.py:164) that EVERY api route resolves
            # to SurrogateSafeJSONResponse - measured: the suite fails. It
            # generates `unknown`, not `never`, so the type offers a JSON body
            # nobody reads rather than denying the binary one; the two sibling
            # binary routes carry the same artifact.
            "content": {"image/*": {}},
            "description": "The candidate portrait. Preview only - nothing is stored.",
        },
        # Every one of these renders `{"detail": "<sentence>"}` at runtime, so
        # every one names the model. A description-only entry REPLACES FastAPI's
        # generated response instead of merging into it, which strips the body
        # schema and generates `content?: never` - a type saying the body cannot
        # exist for statuses whose body the client has to read.
        # TWO causes, and the second one is invisible in the schema: a
        # `dependencies=[...]` guard emits no OpenAPI security scheme, so this
        # sentence is the only place the cross-origin refusal is documented.
        403: {
            "model": ErrorDetail,
            "description": "Artist images are turned off, or the request is cross-origin.",
        },
        404: {"model": ErrorDetail, "description": "That source has no portrait for this artist."},
        # TWO causes, like the 403 above: not configured here, or configured and
        # answering with an image this install cannot store. Both are "the
        # request is fine, this install cannot serve it"; each raises its own
        # one-cause sentence, so the description is the only place both appear.
        409: {
            "model": ErrorDetail,
            "description": (
                "That source is not configured on this install, or its image is in a"
                " format that cannot be stored."
            ),
        },
        502: {
            "model": ErrorDetail,
            "description": "That source failed (timeout, rate limit, bad response).",
        },
        # 422 is deliberately ABSENT: declaring it at all would replace
        # FastAPI's HTTPValidationError (whose `detail` is a LIST of loc/msg
        # objects, not a sentence) with whatever this dict said.
    },
)
async def fetch_artist_image_endpoint(
    name: Annotated[str, Query(min_length=1)],
    source: Annotated[ArtistImageSourceId, Query()],
    service: Annotated[ArtistImageService, Depends(get_artist_image_service)],
    sources: Annotated[ArtistImageSources, Depends(get_artist_image_sources)],
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> Response:
    """Fetch ONE source's portrait candidate for ``name``. Writes NOTHING.

    The preview half of the manual re-fetch: the response is the image itself
    (``no-store``, provenance in ``X-Art-Source``), matching the album cover's
    fetch route so the two panels share one shape. Installing is a SEPARATE
    call - the client posts the very bytes it previewed to
    ``POST /api/artists/image/override``, so nothing can substitute a different
    image between "looks good" and "use it".

    The cache is bypassed in BOTH directions: a fresh ``.miss`` marker does not
    suppress the call (the user asked for it explicitly), and a result is not
    stored (an approved image lands in the override slot, a rejected one leaves
    no trace). The call still takes the service's own rate/concurrency slot, so
    a burst of manual fetches paces against the same 5/s bucket the automatic
    chain uses - ``sources.get`` hands back a bare source with no limiter
    attached, so resolving it directly is the easy thing to write and would
    double the real outbound rate against fanart.tv / Spotify / Deezer.

    ``source`` is typed as the ``Literal``, not ``str``, and that gate is
    load-bearing rather than cosmetic: ``label_for`` echoes an unknown id back
    verbatim and the result lands in the ``X-Art-Source`` header, so a plain
    ``str`` would let a client put its own bytes in a response header. An id
    outside the Literal is refused by validation before this body runs.

    Origin-guarded. A body-less POST is a CORS-simple request, so a foreign page
    can send this one without a preflight - and unlike the album cover's fetch,
    where the only attacker input is a local album id, here the caller picks the
    UPSTREAM and the query it is sent, and that request goes out carrying this
    install's own fanart.tv / Spotify credentials. The route bypasses the cache
    by design, so repeats are not deduplicated either.
    """
    if not service.is_enabled():
        raise HTTPException(status_code=403, detail="Turn on artist images first")
    picked = sources.get(source)
    if picked is None:
        # 409, NOT 422: the request is well-formed, it is this INSTALL that
        # cannot serve it. Keeping 422 for validation alone means one body shape
        # per status - a client branches on the status instead of sniffing
        # whether `detail` came back a string or a list of validation errors.
        raise HTTPException(
            status_code=409, detail=f"{label_for(source)} is not configured on this install"
        )
    # fanart.tv is MBID-keyed; the others ignore it. Resolved for every source
    # because the beets query is one indexed lookup and the branch would only
    # duplicate the sources endpoint's knowledge of which source needs it.
    # Off-loop: it is a blocking sqlite read over every album of the artist.
    mbid = await run_in_threadpool(beets_library.get_artist_mbid, handle.lib, name)
    try:
        async with service.limiter_slot():
            resolved = await picked.resolve(name, mbid=mbid)
    except TransientSourceError as exc:
        # NOT a 404: "the source failed" and "the source has no such image" are
        # different answers, and reporting the first as the second is the exact
        # lie this feature exists to remove. The source is named because 502 is
        # also what a proxy emits when this app itself is down.
        raise HTTPException(
            status_code=502,
            detail=f"{label_for(source)} did not answer - try again in a moment",
        ) from exc
    if resolved is None:
        raise HTTPException(
            status_code=404, detail=f"{label_for(source)} has no portrait for {name}"
        )
    if sniff_image_mime(resolved.data) is None:
        # ONE validation rule for the preview/install pair. The override upload
        # accepts exactly the four families sniff_image_mime knows, so without
        # this a source answering with a real BMP previews 200 and then 422s on
        # approve - the user only discovers it AFTER choosing. Sniffed, not
        # label-checked, because the install sniffs: a source declaring
        # image/png and sending something else would otherwise walk straight
        # through the preview into that same 422.
        #
        # 409 joins the unconfigured-source case: the request is well formed and
        # the source answered, it is THIS install that cannot store the answer.
        # Not 502 - that one promises "try again in a moment", and a source that
        # picks deterministically returns the same unusable image forever.
        declared = header_safe_content_type(resolved.content_type) or FALLBACK_CONTENT_TYPE
        raise HTTPException(
            status_code=409,
            detail=(
                f"{label_for(source)} returned {declared}, which cannot be stored"
                " (needs PNG, JPEG, GIF or WebP)"
            ),
        )
    # The third content-type sink header_safe_content_type's docstring counts,
    # and the only one where the value is a source's OWN answer rather than a
    # cache sidecar. Every source in the tree routes its download through
    # app/artwork/download.py, which already guards this - but the source
    # contract is a Protocol, so nothing stops a future one from returning a
    # header-hostile type, and that would 500 this route (non-ASCII) or drop the
    # connection with no response at all (a control character).
    media_type = header_safe_content_type(resolved.content_type) or FALLBACK_CONTENT_TYPE
    return Response(
        content=resolved.data,
        media_type=media_type,
        # label_for is safe HERE only because `source` came through the Literal.
        headers={
            **NO_SNIFF,
            "Cache-Control": "no-store",
            "X-Art-Source": label_for(source),
        },
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


def _reset_slots(cache: ArtistImageCache, name: str) -> tuple[bool, bool]:
    """Clear both stored portraits for ``name`` in ONE threadpool hop.

    Returns ``(cleared_override, cleared_auto)``. A module-level function rather
    than a lambda so the offload is assertable, and rather than a third cache
    method so the cache keeps only the two primitives that mean something on
    their own.
    """
    return cache.clear_override(name), cache.clear_auto(name)


@router.post(
    "/artists/image/reset",
    response_model=ArtistImageResetResult,
    dependencies=[Depends(verify_upload_origin)],
    # The Origin guard below is invisible in OpenAPI - a `dependencies=[...]`
    # entry emits no security scheme - so a status this route really returns
    # would otherwise be undeclared, and the generated client would be typed as
    # if it could not happen. 422 stays undeclared on purpose: declaring it
    # would replace FastAPI's HTTPValidationError, whose `detail` is a list.
    responses={403: {"model": ErrorDetail, "description": "The request is cross-origin."}},
)
async def reset_artist_image_endpoint(
    request: Request,
    name: Annotated[str, Query(min_length=1)],
    cache: Annotated[ArtistImageCache, Depends(get_artist_image_cache)],
) -> ArtistImageResetResult:
    """Forget every stored portrait for ``name`` so it is looked up again.

    Clears the manual override AND the cached automatic image (plus its
    negative marker and derived thumb). Clearing only the override - which is
    all this used to do - drops the user straight back onto the automatic image
    they just rejected, because a present ``.bin`` means the resolve path never
    runs again.

    The result reports each slot separately: neither may have existed, and on an
    unwritable cache dir a removal can be refused. The caller shows what
    actually happened instead of implying a re-fetch that did not occur.

    Origin-guarded: a body-less POST is a CORS-simple request, so without this
    dependency a foreign page could reset portraits (the DELETE this replaced
    was preflight-protected by its method alone).
    """
    cleared_override, cleared_auto = await run_in_threadpool(_reset_slots, cache, name)
    # UNSCOPED on purpose: the artist image is served under a NORMALIZED name
    # (NFKD accent-fold + casefold + whitespace-collapse — see
    # artwork/normalize.py), so a raw display name is not a reliable identity
    # for the served asset. Scoping on it would silently fail to refresh a
    # twin spelling of the same artist, and mirroring that normalization in TS
    # would duplicate it across languages (casefold != toLowerCase). Album
    # covers ARE scoped — they key off a stable numeric id.
    emit_art_changed(request.app)
    return ArtistImageResetResult(
        ok=True, cleared_override=cleared_override, cleared_auto=cleared_auto
    )


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
