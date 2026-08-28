from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool

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
from app.etag import size_scoped_etag
from app.events.emit import emit_art_changed, emit_library_changed
from app.models.album import Album, AlbumDetail, AlbumPage
from app.models.completeness import AlbumMissingReport
from app.models.cover import CoverInstallResult
from app.models.delete import DeleteResult
from app.models.edit import AlbumEditPreview, AlbumEditRequest, AlbumEditResult
from app.models.errors import ErrorDetail, StructuredErrorDetail, validation_or_detail_422
from app.models.lyrics import LyricsBackfillStatus

router = APIRouter(tags=["albums"])

_MAX_COVER_BYTES = 10 * 1024 * 1024

#: The OpenAPI entry for the cover GET's two "no image to send" exits. Named
#: because both the thumb and the full-size arm end there and must not drift
#: apart (see app/models/errors.py for why the model must be named).
_COVER_NOT_FOUND_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": "No album with that id, or the album has no cover art.",
}

#: The album-edit refusals, all raised inside ``app/beets/edit.py``'s ops rather
#: than in the endpoint bodies below - which is exactly why they were easy to
#: leave undeclared. Read the op, not just the endpoint.
_EDIT_ALBUM_NOT_FOUND_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": "No album has that id.",
}
#: A submitted track id that belongs to a different album is well-formed but
#: semantically impossible, so it shares 422 with FastAPI's own validation
#: failures - hence ``validation_or_detail_422`` rather than a bare model, which
#: would REPLACE the validation shape (see app/models/errors.py).
_EDIT_FOREIGN_TRACK_422 = (
    "One of the submitted track ids does not belong to that album, or the"
    " request failed validation."
)

#: The album-scoped lyrics-fetch refusals. Both are raised inside
#: ``start_album_lyrics_op`` (app/beets/lyrics.py), not in the endpoint body.
_LYRICS_ALBUM_NOT_FOUND_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": "No album has that id.",
}
#: TWO independent gates answer with this one status, and the sentence has to
#: cover both unions, because a description that names a subset reads as a
#: complete list:
#:   - ``raise_if_library_busy`` - the whole FIVE-job union (import, lyrics,
#:     artist-art, reorganize, disk-sync) with no exclusions, OR the beets swap
#:     lock, which ELEVEN call sites take (config Apply, album edit, cover
#:     install, artist rename, duplicate resolve x2, delete x2, trash
#:     restore/empty x3) - hence "a beets swap", not a list that would go stale;
#:   - then ``reg.start`` -> ``claim_slot``, which re-checks that union and
#:     additionally refuses when the lyrics slot itself is already taken.
_LYRICS_FETCH_BUSY_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": (
        "A lyrics fetch is already running, or an import, an artist-art job, a"
        " reorganize, a disk sync or a beets swap holds the library."
    ),
}


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


@router.get("/albums")
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


@router.get(
    "/albums/{album_id}",
    responses={
        404: {"model": ErrorDetail, "description": "No album with that id."},
    },
)
async def get_album_detail_endpoint(
    album_id: int,
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> AlbumDetail:
    detail = await run_in_threadpool(get_album_detail, handle.lib, album_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Album not found")
    return detail


@router.get(
    "/albums/{album_id}/missing",
    # Raised inside ``missing_report_op`` (app/beets/completeness.py), which maps
    # the adapter's ``AlbumNotFoundError`` onto 404. Every OTHER failure -
    # including a provider that cannot be reached - is reported as a non-ok
    # ``status`` field on a 200 body, so 404 is the route's only error status.
    responses={404: {"model": ErrorDetail, "description": "No album has that id."}},
)
async def get_album_missing_endpoint(
    album_id: int,
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> AlbumMissingReport:
    """Full release tracklist vs. the library (missing rows + counts). Read-only."""
    return await missing_report_op(handle.lib, album_id)


@router.post(
    "/albums/{album_id}/edit/preview",
    responses={
        404: _EDIT_ALBUM_NOT_FOUND_RESPONSE,
        422: validation_or_detail_422(_EDIT_FOREIGN_TRACK_422),
    },
)
async def preview_album_edit_endpoint(
    album_id: int,
    payload: AlbumEditRequest,
    request: Request,
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> AlbumEditPreview:
    """Preview a pending album/track edit (field diff + move plan). Read-only."""
    return await preview_album_edit_op(request, album_id, payload)


@router.post(
    "/albums/{album_id}/edit",
    responses={
        404: _EDIT_ALBUM_NOT_FOUND_RESPONSE,
        409: {
            "model": ErrorDetail,
            "description": (
                "A library operation is in progress, so the edit is refused until it finishes."
            ),
        },
        422: validation_or_detail_422(_EDIT_FOREIGN_TRACK_422),
        # The blanket ``except Exception`` in ``apply_album_edit_op`` answers
        # with a NESTED detail, not a sentence - declaring it as ErrorDetail
        # would swap an undeclared status for a wrongly-typed one.
        500: {
            "model": StructuredErrorDetail,
            "description": (
                "The edit failed part-way through writing tags or moving files;"
                " the body carries the cause and a recovery hint."
            ),
        },
    },
)
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


@router.get(
    "/albums/{album_id}/cover",
    responses={404: _COVER_NOT_FOUND_RESPONSE},
)
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
    # needs its own ETag under the same URL family — app.etag splices a "-t" marker
    # inside the closing quote so the tag stays one opaque quoted string (same
    # scheme as get_artist_image_endpoint). Compute the SIZE-SCOPED tag and run
    # exactly ONE If-None-Match check against it: checking the full tag
    # unconditionally would let a `size=thumb` request 304 off a client's cached
    # FULL etag, serving no body while claiming the (different, larger) thumb is
    # current.
    etag = size_scoped_etag(validator, size)
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
    # response at all (a control character). The invariant is a predicate, not
    # a list: an external content-type reaching a response header must pass
    # through header_safe_content_type — a reader checks each site by grepping
    # that name, and a guard with one site missing is not a guard.
    mime = header_safe_content_type(mime) or FALLBACK_CONTENT_TYPE
    if validator is not None:
        return image_response(image_bytes, mime, validator)
    # No stat validator (odd source / a race): fall back to the content-hash ETag.
    return revalidating_image_response(request, image_bytes, mime)


@router.post(
    "/albums/{album_id}/cover/fetch",
    responses={
        # The 200 is image bytes; without this entry the generated client is
        # offered a JSON body and never told about the binary one. (FastAPI adds
        # the `application/json` key regardless - see the artist fetch route for
        # why `response_class=Response` is not the answer.)
        200: {"content": {"image/*": {}}, "description": "The candidate cover. No write."},
        # Both of these render {"detail": "..."} at runtime and neither was
        # declared: a description-only entry, or none at all, generates
        # `content?: never` for a body the client has to read. The 403 is the
        # Origin guard, which is invisible in the schema, so this sentence is
        # the only place it is documented.
        403: {"model": ErrorDetail, "description": "The request came from another origin."},
        404: {
            "model": ErrorDetail,
            "description": "No album with that id, or no cover candidate for it.",
        },
        # 422 stays UNDECLARED so FastAPI's HTTPValidationError survives - the
        # path parameter can fail validation and its `detail` is a LIST.
    },
)
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
        # An image response that does not go through the http_cache constructors,
        # so it needs nosniff spelled out. This mime is already one of the four
        # literals from sniff_image_mime's magic-byte check,
        # never a CDN's word - the header is the backstop, and a backstop with
        # one response missing is not one.
        headers={**NO_SNIFF, "Cache-Control": "no-store", "X-Art-Source": source},
    )


@router.post(
    "/albums/{album_id}/cover",
    # Every status this route can answer, each with a NAMED model: a
    # description-only entry drops the `content` block and openapi-typescript
    # renders `content?: never` for a body the client must read (see
    # app/models/errors.py). Most of these are raised inside install_cover_op
    # (app/beets/cover.py), not in the body below, so they are just as real and
    # just as easy to leave undeclared. 400/403 come from the guard overlay.
    responses={
        404: {"model": ErrorDetail, "description": "No album has that id."},
        409: {
            "model": ErrorDetail,
            "description": (
                "A library operation is in progress, so cover changes are refused"
                " until it finishes."
            ),
        },
        # 413 for an oversize payload, the same code (and reason) the playlist
        # artwork upload and the app-wide body-size guard already use. Declared
        # here rather than left to that guard's overlay entry: this route
        # rejects at its OWN 10 MB cap, well under the app-wide limit, so its
        # refusal is the one a client actually meets.
        413: {
            "model": ErrorDetail,
            "description": (
                "The uploaded cover exceeds the 10 MB limit, or the request body"
                " exceeds the app-wide size limit."
            ),
        },
        415: {
            "model": ErrorDetail,
            "description": "The uploaded bytes are not a PNG, JPEG, GIF or WebP image.",
        },
        # An empty album is a well-formed request the library cannot satisfy, so
        # it stays 422 - which means this route returns BOTH 422 bodies (the
        # multipart body and the path parameter can also fail validation, whose
        # `detail` is a LIST). See app/models/errors.py.
        422: validation_or_detail_422(
            "The album has no tracks, so beets cannot place cover art for it; or"
            " the request failed validation."
        ),
        # The blanket ``except Exception`` at the end of ``install_cover_op``
        # answers with a NESTED detail object, not a sentence, and
        # frontend/src/api/lib.ts unwraps exactly that shape. ErrorDetail here
        # would trade an undeclared status for a wrongly-typed one.
        500: {
            "model": StructuredErrorDetail,
            "description": (
                "Writing the cover into the album folder failed (a full disk or"
                " a read-only album directory); the body carries the cause and a"
                " recovery hint."
            ),
        },
    },
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
        raise HTTPException(status_code=413, detail="Image too large (max 10 MB)")
    # Bounded read: never buffer more than the cap (+1 to detect an exact-cap
    # overrun) even when Content-Length is absent or understated.
    image_bytes = await file.read(_MAX_COVER_BYTES + 1)
    if len(image_bytes) > _MAX_COVER_BYTES:
        raise HTTPException(status_code=413, detail="Image too large (max 10 MB)")
    result = await install_cover_op(request, album_id, image_bytes)
    emit_art_changed(request.app, f"album:{album_id}")
    return result


@router.post(
    "/albums/{album_id}/lyrics/fetch",
    responses={
        404: _LYRICS_ALBUM_NOT_FOUND_RESPONSE,
        409: _LYRICS_FETCH_BUSY_RESPONSE,
    },
)
async def fetch_album_lyrics_endpoint(
    album_id: int,
    request: Request,
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> LyricsBackfillStatus:
    """Start an album-scoped lyrics fetch job (writes tags → Plex). Poll
    GET /api/lyrics/backfill for marching progress. 404 unknown album, 409 if busy."""
    return start_album_lyrics_op(
        request, album_id, on_complete=lambda: emit_library_changed(request.app)
    )


@router.delete(
    "/albums/{album_id}",
    # Named models on all four: a description-only entry drops the `content`
    # block and openapi-typescript renders `content?: never` for a body the
    # client must read (see app/models/errors.py). All four are raised inside
    # delete_album_op (app/beets/delete.py), not here.
    responses={
        404: {"model": ErrorDetail, "description": "No album has that id."},
        409: {
            "model": ErrorDetail,
            "description": (
                "A library operation is in progress, so the delete is refused until it finishes."
            ),
        },
        500: {
            "model": StructuredErrorDetail,
            "description": (
                "Deleting the album failed, but its files are recoverable in the Trash folder."
            ),
        },
        # Flat ErrorDetail, unlike the 500 beside it: this one aborts BEFORE any
        # move or row drop, so there is no Trash state to describe and no
        # recovery hint to give beyond remounting.
        503: {
            "model": ErrorDetail,
            "description": (
                "The music library root is missing, empty or unreadable, so the delete is"
                " refused before anything is moved or dropped (the guard against an"
                " unmounted share). Nothing reached the Trash folder."
            ),
        },
    },
)
async def delete_album_endpoint(album_id: int, request: Request) -> DeleteResult:
    """Move the album's whole folder to Trash (reversible) and drop it from the
    library. 404 unknown album; 409 while a library job is running."""
    result = await delete_album_op(request, album_id)
    emit_library_changed(request.app)
    return result
