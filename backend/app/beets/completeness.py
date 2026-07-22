"""Library-side 'missing tracks' adapter — read-only, on the beets boundary.

Re-fetches an album's official release by ``mb_albumid`` (a library album has no
AlbumMatch, so the import-side _missing_tracks does not transfer) and classifies
each release track present-iff its ``track_id`` is in ``{item.mb_trackid}``.

We call the resolved source plugin's ``album_for_id`` DIRECTLY rather than
``metadata_plugins.album_for_id`` because the latter swallows every exception to
None under the default ``raise_on_error: no`` — making a network outage
indistinguishable from "not found". Direct: bad/deleted id -> None
(release_unavailable); network/timeout -> requests exception (fetch_failed). See
memory ``beets-missing-tracks-contract``.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import requests
from beets import metadata_plugins
from beets.library import Library

from app.models.completeness import AlbumMissingReport, MissingReleaseTrack, ReportStatus

_log = logging.getLogger("musicdrop.completeness")

# Immutable release data keyed by mb_albumid. beets caches nothing (missing.py
# has a TODO); AlbumInfo holds no live config/plugin refs so it is safe to keep
# across a config-apply / reset_beets_globals.
_CACHE_MAX = 256
_CACHE: OrderedDict[str, Any] = OrderedDict()


class AlbumNotFoundError(Exception):
    """A referenced album id is not in the library. Maps to 404."""


def clear_release_cache() -> None:
    """Drop all cached release fetches (used by tests; safe to call anytime)."""
    _CACHE.clear()


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]  # beets value is untyped
    except (TypeError, ValueError):
        return None


@dataclass
class _FetchResult:
    info: Any | None = None
    status: ReportStatus = "ok"


def _fetch_release(mb_albumid: str, data_source: str) -> _FetchResult:
    cached = _CACHE.get(mb_albumid)
    if cached is not None:
        try:
            _CACHE.move_to_end(mb_albumid)
        except KeyError:
            # The LRU promote is check-then-act: on a threadpool a concurrent
            # insert can evict this exact key between the get above and here, so
            # move_to_end raises. The read already succeeded — the promote is
            # best-effort, so swallow it rather than 500 the missing-tracks report.
            pass
        return _FetchResult(info=cached)
    source = metadata_plugins.get_metadata_source(data_source)
    if source is None:
        return _FetchResult(status="release_unavailable")  # source not loaded
    try:
        info = source.album_for_id(mb_albumid)
    except requests.exceptions.RequestException:
        _log.warning("missing-tracks fetch failed for %s", mb_albumid, exc_info=True)
        return _FetchResult(status="fetch_failed")
    if info is None:
        return _FetchResult(status="release_unavailable")  # bad/deleted id
    _CACHE[mb_albumid] = info
    _CACHE.move_to_end(mb_albumid)
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return _FetchResult(info=info)


def _empty(status: ReportStatus, source: str | None = None) -> AlbumMissingReport:
    return AlbumMissingReport(status=status, total=0, present_count=0, missing=[], source=source)


def _build_report(info: Any, present_ids: set[str], data_source: str) -> AlbumMissingReport:
    # MissingReleaseTrack.mb_trackid normalizes the id to a string; matching on
    # that field (not the raw track_id) is essential because a Deezer release
    # yields an integer track_id while the library stores mb_trackid as a string
    # — a raw int-vs-str test would flag every owned track as missing.
    # present_ids is already string-normalized by the caller.
    rows = [
        MissingReleaseTrack(
            index=int(getattr(t, "index", 0) or 0),
            disc=int(getattr(t, "medium", 0) or 1),
            title=str(getattr(t, "title", "") or ""),
            duration_seconds=_optional_float(getattr(t, "length", None)),
            mb_trackid=_optional_str(getattr(t, "track_id", None)),
        )
        for t in info.tracks
    ]
    missing = [r for r in rows if r.mb_trackid not in present_ids]
    total = len(rows)
    return AlbumMissingReport(
        status="ok",
        total=total,
        present_count=total - len(missing),
        missing=missing,
        source=data_source,
    )


def release_missing_report(lib: Library, album_id: int) -> AlbumMissingReport:
    """Full release tracklist diffed against the library; missing rows + counts.

    Raises AlbumNotFoundError when the album id is unknown. All fetch failures map
    to a non-ok status (returned, not raised) so the endpoint stays 200.
    """
    with lib.music_dir_context():  # cheap insurance; scalar reads don't need it
        album = lib.get_album(album_id)
        if album is None:
            raise AlbumNotFoundError(f"album {album_id} not found")
        if not album.mb_albumid:
            return _empty("no_musicbrainz_id")
        items = list(album.items())
        present_ids = {str(it.mb_trackid) for it in items if it.mb_trackid}
        if not present_ids:
            # MB album but nothing carries an id -> can't classify; avoid a
            # misleading "everything missing".
            return _empty("no_musicbrainz_id")
        data_source = (
            album.get("data_source")
            or (items[0].get("data_source") if items else None)
            or "MusicBrainz"
        )
        result = _fetch_release(album.mb_albumid, data_source)
    if result.info is None:
        # Carry the resolved source so a fetch failure can be labelled with the
        # provider it actually tried (e.g. "Couldn't reach Deezer"), not a
        # hardcoded "MusicBrainz". (no_musicbrainz_id returns earlier, source=None.)
        return _empty(result.status, data_source)
    return _build_report(result.info, present_ids, data_source)


async def missing_report_op(lib: Library, album_id: int) -> AlbumMissingReport:
    """Threadpool the (network-touching) read; map a missing album to 404."""
    from fastapi import HTTPException
    from fastapi.concurrency import run_in_threadpool

    try:
        return await run_in_threadpool(release_missing_report, lib, album_id)
    except AlbumNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
