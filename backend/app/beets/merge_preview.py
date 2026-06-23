"""Build the duplicate comparison table — release-anchored, library vs import.

Read-only adapter logic for the import-side duplicate prompt: diffs the matched
release tracklist against the existing library copy AND the incoming import, per
position, so the UI can render a before/after table that makes Merge / Replace /
Skip legible. Apply-matched only — an as-is import has no matched release to
anchor positions on, so the builder returns ``None`` and the prompt renders
without the table.
"""

from __future__ import annotations

from typing import Any

from app.models.import_models import DuplicateTrackRow, DuplicateTrackState, MergePreview

# Formats beets reports for lossless audio (compared case-insensitively). These
# outrank any lossy format regardless of bitrate.
_LOSSLESS = frozenset({"flac", "alac", "wav", "aiff", "aif", "ape", "wv"})


def _norm_id(value: object) -> str | None:
    """String-normalize a track id. Deezer yields an int ``track_id`` while beets
    stores ``mb_trackid`` as a string, so both sides must be coerced before the
    membership test (a raw compare flags every owned track as missing/added)."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _quality(item: Any | None) -> tuple[str | None, int | None]:
    """``(format, kbps)`` for one item; beets stores bitrate in bps. ``(None, None)`` if absent."""
    if item is None:
        return None, None
    fmt = getattr(item, "format", None)
    raw = int(getattr(item, "bitrate", 0) or 0)
    return (str(fmt) if fmt else None), (raw // 1000 if raw else None)


def _rank(fmt: str | None, kbps: int | None) -> tuple[int, int]:
    """Sortable quality: lossless outranks lossy, then higher bitrate wins."""
    lossless = 1 if (fmt or "").lower() in _LOSSLESS else 0
    return lossless, kbps or 0


def _classify(lib: Any | None, imp: Any | None) -> DuplicateTrackState:
    if lib is not None and imp is not None:
        lib_rank, imp_rank = _rank(*_quality(lib)), _rank(*_quality(imp))
        if imp_rank > lib_rank:
            return DuplicateTrackState.upgrade
        if imp_rank < lib_rank:
            return DuplicateTrackState.downgrade
        return DuplicateTrackState.same
    if lib is not None:
        return DuplicateTrackState.library_only
    if imp is not None:
        return DuplicateTrackState.added
    return DuplicateTrackState.missing


def build_merge_preview(task: Any, found_duplicates: Any) -> MergePreview | None:
    """Per-position library-vs-import comparison for a parked duplicate.

    Returns ``None`` when there is no matched release to anchor on (an as-is
    import). All reads are scalar (ids, format, bitrate) — no path expansion, no
    network (the release tracklist already rode in on ``task.match``).
    """
    match = getattr(task, "match", None)
    info = getattr(match, "info", None) if match is not None else None
    tracks = list(getattr(info, "tracks", None) or []) if info is not None else []
    if not tracks:
        return None

    # release position -> incoming item (from the AlbumMatch item->track mapping)
    import_by_pos: dict[int, Any] = {}
    for item, track_info in getattr(match, "mapping", {}).items():
        idx = getattr(track_info, "index", None)
        if idx is not None:
            import_by_pos[int(idx)] = item

    # normalized release track id -> library item (union across all duplicates)
    lib_by_id: dict[str, Any] = {}
    for album in found_duplicates:
        for it in album.items():
            tid = _norm_id(getattr(it, "mb_trackid", None))
            if tid and tid not in lib_by_id:
                lib_by_id[tid] = it

    rows: list[DuplicateTrackRow] = []
    in_library = added = upgrade = missing = 0
    for t in tracks:
        pos = int(getattr(t, "index", 0) or 0)
        key = _norm_id(getattr(t, "track_id", None))
        lib_item = lib_by_id.get(key) if key else None
        imp_item = import_by_pos.get(pos)
        state = _classify(lib_item, imp_item)
        lib_fmt, lib_kbps = _quality(lib_item)
        imp_fmt, imp_kbps = _quality(imp_item)
        rows.append(
            DuplicateTrackRow(
                position=pos,
                disc=int(getattr(t, "medium", 0) or 1),
                title=str(getattr(t, "title", "") or ""),
                state=state,
                library_format=lib_fmt,
                library_bitrate_kbps=lib_kbps,
                import_format=imp_fmt,
                import_bitrate_kbps=imp_kbps,
            )
        )
        if state is DuplicateTrackState.added:
            added += 1
        elif state is DuplicateTrackState.missing:
            missing += 1
        else:
            in_library += 1
            if state is DuplicateTrackState.upgrade:
                upgrade += 1

    return MergePreview(
        rows=rows,
        total=len(rows),
        in_library_count=in_library,
        added_count=added,
        upgrade_count=upgrade,
        missing_count=missing,
    )
