"""Duplicate Albums — detection + resolution (the sole beets importer here).

Detection replicates beets' pure ``duplicates`` logic (``beetsplug/duplicates``
``_group_by``/``_order``/``_duplicates``) over ``lib.albums()`` — side-effect
free, so it is read-only and synchronous. ``normalize`` (fuzzy mode) is the only
piece NOT from beets; beets groups on exact field values, fuzzy layers a
normalized key on the same algorithm.

Resolution (Task 3+4) moves the non-kept copies to a Trash folder and drops them
from the library — exactly ``beet dup --move <trash> --remove`` for albums.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from beets.library import Library
from beets.util import bytestring_path

from app.beets.library import (
    _abs_path,
    _album_fields,
    _coerce_int,
    _coerce_optional_str,
    _coerce_str,
)
from app.models.duplicates import (
    DuplicateAlbum,
    DuplicateGroup,
    DuplicateMode,
    DuplicatesReport,
    MovedAlbum,
    ResolveResult,
)

_PAREN_RE = re.compile(r"[\(\[].*?[\)\]]")
_FEAT_RE = re.compile(r"\b(?:feat|ft|featuring)\b.*", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

_MATCH_REASON = {"mb": "MusicBrainz album id", "fuzzy": "artist + album title"}


def normalize(text: str) -> str:
    """Normalize an artist/album string for fuzzy duplicate grouping.

    Lowercases, drops parentheticals (``(deluxe)``/``[remastered]``) and
    ``feat.`` clauses, removes punctuation, and collapses whitespace. NOT a
    beets behavior — see module docstring.
    """
    text = text.casefold()
    text = _PAREN_RE.sub(" ", text)
    text = _FEAT_RE.sub(" ", text)
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _grouping_key(album: Any, mode: DuplicateMode) -> str | None:
    """Return an album's duplicate-grouping key, or ``None`` to exclude it.

    Mirrors beets ``_group_by`` null handling: an album with no usable key is
    dropped from detection.
    """
    mb = _coerce_optional_str(album.get("mb_albumid"))
    if mb is not None:
        return f"mb:{mb}"
    if mode is DuplicateMode.strict:
        return None  # strict = MB-id only (beets album-mode default)
    artist = normalize(_coerce_optional_str(album.albumartist) or "")
    title = normalize(_coerce_optional_str(album.album) or "")
    if not artist and not title:
        return None
    return f"fuzzy:{artist}\x00{title}"


def _format_bitrate(items: list[Any]) -> tuple[str | None, int | None]:
    """Quality hint from the album's representative (first) item."""
    if not items:
        return None, None
    first = items[0]
    fmt = _coerce_optional_str(getattr(first, "format", None))
    raw = _coerce_int(getattr(first, "bitrate", 0))  # beets stores bitrate in bps
    return fmt, (raw // 1000 if raw else None)


def _folder(lib: Library, items: list[Any]) -> str:
    if not items:
        return ""
    return os.path.dirname(_abs_path(lib, items[0].path))


def _to_duplicate_album(lib: Library, album: Any, *, is_keeper: bool) -> DuplicateAlbum:
    items = list(album.items())
    fmt, bitrate_kbps = _format_bitrate(items)
    return DuplicateAlbum(
        **_album_fields(album, items),
        format=fmt,
        bitrate_kbps=bitrate_kbps,
        folder=_folder(lib, items),
        is_suggested_keeper=is_keeper,
    )


def find_duplicate_albums(lib: Library, *, mode: DuplicateMode) -> DuplicatesReport:
    """Scan the whole library for duplicate album groups.

    Read-only. ``lib.albums()`` (no query) returns every album; albums are
    grouped by :func:`_grouping_key`, singletons dropped, members ordered
    keeper-first by track count (beets ``_order``).
    """
    by_key: dict[str, list[Any]] = {}
    for album in lib.albums():
        key = _grouping_key(album, mode)
        if key is None:
            continue
        by_key.setdefault(key, []).append(album)

    groups: list[DuplicateGroup] = []
    for key, albums in by_key.items():
        if len(albums) < 2:
            continue
        ordered = sorted(albums, key=lambda a: len(a.items()), reverse=True)
        keeper_id = int(ordered[0].id)
        members = [_to_duplicate_album(lib, a, is_keeper=(int(a.id) == keeper_id)) for a in ordered]
        reason = _MATCH_REASON["mb" if key.startswith("mb:") else "fuzzy"]
        groups.append(
            DuplicateGroup(match_reason=reason, suggested_keeper_id=keeper_id, members=members)
        )

    # Stable display order: biggest groups first, then keeper title.
    groups.sort(key=lambda g: (-len(g.members), g.members[0].title.casefold()))
    return DuplicatesReport(
        mode=mode,
        group_count=len(groups),
        album_count=sum(len(g.members) for g in groups),
        groups=groups,
    )


class StaleGroupError(Exception):
    """The requested group no longer matches the current library state.

    Raised when the keeper is no longer in any duplicate group, or the claimed
    ``remove_album_ids`` are not exactly that group's other members. Maps to 409.
    """


class AlbumNotFoundError(Exception):
    """A referenced album id is not in the library. Maps to 404."""


def resolve_duplicate_group(
    lib: Library,
    *,
    mode: DuplicateMode,
    keep_album_id: int,
    remove_album_ids: list[int],
    trash_dir: Path,
) -> ResolveResult:
    """Move ``remove_album_ids`` to ``trash_dir`` and drop them from the library.

    Re-verifies the group with the same ``mode`` detection the client saw, so a
    library that changed underneath the user (stale UI) raises
    :class:`StaleGroupError` instead of mutating the wrong albums. Each loser is
    relocated with beets' own ``Album.move(basedir=trash)`` (files move under
    Trash by path template, the vacated source dir is pruned) then
    ``Album.remove(delete=False)`` (DB rows dropped, files remain in Trash) —
    exactly ``beet dup --move <trash> --remove`` for albums.
    """
    report = find_duplicate_albums(lib, mode=mode)
    target = next(
        (g for g in report.groups if any(m.id == keep_album_id for m in g.members)),
        None,
    )
    if target is None:
        raise StaleGroupError("keep album is no longer part of a duplicate group")
    current_others = {m.id for m in target.members} - {keep_album_id}
    if set(remove_album_ids) != current_others:
        raise StaleGroupError("duplicate group membership changed")

    trash_dir.mkdir(parents=True, exist_ok=True)
    basedir = bytestring_path(str(trash_dir))
    moved: list[MovedAlbum] = []
    with lib.transaction():
        for album_id in remove_album_ids:
            album = lib.get_album(album_id)
            if album is None:
                raise AlbumNotFoundError(f"album {album_id} not found")
            album.move(basedir=basedir)  # relocate under Trash + prune source dir
            items = list(album.items())
            trash_path = os.path.dirname(_abs_path(lib, items[0].path)) if items else str(trash_dir)
            moved.append(
                MovedAlbum(
                    id=int(album.id),
                    album_artist=_coerce_str(album.albumartist),
                    title=_coerce_str(album.album),
                    trash_path=trash_path,
                )
            )
            album.remove(delete=False)  # drop DB rows; files stay in Trash
    return ResolveResult(kept_album_id=keep_album_id, moved=moved)
