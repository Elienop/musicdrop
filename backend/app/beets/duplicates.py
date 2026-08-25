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
from functools import lru_cache
from pathlib import Path
from typing import Any

from beets import config
from beets.library import Album, Library
from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool

# Reuse config-Apply's lock + settings accessors so resolve shares the SAME
# app.state.beets_swap_lock (genuine mutual exclusion with Apply) and the same
# lifespan-less TestClient fallback. Both live in the app/beets/ boundary.
from app.beets.config_editor import _settings, _swap_lock
from app.beets.existing_album import to_existing_album
from app.beets.library import (
    LibraryHandle,
    _album_fields,
    _album_genre,
    _coerce_optional_str,
    _coerce_str,
)
from app.beets.trash import album_folder, album_format_bitrate, resolve_trash_dir, trash_album
from app.library_busy import library_job_active
from app.models.duplicates import (
    DuplicateAlbum,
    DuplicateGroup,
    DuplicateMode,
    DuplicatesReport,
    GroupDecision,
    MovedAlbum,
    ResolveAllRequest,
    ResolveAllResult,
    ResolveRequest,
    ResolveResult,
    SkippedGroup,
)
from app.models.import_models import ExistingAlbum

_PAREN_RE = re.compile(r"[\(\[].*?[\)\]]")
_FEAT_RE = re.compile(r"\b(?:feat|ft|featuring)\b.*", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

_MATCH_REASON = {"mb": "MusicBrainz album id", "fuzzy": "artist + album title"}


def _soft_normalize(text: str) -> str:
    """Everything :func:`normalize` does EXCEPT removing punctuation.

    Split out so :func:`normalize` is literally "this, then drop punctuation" —
    the two share one pipeline and cannot drift apart. Used on its own only as
    :func:`_fuzzy_part`'s second rung.
    """
    text = _PAREN_RE.sub(" ", text.casefold())
    text = _FEAT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def normalize(text: str) -> str:
    """Normalize an artist/album string for fuzzy duplicate grouping.

    Lowercases, drops parentheticals (``(deluxe)``/``[remastered]``) and
    ``feat.`` clauses, removes punctuation, and collapses whitespace. NOT a
    beets behavior — see module docstring.
    """
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", _soft_normalize(text))).strip()


# Pure text -> text, so safe to memoize; the import gate's variant index re-runs
# the ladder over every album title on each rebuild, and an applied import run
# rebuilds per task — the memo turns those repeat rebuilds into dict hits. The
# 64k bound caps worst-case memory around 15-20 MB (measured with realistic
# 50-char titles); typical libraries stay far below the cap.
@lru_cache(maxsize=65536)
def _fuzzy_part(text: str) -> str:
    """One half of the fuzzy signal: the strongest normalization that still says
    something. Three rungs, each used only when the one above empties the value.

    :func:`normalize` strips ALL punctuation, so a symbol-only value collapses to
    "" — it maps ``=``, ``+`` and U+2212 MINUS SIGN all to the empty string.
    Without a fallback every such value compares EQUAL, so Ed Sheeran's three
    symbol-titled albums emit one identical ``fuzzy:ed sheeran\\x00`` signal and
    group as a single duplicate set.

    Rung 2 (:func:`_soft_normalize`) keeps the punctuation but still folds
    parentheticals, ``feat.`` clauses and whitespace, so ``=`` and
    ``= (Deluxe Edition)`` — the commonest real duplicate shape — still group,
    which a raw ``casefold()`` fallback would have missed. Rung 3 is that raw
    ``casefold()``, reached only when rung 2 is ALSO empty: a title that is
    nothing but a parenthetical (Sigur Rós' ``( )``) folds to "" under rung 2,
    and without rung 3 it would compare equal to every other such title by the
    same artist — the original bug in a narrower shape.

    What this trades away, stated exactly: two copies whose titles both empty
    :func:`normalize` group only when their folded forms agree — and when those
    are empty too, only when the raw text agrees. That covers byte-identical
    titles and the deluxe/standard pair; it misses a pair that is
    parenthetical-only on BOTH sides with differing raw text (``( )`` vs
    ``( ) (Remastered)``). Deliberate — a missed duplicate is recoverable, an
    unrelated album filed away by *Resolve all* is not.

    A genuinely empty value stays empty through all three rungs: the ladder
    distinguishes "there was no text" (absent — contributes no signal) from
    "there was text and normalization ate it" (present — keep it).
    ``playlist_match._title_key`` falls back for the same reason but stops at the
    raw form, because there the value being rescued IS an all-parenthetical track
    title ("(Intro)"), which rung 2 would empty.
    """
    return normalize(text) or _soft_normalize(text) or text.casefold().strip()


def _grouping_signals(album: Any, mode: DuplicateMode) -> list[str]:
    """The grouping signals for an album — albums sharing ANY signal are one dup
    group. Empty list = no usable key, so the album is dropped from detection
    (mirrors beets ``_group_by`` null handling).

    Strict mode = MB-id only (beets album-mode default). Fuzzy mode adds the
    normalized artist+title signal ON TOP of the MB-id one, so fuzzy stays a
    proper superset of strict AND catches the two real dup shapes MB-precedence
    alone missed: an MB-tagged copy paired with an untagged/as-is copy (only the
    tagged one has an ``mb:`` signal, but both share the ``fuzzy:`` one), and two
    distinct releases of the same album (different MBIDs, same fuzzy key).

    "Same fuzzy key" is NOT always "same normalized title": both halves go
    through :func:`_fuzzy_part`, so a value that :func:`normalize` empties is
    keyed on its folded or raw form instead of comparing equal to every other
    such value. For a symbol-only title (``=``) that means two releases group
    only if the glyph agrees after parenthetical folding — see that docstring for
    exactly what the ladder catches and what it gives up. The ``artist or title``
    guard therefore still means "no usable key", never "normalization ate the
    only key I had".
    """
    signals: list[str] = []
    mb = _coerce_optional_str(album.get("mb_albumid"))
    if mb is not None:
        signals.append(f"mb:{mb}")
    if mode is DuplicateMode.fuzzy:
        artist = _fuzzy_part(_coerce_optional_str(album.albumartist) or "")
        title = _fuzzy_part(_coerce_optional_str(album.album) or "")
        if artist or title:
            signals.append(f"fuzzy:{artist}\x00{title}")
    return signals


def _ufind(parent: list[int], x: int) -> int:
    """Union-find root of ``x`` with path halving (mutates ``parent``)."""
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def _union_find_link(albums: list[Any], mode: DuplicateMode) -> tuple[list[int], list[bool]]:
    """Link albums that share any grouping signal; return ``(parent, has_signal)``.

    ``parent`` is the union-find table after linking; ``has_signal`` marks each
    album that contributed at least one signal (those without are later excluded
    from grouping, mirroring beets ``_group_by`` null handling).
    """
    parent = list(range(len(albums)))
    signal_owner: dict[str, int] = {}
    has_signal: list[bool] = []
    for idx, album in enumerate(albums):
        signals = _grouping_signals(album, mode)
        has_signal.append(bool(signals))
        for sig in signals:
            owner = signal_owner.setdefault(sig, idx)
            if owner != idx:
                ra, rb = _ufind(parent, idx), _ufind(parent, owner)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    return parent, has_signal


def _components_to_groups(
    albums: list[Any], parent: list[int], has_signal: list[bool]
) -> list[tuple[list[Any], bool]]:
    """Collapse each union-find root into its member list, drop singletons, and
    tag each group with ``all_share_mb`` (every member carries the same MBID)."""
    components: dict[int, list[Any]] = {}
    for idx, album in enumerate(albums):
        if has_signal[idx]:
            components.setdefault(_ufind(parent, idx), []).append(album)

    out: list[tuple[list[Any], bool]] = []
    for members in components.values():
        if len(members) < 2:
            continue
        mbids = {_coerce_optional_str(a.get("mb_albumid")) for a in members}
        all_share_mb = len(mbids) == 1 and None not in mbids
        out.append((members, all_share_mb))
    return out


def _group_by_shared_signal(albums: list[Any], mode: DuplicateMode) -> list[tuple[list[Any], bool]]:
    """Union albums that share any grouping signal into connected components.

    Returns ``(members, all_share_mb)`` per component with >= 2 members (albums
    with no signal are excluded). ``all_share_mb`` drives the match reason: a
    component whose members all carry the SAME MBID is an MB-id match; anything
    bridged by the normalized artist+title signal (mixed/absent MBIDs) is an
    artist+title match. Union-find keeps the grouping transitive — A~B via MBID,
    B~C via title folds all three together.
    """
    parent, has_signal = _union_find_link(albums, mode)
    return _components_to_groups(albums, parent, has_signal)


def _to_duplicate_album(lib: Library, album: Any, *, is_keeper: bool) -> DuplicateAlbum:
    items = list(album.items())
    fmt, bitrate_kbps = album_format_bitrate(items)
    return DuplicateAlbum(
        **_album_fields(album, track_count=len(items), genre=_album_genre(album, items)),
        format=fmt,
        bitrate_kbps=bitrate_kbps,
        folder=album_folder(lib, items),
        is_suggested_keeper=is_keeper,
    )


def find_duplicate_albums(lib: Library, *, mode: DuplicateMode) -> DuplicatesReport:
    """Scan the whole library for duplicate album groups.

    Read-only. ``lib.albums()`` (no query) returns every album; albums are
    grouped by :func:`_group_by_shared_signal`, singletons dropped, members
    ordered keeper-first by track count (beets ``_order``).
    """
    groups: list[DuplicateGroup] = []
    for albums, all_share_mb in _group_by_shared_signal(list(lib.albums()), mode):
        ordered = sorted(albums, key=lambda a: len(a.items()), reverse=True)
        keeper_id = int(ordered[0].id)
        members = [_to_duplicate_album(lib, a, is_keeper=(int(a.id) == keeper_id)) for a in ordered]
        reason = _MATCH_REASON["mb" if all_share_mb else "fuzzy"]
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


def find_import_duplicates(
    lib: Library,
    *,
    albumartist: str | None,
    album: str | None,
    year: int | None = None,
    mb_albumid: str | None = None,
    exclude_under: str | None = None,
) -> list[ExistingAlbum]:
    """Library albums that the matched release would duplicate.

    Faithful to beets' ``AlbumImportTask.find_duplicates`` (importer/tasks.py:391):
    builds a transient Album from the *matched release's* metadata, queries the
    library with beets' own ``duplicates_query`` over the configured
    ``import.duplicate_keys.album`` (default ``albumartist album``), and drops
    any existing album whose files all live under ``exclude_under`` (a re-import
    of the same folder is not a collision; tasks.py:410-420). Read-only.

    It shares NO machinery with the fuzzy detection above — it matches exact
    field values through beets' own query and never calls :func:`normalize`,
    :func:`_fuzzy_part` or :func:`_grouping_signals` (instrumented, 0 calls), so
    changes to fuzzy grouping cannot reach the pre-import collision warning in
    ``api/import_.py`` / ``api/bank.py``. Verified so the next reader need not
    re-derive it.
    """
    if not albumartist:
        return []  # mirrors beets' as-is/no-artist guard
    info: dict[str, Any] = {"albumartist": albumartist, "album": album}
    if year is not None:
        info["year"] = year
    if mb_albumid:
        info["mb_albumid"] = mb_albumid
    tmp_album = Album(lib, **info)
    keys: list[str] = config["import"]["duplicate_keys"]["album"].as_str_seq()
    dup_query = tmp_album.duplicates_query(keys)
    # realpath (not abspath) on BOTH sides: beets stores item paths under the
    # real library dir, but exclude_under (the banked/parked source folder) can
    # reach the same files through a symlinked alias (the documented TrueNAS
    # layout). abspath leaves the two alias strings distinct, so an in-library
    # re-import's own copy would be wrongly flagged as a collision. This mirrors
    # is_in_library_source's realpath hardening in import_session.py.
    excl = os.path.realpath(exclude_under) if exclude_under else None
    out: list[ExistingAlbum] = []
    with lib.music_dir_context():
        for album_obj in lib.albums(dup_query):
            if excl is not None:
                items = list(album_obj.items())
                paths = [os.path.realpath(os.fsdecode(i.path)) for i in items if i.path]
                if paths and all(p == excl or p.startswith(excl + os.sep) for p in paths):
                    continue
            out.append(to_existing_album(lib, album_obj))
    return out


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

    Binds ``lib.music_dir_context()`` for the whole operation: beets stores
    item paths relative to the library dir and re-expands them to absolute on load
    via a ``ContextVar`` (``beets.context``) set when the ``Library`` is opened.
    The API runs this in a FastAPI threadpool thread that does NOT inherit that
    ``ContextVar``, so without the bind ``Album.move`` gets a relative source path
    and raises ``FileNotFoundError``. Reentrant/cheap, so the per-group bind in
    :func:`resolve_all_groups`'s loop is also safe.
    """
    with lib.music_dir_context():
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
        moved: list[MovedAlbum] = []
        with lib.transaction():
            for album_id in remove_album_ids:
                album = lib.get_album(album_id)
                if album is None:
                    raise AlbumNotFoundError(f"album {album_id} not found")
                album_artist = _coerce_str(album.albumartist)
                title = _coerce_str(album.album)
                trash_path = trash_album(lib, album, trash_dir=trash_dir)
                moved.append(
                    MovedAlbum(
                        id=album_id,
                        album_artist=album_artist,
                        title=title,
                        trash_path=trash_path,
                    )
                )
        return ResolveResult(kept_album_id=keep_album_id, moved=moved)


async def resolve_duplicates_op(request: Request, req: ResolveRequest) -> ResolveResult:
    """Resolve a duplicate group, serialized against imports and config Apply.

    Mirrors :func:`app.beets.config_editor.apply`:

    1. **Import gate** (409) — refuse while an import is active. Moving files +
       dropping DB rows under a live import worker would corrupt it. Best-effort
       TOCTOU, accepted for the single-user self-host case exactly as Apply does.
    2. **Shared lock** — ``app.state.beets_swap_lock`` (via ``_swap_lock``) so
       resolve and Apply (and concurrent resolves) never overlap.
    3. **Threadpool** — beets file moves + SQLite are blocking; offload them.
    4. **Error mapping** — StaleGroupError → 409, AlbumNotFoundError → 404, any
       other failure → structured 500 ``{message, recovery}`` (the nested shape
       config Apply uses; flat ``detail: str`` for the 409/404 siblings).
    """
    app = request.app
    if library_job_active():
        raise HTTPException(
            status_code=409,
            detail="Import in progress; resolve available when it finishes",
        )
    async with _swap_lock(app):
        handle: LibraryHandle = app.state.beets_library
        trash_dir = resolve_trash_dir(_settings(app), handle)
        try:
            return await run_in_threadpool(
                resolve_duplicate_group,
                handle.lib,
                mode=req.mode,
                keep_album_id=req.keep_album_id,
                remove_album_ids=req.remove_album_ids,
                trash_dir=trash_dir,
            )
        except StaleGroupError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AlbumNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={
                    "message": f"Resolve failed: {exc}",
                    "recovery": (
                        "Moved copies are recoverable in the Trash folder. "
                        "Refresh the report and retry."
                    ),
                },
            ) from exc


def resolve_all_groups(
    lib: Library,
    *,
    mode: DuplicateMode,
    groups: list[GroupDecision],
    trash_dir: Path,
) -> ResolveAllResult:
    """Resolve many duplicate groups in one pass.

    Loops :func:`resolve_duplicate_group` per group (each re-detects with ``mode``
    and moves its losers to Trash). A group that drifted since the report
    (:class:`StaleGroupError`) is SKIPPED and recorded — the rest still resolve
    (bulk ops self-heal rather than failing wholesale). ``AlbumNotFoundError``
    and any other fault propagate; copies already moved stay safe in Trash.
    """
    resolved: list[ResolveResult] = []
    skipped: list[SkippedGroup] = []
    # Per-group re-detection (resolve_duplicate_group re-scans the library each
    # call) is intentional, not an oversight: the library mutates as earlier
    # groups resolve, so a single cached report would be stale by the time later
    # groups run. The O(groups x albums) cost is fine at single-user scale.
    for decision in groups:
        try:
            result = resolve_duplicate_group(
                lib,
                mode=mode,
                keep_album_id=decision.keep_album_id,
                remove_album_ids=decision.remove_album_ids,
                trash_dir=trash_dir,
            )
        except StaleGroupError as exc:
            skipped.append(SkippedGroup(keep_album_id=decision.keep_album_id, reason=str(exc)))
            continue
        resolved.append(result)
    return ResolveAllResult(
        resolved=resolved,
        skipped_stale=skipped,
        group_count=len(resolved),
        moved_count=sum(len(r.moved) for r in resolved),
    )


async def resolve_all_op(request: Request, req: ResolveAllRequest) -> ResolveAllResult:
    """Batch resolve, serialized against imports + config Apply.

    Mirrors :func:`resolve_duplicates_op`: import gate (409), the shared
    ``beets_swap_lock`` acquired ONCE for the whole batch, threadpool, error
    mapping. ``StaleGroupError`` is handled per-group inside
    :func:`resolve_all_groups` (skipped), so only ``AlbumNotFoundError`` (404)
    and unexpected faults (500) surface here. An all-stale batch is a normal
    200 with an empty ``resolved`` + populated ``skipped_stale``.
    """
    app = request.app
    if library_job_active():
        raise HTTPException(
            status_code=409,
            detail="Import in progress; resolve available when it finishes",
        )
    async with _swap_lock(app):
        handle: LibraryHandle = app.state.beets_library
        trash_dir = resolve_trash_dir(_settings(app), handle)
        try:
            return await run_in_threadpool(
                resolve_all_groups,
                handle.lib,
                mode=req.mode,
                groups=req.groups,
                trash_dir=trash_dir,
            )
        except AlbumNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={
                    "message": f"Resolve failed: {exc}",
                    "recovery": (
                        "Any moved copies are recoverable in the Trash folder. "
                        "Refresh the report and retry."
                    ),
                },
            ) from exc
