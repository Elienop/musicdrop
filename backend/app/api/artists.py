import logging
import os
from functools import partial
from pathlib import Path
from typing import Annotated, Final, Literal, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.api.albums import get_library
from app.api.http_cache import (
    NO_SNIFF,
    if_none_match_hit,
    image_response,
    not_modified,
    revalidating_image_response,
)
from app.artist_art_jobs.registry import (
    ArtistArtBackfillRegistry,
    artist_art_backfill_active,
    get_artist_art_backfill,
)
from app.artist_art_jobs.runner import start_backfill as start_art_backfill
from app.artwork.cache import ArtistImageCache, CachedImage
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
from app.beets.artist_art import ArtTrashStore
from app.beets.config_editor import _swap_lock
from app.beets.delete import delete_artist_op
from app.beets.library import LibraryHandle, LibraryRootUnavailableError, list_artists
from app.beets.rename import apply_artist_rename_op, preview_artist_rename_op
from app.beets.store_layout import (
    StoreLayoutError,
    checked_protected_trees,
    checked_store_dirs,
)
from app.beets.trash import safe_container_name, trash_replaced_files
from app.beets.trash_origins import TrashOriginsStoreUnusableError
from app.config import Settings, resolve_artist_image_cache_dir
from app.config import settings as _module_settings
from app.etag import size_scoped_etag
from app.events.emit import emit_art_changed, emit_library_changed
from app.fsutil import open_root
from app.library_busy import raise_if_library_busy, raise_if_swap_lock_held
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
from app.models.errors import ErrorDetail, StructuredErrorDetail, validation_or_detail_422
from app.models.rename import ArtistRenamePreview, ArtistRenameRequest, ArtistRenameResult
from app.playlists.reexport import reexport_playlists_containing
from app.playlists.store import get_playlists_dir
from app.wire import display_path

_log = logging.getLogger(__name__)

router = APIRouter(tags=["artists"])

#: The one thing every "no portrait" exit of the image GET says. Named because
#: the endpoint has four such exits and they must not drift apart.
_IMAGE_NOT_FOUND = "Artist image not found"

#: The OpenAPI entry for the 409 that ``_gate_artist_art_busy`` raises. Declared
#: rather than left implicit for the same reason the reset route declares its
#: 403: a status the route really returns but the spec omits renders in
#: ``openapi-typescript`` as ``content?: never`` - a body the client receives,
#: typed as impossible. Named because all THREE mutation routes carry it.
_ART_BUSY_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": "An artist-art job is running, so image changes are refused until it finishes.",
}

#: The reset's 409, which has a cause the shared entry above does not: it is a
#: Trash mutator, so it also refuses while the beets swap lock is held rather
#: than queueing behind a holder with no bound.
_RESET_BUSY_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": (
        "An artist-art job is running or the beets swap lock is held, so the reset is"
        " refused until it finishes."
    ),
}

#: The OpenAPI entries for the two art-WRITE job starters (apply + backfill),
#: which refuse identically: the write toggle is off (403), or some library job
#: already holds the slot (409). Named because both routes carry both.
_ART_WRITE_DISABLED_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": (
        "Writing artist art to the library is turned off in settings, or the"
        " request is cross-origin."
    ),
}
#: Both routes reach this through ``_gate_library_busy`` ->
#: ``raise_if_library_busy``, which refuses for TWO reasons, not one: a library
#: job holds the slot, OR the beets swap lock is held (a config Apply or a
#: duplicate resolve, neither of which is a library job). The swap-lock arm is
#: named here for the same reason trash.py's entry names it - a description
#: that omits half its causes reads as a complete list.
_ART_JOB_TAKEN_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": (
        "A library job (an import, a backfill, or another artist-art run) already"
        " holds the slot, or the beets swap lock is held."
    ),
}

#: The artist-rename refusals, all raised inside ``app/beets/rename.py``'s ops
#: rather than in the endpoint bodies - read the op, not just the endpoint.
_ARTIST_NOT_FOUND_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": "No album in the library has that album artist.",
}


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


@router.get("/artists")
async def list_artists_endpoint(
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> list[Artist]:
    return await run_in_threadpool(list_artists, handle.lib)


@router.post(
    "/artists/rename/preview",
    responses={404: _ARTIST_NOT_FOUND_RESPONSE},
)
async def preview_artist_rename_endpoint(
    payload: ArtistRenameRequest,
    request: Request,
) -> ArtistRenamePreview:
    """Preview an artist rename: per-album move counts + refusals + merge note.

    Read-only. The rename edits ``album_artist`` only; per-track artists never
    follow (see the spec's owner decisions).
    """
    return await preview_artist_rename_op(request, payload)


@router.post(
    "/artists/rename",
    responses={
        404: _ARTIST_NOT_FOUND_RESPONSE,
        409: {
            "model": ErrorDetail,
            "description": (
                "A library operation is in progress, so the rename is refused until it finishes."
            ),
        },
        # The blanket ``except Exception`` in ``apply_artist_rename_op``
        # answers with a NESTED detail object, not a sentence;
        # frontend/src/api/useArtistRename.ts reads it through lib.ts.
        500: {
            "model": StructuredErrorDetail,
            "description": (
                "The rename failed part-way through the batch; the body carries"
                " the cause and a recovery hint."
            ),
        },
    },
)
async def rename_artist_endpoint(
    payload: ArtistRenameRequest,
    request: Request,
    cache: Annotated[ArtistImageCache, Depends(get_artist_image_cache)],
    toggle: Annotated[ArtistArtWriteToggle, Depends(get_artist_art_write_toggle)],
    reg: Annotated[ArtistArtBackfillRegistry, Depends(get_artist_art_backfill)],
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
) -> ArtistRenameResult:
    """Rename an artist: fan the album edit across every album, then the collateral.

    Collateral order matters: the portrait re-key runs FIRST (only when the old
    name is fully vacated AND at least one album actually renamed - a
    fully-drifted batch vacates the old name without renaming anything, so
    re-keying would orphan the still-live old key's image), THEN the artist-art
    job is kicked (it writes poster files FROM the cache, so the cache must
    already answer for the new name), then the ``.m3u8`` re-export (best-effort).
    All collateral is best-effort reporting, never a failure of the rename
    itself.
    """
    outcome = await apply_artist_rename_op(request, payload)
    app_obj = request.app
    handle = app_obj.state.beets_library

    portrait: Literal["moved", "kept_target", "none", "not_rekeyed"] = "not_rekeyed"
    if outcome.old_name_remaining_albums == 0 and any(
        a.outcome == "renamed" for a in outcome.albums
    ):
        # Ruling: an all-drifted batch empties the old name without renaming
        # anything - the old key is still live, so the portrait must stay.
        portrait = await run_in_threadpool(cache.rename, payload.name, payload.new_name)

    moved_ids = set(outcome.moved_item_ids)
    art_job: Literal["started", "skipped_busy", "not_needed"] = "not_needed"
    if moved_ids and toggle.is_enabled():
        # force=False: the new folders have no art and get it, folders that
        # already hold a poster/background keep it. A rename that merges two
        # artists arrives at a target folder somebody may have curated, and
        # nobody asked for that to be replaced (owner ruling, 2026-09-11).
        # Replacing is what the per-artist Apply button is for.
        try:
            reg.start(force=False, artist=payload.new_name, scope_label=payload.new_name)
        except RuntimeError:
            art_job = "skipped_busy"
        else:
            _start(app_obj, reg, handle.lib, force=False, artist=payload.new_name)
            art_job = "started"

    reexported = await reexport_playlists_containing(moved_ids, handle, playlists_dir)

    emit_library_changed(app_obj)
    if portrait in ("moved", "kept_target"):
        emit_art_changed(app_obj)

    return ArtistRenameResult(
        name=payload.name,
        new_name=payload.new_name,
        albums=outcome.albums,
        old_name_remaining_albums=outcome.old_name_remaining_albums,
        portrait=portrait,
        playlists_reexported=reexported,
        artist_art_job=art_job,
    )


async def _serve_cached(
    cache: ArtistImageCache, name: str, *, size: Literal["full", "thumb"], etag: str
) -> Response | None:
    """The response for an artist whose cache slot exists. Never the network.

    ``None`` means the slot could not be turned into bytes after all - it
    vanished between the stat and the read (the backfill daemon's atomic
    replace, a reset), or the read itself failed. The caller falls THROUGH to
    the resolve path rather than 404ing: ``validator`` would keep stat-ing that
    same file, so a 404 here would be permanent instead of self-healing, and a
    re-resolve overwrites the unusable slot.

    Precondition: ``etag`` is the tag ``app.etag.size_scoped_etag`` built from
    the validator that proved this slot exists, for THIS size.
    """
    if size == "thumb":
        thumb = await run_in_threadpool(cache.get_thumb, name)
        return None if thumb is None else image_response(thumb.data, thumb.content_type, etag)
    cached = await run_in_threadpool(cache.get, name)
    if not isinstance(cached, CachedImage):
        return None
    return image_response(cached.data, cached.content_type, etag)


async def _serve_full(
    request: Request, cache: ArtistImageCache, name: str, image_bytes: bytes, mime: str
) -> Response:
    """Serve freshly-resolved full-size bytes with the cheapest validator
    available: the cache's own tag IF the store left something to validate,
    else the content-hash ETag computed OFF the loop.

    The store is best-effort by design (an unwritable cache dir must cost the
    caching, not the response), so the re-read can still come back None — but a
    broken dir alone no longer forces that: a write that could not reach disk is
    stranded in the memory tier, which answers ``validator`` with its own
    precomputed tag, so this path takes the cheap branch and the per-request
    sha256 of up to 10 MB is not paid. What still reaches the fallback is a
    store that left NOTHING in either tier — an entry over the tier's byte
    budget, or a slot that raced away between the store and this read.
    """
    validator = await run_in_threadpool(cache.validator, name)
    if validator is not None:
        return image_response(image_bytes, mime, validator)
    return await run_in_threadpool(revalidating_image_response, request, image_bytes, mime)


async def _serve_thumb(
    request: Request, cache: ArtistImageCache, name: str, source: tuple[bytes, str]
) -> Response:
    """Serve the 320px derivation of a freshly-resolved image, preferring the
    cache's own (which it wrote during the resolve) and deriving from the bytes
    in hand when the cache could not store one.

    That second branch is no longer "the cache dir is unwritable": a store that
    could not reach disk strands the source in the memory tier, and
    ``get_thumb`` derives from the strand and serves it (caching no ``.thumb``
    pair, since the tag dies with the process). It stays reachable for the cases
    that leave NOTHING to derive from — an image over the memory tier's byte
    budget, or an override that raced away between the store and this read.
    Deriving beats serving a 1000px+ original under a ``?size=thumb`` URL:
    losing the cache must cost cache HITS, not the feature. Off-loop, because
    it is a decode+resize of up to 10 MB.
    """
    thumb = await run_in_threadpool(cache.get_thumb, name)
    if thumb is not None:
        validator = await run_in_threadpool(cache.validator, name)
        if validator is not None:
            return image_response(
                thumb.data, thumb.content_type, size_scoped_etag(validator, "thumb")
            )
        return await run_in_threadpool(
            revalidating_image_response, request, thumb.data, thumb.content_type
        )
    data, mime = await run_in_threadpool(
        derive_thumb_or_degrade, *source, subject=f"artist {name!r}"
    )
    return await run_in_threadpool(revalidating_image_response, request, data, mime)


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
            "description": (
                "Feature disabled, no verified match, a transient error, or not resolved "
                "YET - an uncached portrait fills in the background and announces itself."
            ),
        },
    },
)
async def get_artist_image_endpoint(
    request: Request,
    name: Annotated[str, Query(min_length=1)],
    service: Annotated[ArtistImageService, Depends(get_artist_image_service)],
    handle: Annotated[LibraryHandle, Depends(get_library)],
    cache: Annotated[ArtistImageCache, Depends(get_artist_image_cache)],
    filler: Annotated[ArtistImageFiller, Depends(get_artist_image_filler)],
    size: Annotated[Literal["full", "thumb"], Query()] = "full",
) -> Response:
    """The artist portrait, served from cache or resolved without blocking.

    A cache hit answers from a cheap stat-based ETag (the roster revalidates
    every portrait on every paint). A MISS no longer resolves inside the request
    for as long as the sources take: it waits a short grace window and then
    404s, leaving one single-flight background task to finish and announce
    itself with a coalesced ``art:changed``. So a 404 here means "no portrait
    right now", not necessarily "never" - the frontend already renders the
    monogram on 404 and re-requests when the global asset version moves.

    ``name`` is a query param (not a path segment) so "AC/DC" works. The MBID is
    resolved lazily - the service only invokes ``get_mbid`` on a cache miss,
    because fanart.tv is the only MBID-keyed source.
    """
    # Validate off a CHEAP stat-based ETag first: the roster revalidates every
    # portrait on every paint (no ?v= buster, no max-age), and the old path
    # re-read the cached bytes and re-hashed them just to answer a 304.
    # cache.validator stats the winning slot (override > positive) instead, so
    # an unchanged portrait answers 304 without touching the bytes at all.
    validator = await run_in_threadpool(cache.validator, name)
    # The thumb (size == "thumb") needs its OWN tag under this one URL: a shared
    # tag would let a cache/proxy or the client 304 a thumb request off the
    # full tag (or vice versa). size_scoped_etag splices the "-t" marker inside
    # the closing quote and passes a None validator through as None.
    etag = size_scoped_etag(validator, size)
    if etag is not None and if_none_match_hit(request, etag):
        return not_modified(etag)

    # Checked AFTER the conditional GET so a client holding a cached copy still
    # gets its cheap 304 when the feature is toggled off mid-session - the
    # ordering this endpoint has always had. Checked HERE at all because the
    # cache branch below never reaches the service, and because a disabled
    # feature must leave no background task running.
    if not service.is_enabled():
        raise HTTPException(status_code=404, detail=_IMAGE_NOT_FOUND)

    if etag is not None:
        served = await _serve_cached(cache, name, size=size, etag=etag)
        if served is not None:
            return served

    # Nothing usable cached. Honour an unexpired no-match marker WITHOUT reading
    # bytes, then hand the resolve to the single-flight filler.
    if await run_in_threadpool(cache.has_fresh_negative, name):
        raise HTTPException(status_code=404, detail=_IMAGE_NOT_FOUND)
    resolved = await filler.fill(
        service,
        name,
        get_mbid=lambda: beets_library.get_artist_mbid(handle.lib, name),
        grace_seconds=_inline_grace_seconds(request.app),
    )
    if resolved is None:
        # Either a confirmed no-match / transient failure, or still running.
        # Both are "no bytes for you right now"; a running fill announces
        # itself when it lands.
        raise HTTPException(status_code=404, detail=_IMAGE_NOT_FOUND)
    if size == "thumb":
        return await _serve_thumb(request, cache, name, resolved)
    image_bytes, mime = resolved
    return await _serve_full(request, cache, name, image_bytes, mime)


@router.get("/artists/image/settings")
async def get_artist_image_settings_endpoint(
    toggle: Annotated[ArtistImageToggle, Depends(get_artist_image_toggle)],
) -> ArtistImageSettings:
    return ArtistImageSettings(enabled=toggle.is_enabled())


@router.put("/artists/image/settings")
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


@router.get("/artists/image/sources")
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
        # TWO causes, and the second one is invisible in the schema: the
        # app-wide Origin guard is middleware, which emits no OpenAPI security
        # scheme, so this sentence is the only place it is documented.
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
    """Fetch ONE source's portrait candidate for ``name``. Writes NOTHING."""
    # The preview half of the manual re-fetch: the response is the image itself
    # (``no-store``, provenance in ``X-Art-Source``), matching the album cover's
    # fetch route. Installing is a SEPARATE call — the client posts the very
    # bytes it previewed to ``POST /api/artists/image/override``, so nothing can
    # substitute a different image between "looks good" and "use it".
    #
    # The cache is bypassed in BOTH directions: a fresh ``.miss`` marker does not
    # suppress the call, and a result is not stored. It still takes the service's
    # own rate/concurrency slot, so a burst paces against the same 5/s bucket the
    # automatic chain uses — ``sources.get`` hands back a bare source with no
    # limiter, so resolving it directly would double the real outbound rate.
    #
    # ``source`` is the ``Literal``, not ``str``: ``label_for`` echoes an unknown
    # id back verbatim into the ``X-Art-Source`` header, so a plain ``str`` would
    # let a client put its own bytes in a response header.
    #
    # Origin-guarded. A body-less POST is CORS-simple, and unlike the album
    # cover's fetch the caller picks the UPSTREAM and the query, on a request
    # that carries this install's fanart.tv / Spotify credentials.
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
    # A content-type sink whose value is a source's OWN answer rather than a
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
    responses={
        409: _ART_BUSY_RESPONSE,
        # 413/415 rather than 422, matching the playlist artwork upload: an
        # oversize payload and an unsupported media type each have their own
        # status, and 422 is left to mean "well-formed but semantically wrong".
        413: {
            "model": ErrorDetail,
            "description": (
                "The uploaded image exceeds the 10 MB limit, or the request body"
                " exceeds the app-wide size limit."
            ),
        },
        415: {
            "model": ErrorDetail,
            "description": "The uploaded bytes are not a PNG, JPEG, GIF or WebP image.",
        },
    },
)
async def upload_artist_image_override_endpoint(
    request: Request,
    file: UploadFile,
    name: Annotated[str, Query(min_length=1)],
    cache: Annotated[ArtistImageCache, Depends(get_artist_image_cache)],
) -> ArtistImageOverrideResult:
    _gate_artist_art_busy()
    # Reject an oversize body before materializing it when the client declares
    # its size; the post-read length check below is the authoritative guard.
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image too large (max 10 MB)")
    # Bounded read: never buffer more than the cap (+1 to detect an exact-cap
    # overrun) even when Content-Length is absent or understated.
    image_bytes = await file.read(MAX_IMAGE_BYTES + 1)
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image too large (max 10 MB)")
    mime = sniff_image_mime(image_bytes)
    if mime is None:
        raise HTTPException(status_code=415, detail="not a supported image (png/jpeg/gif/webp)")
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


@router.post(
    "/artists/image/override/from-url",
    responses={
        409: _ART_BUSY_RESPONSE,
        # A reachable-but-useless link is a semantic failure of a well-formed
        # request, so it stays 422 - and the route therefore returns BOTH 422
        # bodies (see app/models/errors.py).
        422: validation_or_detail_422(
            "The link could not be fetched, or what it returned is not a PNG,"
            " JPEG, GIF or WebP image; or the request failed validation."
        ),
    },
)
async def set_artist_image_override_from_url_endpoint(
    request: Request,
    body: ArtistImageUrlOverride,
    name: Annotated[str, Query(min_length=1)],
    cache: Annotated[ArtistImageCache, Depends(get_artist_image_cache)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_artist_image_http_client)],
) -> ArtistImageOverrideResult:
    # Before the outbound fetch, not just before the write: a refused request
    # must not spend an upstream call it is going to throw away.
    _gate_artist_art_busy()
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


def _clear_auto_slot(cache: ArtistImageCache, name: str) -> bool:
    """Clear the CACHED AUTOMATIC portrait for ``name``. Blocking.

    True when the automatic slot held an image. A module-level function rather
    than a lambda so the offload is assertable.

    The OVERRIDE slot is deliberately not touched here: its files went to Trash
    before this ran, so a slot-scoped unlink of the override pair would
    re-resolve the slot and remove whatever it holds NOW - after an upload that
    landed in between, a file that never reached Trash. The reset removes
    exactly what it moved.
    """
    return cache.clear_auto(name)


def _trash_override_files(files: list[Path], name: str, store: ArtTrashStore) -> None:
    """Move a stored override's files into one Trash container. Blocking.

    A module-level function rather than a lambda so the offload is assertable.
    The container is named from the artist's display NAME, so it goes through
    ``safe_container_name``: "AC/DC" would otherwise nest it out of the Trash
    page. The origin recorded is the cache dir the files were in — read off the
    files themselves rather than resolved a second time.

    The mover takes NAMES against a directory descriptor, so the cache dir is
    opened once here and the two files are passed by name. FOLLOWING links,
    unlike the library-side caller: this is an app-owned directory the operator
    may legitimately place through a symlink, and it is not the surface the
    anchoring exists for.
    """
    cache_dir = files[0].parent
    dir_fd = open_root(cache_dir)
    try:
        trash_replaced_files(
            [file.name for file in files],
            src_dir_fd=dir_fd,
            container_name=safe_container_name(name, " - artist image"),
            origin=cache_dir,
            trash_dir=store.trash_dir,
            origins_dir=store.origins_dir,
            protected=store.protected,
        )
    finally:
        os.close(dir_fd)


#: The 503 when the move into Trash fails. "The reset stopped" rather than
#: "nothing was reset": the mover records a partial move, so the portrait can
#: already be in Trash — measured in
#: ``tests/test_artist_image_reset_to_trash.py``. The cause is appended by the
#: caller as the ``OSError``'s ``strerror``, never as a server path.
_MOVE_FAILED: Final = (
    "The uploaded image could not be fully moved to Trash, so the reset stopped;"
    " check Trash before retrying."
)


async def _move_override_to_trash(
    handle: LibraryHandle, settings: Settings, cache: ArtistImageCache, name: str
) -> bool:
    """Put a stored override in Trash; ``True`` when there was one.

    Runs BEFORE the slots are cleared, and a failure here raises instead of
    letting the clear go ahead: the override is a file the user uploaded or
    pasted, so the alternatives are "in Trash with a record" or "still served",
    never unlinked. Both 503s are raised inline so the status stays a literal
    ``tests/test_route_status_declarations.py`` can see.

    :data:`_MOVE_FAILED` says the reset stopped rather than that nothing moved:
    ``trash_replaced_files`` records a PARTIAL move on purpose, and the image
    moves before its mime sidecar (``cache.override_files`` order), so a failure
    on the second move leaves the portrait in Trash with a record.
    """
    files = await run_in_threadpool(cache.override_files, name)
    if not files:
        return False
    try:
        store = await run_in_threadpool(_checked_art_trash_store, handle, settings)
    except StoreLayoutError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # The same tier: the store check refuses to create a Trash inside a library
    # whose music is not there (security seat H-1), and the override is still
    # served rather than unlinked.
    except LibraryRootUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        await run_in_threadpool(_trash_override_files, files, name, store)
    except (OSError, TrashOriginsStoreUnusableError) as exc:
        # The path is logged and kept off the wire, the shape
        # ``trash_origins._store_unusable`` uses; ``%r`` because a configured
        # store path carrying a newline or an ANSI escape forges log lines.
        _log.warning(
            "The artist-image reset could not move an override into Trash at %r.",
            display_path(str(store.trash_dir)),
            exc_info=True,
        )
        why = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        raise HTTPException(status_code=503, detail=f"{_MOVE_FAILED} {why}") from exc
    return True


@router.post(
    "/artists/image/reset",
    # The app-wide Origin guard is invisible in OpenAPI - middleware emits no
    # security scheme - so a status this route really returns would otherwise be
    # undeclared, and the generated client would be typed as if it could not
    # happen. 422 stays undeclared on purpose: declaring it would replace
    # FastAPI's HTTPValidationError, whose `detail` is a list.
    responses={
        403: {"model": ErrorDetail, "description": "The request is cross-origin."},
        409: _RESET_BUSY_RESPONSE,
        503: {
            "model": ErrorDetail,
            "description": (
                "An uploaded or pasted image is stored for this artist and could not be"
                " fully moved to Trash, so the reset stopped. Part of it may already be"
                " in Trash."
            ),
        },
    },
)
async def reset_artist_image_endpoint(
    request: Request,
    name: Annotated[str, Query(min_length=1)],
    cache: Annotated[ArtistImageCache, Depends(get_artist_image_cache)],
    service: Annotated[ArtistImageService, Depends(get_artist_image_service)],
    handle: Annotated[LibraryHandle, Depends(get_library)],
    filler: Annotated[ArtistImageFiller, Depends(get_artist_image_filler)],
) -> ArtistImageResetResult:
    """Forget this artist's portrait so it is looked up again; an uploaded or
    pasted image moves to Trash first.
    """
    _gate_artist_art_busy()
    # Refuse rather than QUEUE behind the lock: no holder is bounded (a restore
    # re-imports, a duplicates merge runs a whole batch) and ``apiFetch`` sets
    # no timeout, so waiting pins the confirm dialog with Cancel disabled for as
    # long as the holder runs. The lock half only - the job union would refuse
    # for the length of an import, which never holds the lock.
    raise_if_swap_lock_held(request.app)
    settings: Settings = getattr(request.app.state, "settings", None) or _module_settings
    # The beets swap lock, held across the store check, the move and the clear:
    # this route is a Trash MUTATOR now, and the three in ``app/api/trash.py``
    # hold the same lock. An Empty-all landing between the container's ``mkdir``
    # and the move rmtree'd the file this feature exists to protect, and a
    # config Apply landing there moved it into the OLD Trash dir.
    async with _swap_lock(request.app):
        # Asked AGAIN, now that the lock is ours: the gate above read a flag the
        # pre-check's own 409 window and the acquire can outlive, and a sweep
        # that started meanwhile re-stores the slot this is about to clear. A
        # 409 raised here leaves the lock through ``async with``.
        _gate_artist_art_busy()
        moved_to_trash = await _move_override_to_trash(handle, settings, cache, name)
        # The AUTOMATIC slot too: a present ``.bin`` means the resolve path never
        # runs, so clearing only the override lands the user back on the image
        # they just rejected.
        cleared_auto = await run_in_threadpool(_clear_auto_slot, cache, name)
    if service.is_enabled():
        # Clearing the automatic slot is the point of this route, which means
        # that without a kick the artist shows a monogram until the NEXT image
        # request resolves one - the user pressed a button and the app appears
        # to have lost the picture. A zero grace starts the resolve without
        # waiting for it, so the reset still answers immediately; the filler's
        # single-flight map makes a redundant kick free, and its coalesced
        # art:changed announces the portrait when it lands.
        await filler.fill(
            service,
            name,
            get_mbid=lambda: beets_library.get_artist_mbid(handle.lib, name),
            grace_seconds=0.0,
        )
    # UNSCOPED on purpose: the artist image is served under a NORMALIZED name
    # (NFKD accent-fold + casefold + whitespace-collapse — see
    # artwork/normalize.py), so a raw display name is not a reliable identity
    # for the served asset. Scoping on it would silently fail to refresh a
    # twin spelling of the same artist, and mirroring that normalization in TS
    # would duplicate it across languages (casefold != toLowerCase). Album
    # covers ARE scoped — they key off a stable numeric id.
    emit_art_changed(request.app)
    # ``cleared_override`` IS the move: the files are in Trash and the clear
    # above leaves the slot alone, so nothing else can have emptied it. The
    # user's answer is "your upload is no longer in play", and Trash is where
    # it went.
    return ArtistImageResetResult(
        ok=True, cleared_override=moved_to_trash, cleared_auto=cleared_auto
    )


def _gate_library_busy(app: object) -> None:
    # Full union + swap lock; the default message matches the trash gate's.
    raise_if_library_busy(app)


def _gate_artist_art_busy() -> None:
    """Refuse an artist-IMAGE mutation while the artist-art sweep is running.

    Narrower than ``_gate_library_busy`` on purpose. What these endpoints write
    in the artist-image cache directory has exactly ONE background job touching
    it: the artist-art sweep, which builds its own ArtistImageCache over the
    same dir. Using the full library-busy union would 409 a portrait upload for
    the whole length of an unrelated import, to prevent a collision that import
    cannot cause. ``artist_art_backfill_active`` is called directly rather than
    through ``library_job_active(exclude=...)`` so a sixth job type added later
    cannot silently join this gate.

    The reset writes the Trash dir and the origin store as well, which three
    lock-holding routes in ``app/api/trash.py`` also mutate. That half is
    serialised by the beets swap lock the reset holds, not by this gate - so
    read the route's own ``async with`` before concluding it is only gated here.

    For the cache writes this gate does cover, it is a COHERENCE guard, not a
    corruption guard: they are atomic (tmp + os.replace) and the override slot
    always beats the positive one. What it prevents is a nonsense OUTCOME - a
    reset that clears the automatic slot while the sweep is mid-resolve for that
    same artist can be undone by the sweep's own store, and
    ``POST /artists/art/apply`` copies whatever the cache holds into the music
    folder, so an image changing under it makes the file it writes
    nondeterministic.

    The FETCH route is deliberately NOT gated: it writes nothing. It shares the
    service's limiter with the sweep, which paces it rather than conflicting.
    """
    if artist_art_backfill_active():
        raise HTTPException(
            status_code=409,
            detail="An artist art job is running; image changes available when it finishes",
        )


@router.get("/artists/art/settings")
async def get_artist_art_settings(
    toggle: Annotated[ArtistArtWriteToggle, Depends(get_artist_art_write_toggle)],
) -> ArtistArtWriteSettings:
    return ArtistArtWriteSettings(enabled=toggle.is_enabled())


@router.put("/artists/art/settings")
async def set_artist_art_settings(
    body: ArtistArtWriteSettings,
    toggle: Annotated[ArtistArtWriteToggle, Depends(get_artist_art_write_toggle)],
) -> ArtistArtWriteSettings:
    return ArtistArtWriteSettings(enabled=toggle.set_enabled(body.enabled))


@router.post(
    "/artists/art/apply",
    responses={403: _ART_WRITE_DISABLED_RESPONSE, 409: _ART_JOB_TAKEN_RESPONSE},
)
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


@router.post(
    "/artists/art/backfill",
    responses={403: _ART_WRITE_DISABLED_RESPONSE, 409: _ART_JOB_TAKEN_RESPONSE},
)
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


@router.get("/artists/art/backfill")
async def get_artist_art_backfill_status(
    reg: Annotated[ArtistArtBackfillRegistry, Depends(get_artist_art_backfill)],
) -> ArtistArtBackfillStatus:
    return reg.state()


@router.post("/artists/art/backfill/stop")
async def stop_artist_art_backfill(
    reg: Annotated[ArtistArtBackfillRegistry, Depends(get_artist_art_backfill)],
) -> ArtistArtBackfillStatus:
    reg.request_stop()
    return reg.state()


def _checked_art_trash_store(handle: LibraryHandle, settings: Settings) -> ArtTrashStore:
    """The Trash store a replaced image goes into, checked for THIS call.

    Raises rather than answering ``None``, because the reset endpoint has to
    turn a refused store into a 503 with the store's own sentence: it is about
    to move a file the user uploaded, and unlinking it instead is the data loss
    the whole move-aside exists to stop. Blocking (``resolve`` + the layout
    walk's stats + a stat per app-owned directory).

    The identities go with the pair: the mover opens the Trash ROOT as the
    directory this examined, so the two must come from one moment.

    Raises:
        StoreLayoutError: refused, or a path would not resolve.
    """
    trash_dir, origins_dir = checked_store_dirs(settings, handle)
    protected = checked_protected_trees(
        settings, handle, trash_dir=trash_dir, origins_dir=origins_dir
    )
    return ArtTrashStore(trash_dir=trash_dir, origins_dir=origins_dir, protected=protected)


def _art_trash_store(app: object, settings: Settings) -> ArtTrashStore | None:
    """Where a REPLACED poster/background goes, or ``None`` if it cannot be named.

    Handed to the job as a callable, not a resolved pair: ``checked_store_dirs``
    is a per-call check, and the job asks this again for every artist so a Trash
    dir swapped for a symlink into the library after the start is refused for
    the folders still to come.

    Only the forced write needs it, so only the forced write pays for the layout
    walk. ``None`` is the honest answer to a refused or unresolvable store, and
    it is not a 503 here: this route's job is to START a job, the run reports the
    outcome per artist, and declaring a new status on the route would change the
    contract. Each folder holding art the run would replace is then reported
    ``failed`` with its files still in place (``artist_art.write_artist_art``).
    """
    handle: LibraryHandle | None = getattr(app.state, "beets_library", None)  # type: ignore[attr-defined]  # app is duck-typed (object) so tests can pass a stub
    if handle is None:
        return None
    try:
        return _checked_art_trash_store(handle, settings)
    except (StoreLayoutError, LibraryRootUnavailableError):
        # Both refusals mean the same thing here — there is no store to move a
        # replaced file into — and both are per-artist, so a sweep reports the
        # folders it could not touch rather than failing the job.
        _log.warning(
            "artist art: the Trash store is refused, so a forced write will not replace"
            " any existing file",
            exc_info=True,
        )
        return None


def _start(
    app: object,
    reg: ArtistArtBackfillRegistry,
    lib: object,
    *,
    force: bool,
    artist: str | None,
) -> None:
    app_settings: Settings = getattr(app.state, "settings", None) or _module_settings  # type: ignore[attr-defined]  # app is duck-typed (object) so tests can pass a stub
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
        # Only a forced run gets a resolver — the skip-existing sweep replaces
        # nothing, so it has no file to move aside. Not called here: the job
        # asks it per artist, on its own thread.
        resolve_trash=partial(_art_trash_store, app, app_settings) if force else None,
        artist=artist,
        # Repaint open tabs when the run finishes (fired from the daemon thread;
        # the broker hops onto the main loop via call_soon_threadsafe). UNSCOPED:
        # a sweep repaints many artists, and even a single-artist run cannot use a
        # display name as an asset identity (see the override handlers above).
        on_complete=lambda: emit_art_changed(app),
    )


@router.delete(
    "/artists",
    # Named models on all three: a description-only entry drops the `content`
    # block and openapi-typescript renders `content?: never` for a body the
    # client must read (see app/models/errors.py). All three are raised inside
    # delete_artist_op (app/beets/delete.py), not here. No 404: deleting an
    # unknown artist is a no-op on this route - delete_artist_op never raises
    # AlbumNotFoundError, only its album twin does.
    responses={
        409: {
            "model": ErrorDetail,
            "description": (
                "A library operation is in progress, so the delete is refused until it finishes."
            ),
        },
        500: {
            "model": StructuredErrorDetail,
            "description": (
                # Also the status for a fault PART-WAY through the fan-out. The
                # promise excludes the album it stopped on: its files can be
                # under Trash with its rows kept, or with them gone.
                "Deleting the artist failed; the message names how far the fan-out got,"
                " and the body promises recovery from the Trash folder only when albums"
                " really reached it."
            ),
        },
        # Flat ErrorDetail, unlike the 500 beside it: this one is raised only
        # while the fan-out has DROPPED nothing, which is what lets its
        # description promise that and nothing else. Once an album has been
        # dropped the same cause is re-raised as ArtistDeletePartialError and
        # lands on the 500 above - see app/beets/delete.py.
        503: {
            "model": ErrorDetail,
            "description": (
                # Same three causes as the album route; the message says which.
                # Once an album HAS been dropped the same fault is reported as the
                # 500 above instead, which names how far the fan-out got.
                "None of the artist's albums has been dropped: a setup fault refused the"
                " delete, and a share that dropped mid-move can leave part of one under"
                " Trash — check there before retrying."
            ),
        },
    },
)
async def delete_artist_endpoint(
    request: Request,
    name: Annotated[str, Query(min_length=1)],
    handle: Annotated[LibraryHandle, Depends(get_library)],
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
) -> DeleteResult:
    """Move EVERY album of the named artist to Trash (reversible) and drop them.

    ``name`` is a query param so slashes (e.g. "AC/DC") survive routing. 409
    while a library job is running.

    Same `.m3u8` collateral as the single-album delete: every playlist holding one
    of the dropped tracks is re-exported so it stops listing a file that is now in
    Trash.
    """
    dropped_ids: set[int] = set()
    result = await delete_artist_op(request, name, dropped_ids)
    reexported = await reexport_playlists_containing(dropped_ids, handle, playlists_dir)
    emit_library_changed(request.app)
    return result.model_copy(update={"playlists_reexported": reexported})
