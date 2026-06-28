from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile

from app.api.http_cache import revalidating_image_response
from app.beets.completeness import missing_report_op
from app.beets.cover import fetch_cover_op, install_cover_op
from app.beets.delete import delete_album_op
from app.beets.edit import apply_album_edit_op, preview_album_edit_op
from app.beets.library import LibraryHandle, get_album_cover, get_album_detail, list_albums
from app.beets.lyrics import start_album_lyrics_op
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


@router.get("/albums", response_model=AlbumPage)
async def list_albums_endpoint(
    handle: Annotated[LibraryHandle, Depends(get_library)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    artist: Annotated[str | None, Query()] = None,
) -> AlbumPage:
    items: list[Album]
    items, total = list_albums(handle.lib, limit=limit, offset=offset, artist=artist)
    return AlbumPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/albums/{album_id}", response_model=AlbumDetail)
async def get_album_detail_endpoint(
    album_id: int,
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> AlbumDetail:
    detail = get_album_detail(handle.lib, album_id)
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
) -> Response:
    cover = get_album_cover(handle.lib, album_id)
    if cover is None:
        raise HTTPException(status_code=404, detail="Cover not found")

    image_bytes, mime = cover
    # Revalidate every time (no max-age) so a freshly-edited cover shows up
    # immediately — no hard refresh, and correct even for the roster grid, which
    # fetches /cover with no ?v= buster. See app.api.http_cache.
    return revalidating_image_response(request, image_bytes, mime)


@router.post("/albums/{album_id}/cover/fetch")
async def fetch_album_cover_endpoint(
    album_id: int,
    request: Request,
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> Response:
    """Fetch beets' best cover candidate. Returns the image (preview) or 404. No write."""
    image_bytes, mime, source = await fetch_cover_op(request, album_id)
    return Response(
        content=image_bytes,
        media_type=mime,
        headers={"Cache-Control": "no-store", "X-Art-Source": source},
    )


@router.post("/albums/{album_id}/cover", response_model=CoverInstallResult)
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
    image_bytes = await file.read()
    if len(image_bytes) > _MAX_COVER_BYTES:
        raise HTTPException(status_code=422, detail="Image too large (max 10 MB)")
    result = await install_cover_op(request, album_id, image_bytes)
    emit_art_changed(request.app)
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
