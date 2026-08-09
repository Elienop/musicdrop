from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.api.csrf import verify_upload_origin
from app.api.http_cache import (
    NO_SNIFF,
    if_none_match_hit,
    image_response,
    not_modified,
    revalidating_image_response,
)
from app.artwork.cover_thumbs import CoverThumbCache
from app.artwork.images import FALLBACK_CONTENT_TYPE, header_safe_content_type
from app.beets.completeness import missing_report_op
from app.beets.cover import fetch_cover_op, install_cover_op
from app.beets.delete import delete_album_op
from app.beets.edit import apply_album_edit_op, preview_album_edit_op
from app.beets.library import (
    LibraryHandle,
    cover_validator,
    get_album_cover,
    get_album_detail,
    list_albums,
)
from app.beets.lyrics import start_album_lyrics_op
from app.config import resolve_cover_thumb_cache_dir
from app.events.emit import emit_art_changed, emit_library_changed
from app.models.album import Album, AlbumDetail, AlbumPage
from app.models.completeness import AlbumMissingReport
from app.models.cover import CoverInstallResult
from app.models.delete import DeleteResult
from app.models.edit import AlbumEditPreview, AlbumEditRequest, AlbumEditResult
from app.models.lyrics import LyricsBackfillStatus

router = APIRouter(tags=["albums"])

_MAX_COVER_BYTES = 10 * 1024 * 1024


def get_library(request: Request) -> LibraryHandle:
    """Return the process-wide beets library handle opened at startup.

    The library is resolved once in the app lifespan and stored on
    ``app.state.beets_library`` — no per-request open. ``setup_beets`` always
    returns a handle (BEETSDIR + ``config.yaml`` are created from the starter
    if missing), so this dependency is non-optional. Tests override it to
    inject a hermetic temp handle (the override bypasses ``app.state``).
    """
    handle: LibraryHandle = request.app.state.beets_library
    return handle


def get_cover_thumb_cache(request: Request) -> CoverThumbCache:
    """The process-wide cover-thumb disk cache, built in the app lifespan.

    Falls back to a fresh instance over the configured dir when unset (mirrors
    the ``getattr(..., None)`` idiom other state getters use for the same
    reason — see e.g. ``app.api.acquisition``/``app.api.events``). This
    dependency runs on EVERY cover GET, not just ``size=thumb`` ones, so a
    plain ``request.app.state.cover_thumb_cache`` would make even a full-size
    request hard-depend on lifespan-built state; ``TestClient(app)`` skips the
    lifespan, and tests that never touch thumbs never wire this attribute.
    """
    cache: CoverThumbCache | None = getattr(request.app.state, "cover_thumb_cache", None)
    if cache is None:
        cache = CoverThumbCache(resolve_cover_thumb_cache_dir())
    return cache


@router.get("/albums", response_model=AlbumPage)
async def list_albums_endpoint(
    handle: Annotated[LibraryHandle, Depends(get_library)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    artist: Annotated[str | None, Query()] = None,
) -> AlbumPage:
    items: list[Album]
    items, total = await run_in_threadpool(
        list_albums, handle.lib, limit=limit, offset=offset, artist=artist
    )
    return AlbumPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/albums/{album_id}", response_model=AlbumDetail)
async def get_album_detail_endpoint(
    album_id: int,
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> AlbumDetail:
    detail = await run_in_threadpool(get_album_detail, handle.lib, album_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Album not found")
    return detail


@router.get("/albums/{album_id}/missing", response_model=AlbumMissingReport)
async def get_album_missing_endpoint(
    album_id: int,
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> AlbumMissingReport:
    """Full release tracklist vs. the library (missing rows + counts). Read-only."""
    return await missing_report_op(handle.lib, album_id)


@router.post("/albums/{album_id}/edit/preview", response_model=AlbumEditPreview)
async def preview_album_edit_endpoint(
    album_id: int,
    payload: AlbumEditRequest,
    request: Request,
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> AlbumEditPreview:
    """Preview a pending album/track edit (field diff + move plan). Read-only."""
    return await preview_album_edit_op(request, album_id, payload)


@router.post("/albums/{album_id}/edit", response_model=AlbumEditResult)
async def edit_album_endpoint(
    album_id: int,
    payload: AlbumEditRequest,
    request: Request,
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> AlbumEditResult:
    """Apply an album/track tag edit (write + config-gated move). 409 if importing."""
    result = await apply_album_edit_op(request, album_id, payload)
    emit_library_changed(request.app)
    return result


@router.get("/albums/{album_id}/cover")
async def get_album_cover_endpoint(
    album_id: int,
    request: Request,
    handle: Annotated[LibraryHandle, Depends(get_library)],
    thumb_cache: Annotated[CoverThumbCache, Depends(get_cover_thumb_cache)],
    size: Annotated[Literal["full", "thumb"], Query()] = "full",
) -> Response:
    lib = handle.lib
    # Validate off a CHEAP stat-based ETag first: the album grid revalidates every
    # cover on every paint (no ?v= buster, no max-age), and the old path re-read the
    # whole image / re-parsed the audio file just to hash it and return a 304.
    # cover_validator stats the source instead, so an unchanged cover answers 304
    # without touching the bytes. Only a validator miss falls through to the read.
    validator = await run_in_threadpool(cover_validator, lib, album_id)
    # The thumb is a DIFFERENT entity than the full image (distinct bytes), so it
    # needs its own ETag under the same URL family — splice a "-t" marker inside
    # the closing quote so the tag stays one opaque quoted string (same scheme as
    # get_artist_image_endpoint). Compute the SIZE-SCOPED tag and run exactly ONE
    # If-None-Match check against it: checking the full tag unconditionally would
    # let a `size=thumb` request 304 off a client's cached FULL etag, serving no
    # body while claiming the (different, larger) thumb is current.
    etag = validator if size == "full" else (f'{validator[:-1]}-t"' if validator else None)
    if etag is not None and if_none_match_hit(request, etag):
        return not_modified(etag)

    if size == "thumb" and validator is not None:
        assert etag is not None  # size != "full" and validator set => etag was built above
        thumb = await run_in_threadpool(
            thumb_cache.get, album_id, validator, lambda: get_album_cover(lib, album_id)
        )
        if thumb is None:
            raise HTTPException(status_code=404, detail="Cover not found")
        return image_response(thumb.data, thumb.content_type, etag)

    # size == "full" OR no stat validator (odd source / a race): existing flow.
    cover = await run_in_threadpool(get_album_cover, lib, album_id)
    if cover is None:
        raise HTTPException(status_code=404, detail="Cover not found")
    image_bytes, mime = cover
    # One of two MEDIA-FILE content-type sinks (the other is the import
    # candidate-cover endpoint, guarded the same way). This mime comes from an
    # artpath extension or an embedded picture's declared MIME rather than a
    # cache sidecar, and today neither can produce an unsendable value — the
    # extension map is fixed, and mediafile re-derives an embedded picture's
    # type from its magic bytes. Defence in depth, then: a value that cannot be
    # a header 500s this endpoint (non-ASCII) or drops the connection with no
    # response at all (a control character), and header_safe_content_type's
    # docstring enumerates the sinks — which is only true if this one is in.
    mime = header_safe_content_type(mime) or FALLBACK_CONTENT_TYPE
    if validator is not None:
        return image_response(image_bytes, mime, validator)
    # No stat validator (odd source / a race): fall back to the content-hash ETag.
    return revalidating_image_response(request, image_bytes, mime)


@router.post("/albums/{album_id}/cover/fetch", dependencies=[Depends(verify_upload_origin)])
async def fetch_album_cover_endpoint(
    album_id: int,
    request: Request,
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> Response:
    """Fetch beets' best cover candidate. Returns the image (preview) or 404. No write.

    Origin-guarded: a body-less POST is a CORS-simple request, so without this a
    foreign page could drive this install's outbound cover lookups. It writes
    nothing, which is why this guard arrived later than the install route's -
    but "changes no state" is not the same as "costs nothing to trigger".
    """
    image_bytes, mime, source = await fetch_cover_op(request, album_id)
    return Response(
        content=image_bytes,
        media_type=mime,
        # The last image response in the app that does not go through the two
        # http_cache constructors, so it needs nosniff spelled out. This mime is
        # already one of four literals from sniff_image_mime's magic-byte check,
        # never a CDN's word - the header is the backstop, and a backstop with
        # one response missing is not one.
        headers={**NO_SNIFF, "Cache-Control": "no-store", "X-Art-Source": source},
    )


@router.post(
    "/albums/{album_id}/cover",
    response_model=CoverInstallResult,
    dependencies=[Depends(verify_upload_origin)],
)
async def install_album_cover_endpoint(
    album_id: int,
    request: Request,
    file: UploadFile,
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> CoverInstallResult:
    """Install an uploaded (or approved-fetched) cover. 409 while importing."""
    # Reject an oversized body before materializing it, when the client declares
    # its size. The post-read length check below remains the authoritative guard
    # (Content-Length is client-supplied and may be absent or wrong).
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > _MAX_COVER_BYTES:
        raise HTTPException(status_code=422, detail="Image too large (max 10 MB)")
    # Bounded read: never buffer more than the cap (+1 to detect an exact-cap
    # overrun) even when Content-Length is absent or understated.
    image_bytes = await file.read(_MAX_COVER_BYTES + 1)
    if len(image_bytes) > _MAX_COVER_BYTES:
        raise HTTPException(status_code=422, detail="Image too large (max 10 MB)")
    result = await install_cover_op(request, album_id, image_bytes)
    emit_art_changed(request.app, f"album:{album_id}")
    return result


@router.post("/albums/{album_id}/lyrics/fetch", response_model=LyricsBackfillStatus)
async def fetch_album_lyrics_endpoint(
    album_id: int,
    request: Request,
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> LyricsBackfillStatus:
    """Start an album-scoped lyrics fetch job (writes tags → Plex). Poll
    GET /api/lyrics/backfill for marching progress. 404 unknown album, 409 if busy."""
    return await start_album_lyrics_op(
        request, album_id, on_complete=lambda: emit_library_changed(request.app)
    )


@router.delete("/albums/{album_id}", response_model=DeleteResult)
async def delete_album_endpoint(album_id: int, request: Request) -> DeleteResult:
    """Move the album's whole folder to Trash (reversible) and drop it from the
    library. 404 unknown album; 409 while a library job is running."""
    result = await delete_album_op(request, album_id)
    emit_library_changed(request.app)
    return result
