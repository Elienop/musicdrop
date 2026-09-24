"""Offline re-lookup for banked folders — search re-lookup AND default-lookup rescan.

Reads a banked folder's audio files into DETACHED beets Items (``Item.from_path``,
no library DB — the trash_manage pattern) and re-matches them in one of two
modes: the SEARCH re-lookup (a release id pin / name search, via
:func:`relookup_source`) that powers the bank's "search for a different release",
or the RESCAN — beets' DEFAULT first-scan ``tag_album`` with no search terms,
the "I changed the folder on purpose" re-read. Either way the winner is mapped
through the same mappers the sweep uses, so the payload is exactly what the
review screens render. Preview-only: no file moves, no library writes, no beets
config mutation (``search_ids`` is a ``tag_album`` argument, never set on config).

Thread-safety: may run on an API threadpool thread while a live import's own
lookups are in flight. beets' HTTP layer makes that safe by construction:
``TimeoutAndRetrySession`` is a singleton ``requests.Session`` whose
``RateLimitAdapter`` paces every request under its own ``threading.Lock``
(2.14.0 beetsplug/_utils/requests.py:44-62, :65, :106-122).

beets imports are allowed here (inside app/beets/, CLAUDE.md rule 3).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from beets.autotag import Source
from beets.autotag.match import Recommendation as BeetsRec
from beets.autotag.match import tag_album
from beets.library import Item as Item  # explicit re-export: tests patch res.Item.from_path

from app.beets.import_mapping import (
    _REC_MAP,
    _confidence,
    _opt_str,
    embedded_art,
    map_album_match,
    map_candidate_options,
)
from app.beets.relookup import relookup_source
from app.models.import_models import Candidate, Recommendation

if TYPE_CHECKING:
    from app.models.import_models import ImportSearch


@dataclass(frozen=True)
class ResearchResult:
    """One successful re-lookup: the payload plus the row-summary fields."""

    candidate: Candidate
    artist: str | None
    album: str | None
    recommendation: Recommendation
    confidence: float


def _read_items(folder: Path) -> list[Item]:
    """Detached items for every readable media file under ``folder``.

    Non-media files (art, logs) raise inside ``Item.from_path`` and are
    skipped. Path-sorted so candidate mapping is deterministic.
    """
    paths: list[str] = []
    for root, _dirs, files in os.walk(folder):
        for name in files:
            paths.append(os.path.join(root, name))
    items: list[Item] = []
    for path in sorted(paths):
        try:
            items.append(Item.from_path(os.fsencode(path)))
        except Exception:  # beets raises assorted errors on non-media
            continue
    return items


class NoAudioFilesError(Exception):
    """The folder exists but holds no readable audio files."""


@dataclass(frozen=True)
class RescanOutcome:
    """A default-lookup rescan. ``result`` None = the lookup matched nothing;
    the ``cur_*`` identity and recommendation still describe the fresh read so
    a no-match outcome can refresh the row's summary fields (confidence 0.0,
    the sweep's own no-match posture)."""

    result: ResearchResult | None
    cur_artist: str | None
    cur_album: str | None
    recommendation: Recommendation


def lookup_items(
    items: list[Item], search: ImportSearch | None
) -> tuple[str | None, str | None, list[Any], BeetsRec]:
    """One session-less lookup, two modes.

    With ``search``: the relookup dialect (release id pin / name search).
    With ``None``: beets' DEFAULT first-scan lookup — plain ``tag_album(source)``
    with no search terms, VA logic intact. The identity is beets' own
    ``Source`` of the items (its ``artist`` and ``name``). Returns
    ``(cur_artist, cur_album, candidates, beets recommendation)``.
    """
    source = Source.from_items(items)
    if search is not None:
        candidates, rec = relookup_source(source, search)
    else:
        proposal = tag_album(source)
        candidates, rec = list(proposal.candidates), proposal.recommendation
    return _opt_str(source.artist), _opt_str(source.name), candidates, rec


def _map_result(
    items: list[Item],
    candidates: list[Any],
    rec: BeetsRec,
    cur_artist: str | None,
    cur_album: str | None,
) -> ResearchResult:
    """Map a non-empty lookup to the review payload (shared by search + rescan)."""
    art_source = os.fsdecode(items[0].path) if items[0].path else None
    has_current_art = art_source is not None and embedded_art(art_source) is not None
    top = candidates[0]
    recommendation = _REC_MAP.get(rec, Recommendation.none)
    candidate = map_album_match(
        top,
        cur_artist=cur_artist,
        cur_album=cur_album,
        options=map_candidate_options(candidates),
        recommendation=recommendation,
        has_current_art=has_current_art,
    )
    return ResearchResult(
        candidate=candidate,
        artist=_opt_str(top.info.artist),
        album=_opt_str(top.info.album),
        recommendation=recommendation,
        confidence=_confidence(top.distance),
    )


def research_folder(folder: str, search: ImportSearch) -> ResearchResult | None:
    """Re-look-up ``folder`` against ``search``; None = nothing usable found.

    None covers both "no readable audio in the folder" and "the lookup
    returned no candidates" — the caller reports either as found=False.
    """
    items = _read_items(Path(folder))
    if not items:
        return None
    cur_artist, cur_album, candidates, rec = lookup_items(items, search)
    if not candidates:
        return None
    return _map_result(items, candidates, rec, cur_artist, cur_album)


def rescan_folder(folder: str) -> RescanOutcome:
    """Re-read ``folder`` and re-match it with beets' default lookup.

    The explicit "I changed the folder on purpose" gesture — the caller
    refreshes the row's fingerprint with the result. Raises
    :class:`NoAudioFilesError` when no readable audio remains.
    """
    items = _read_items(Path(folder))
    if not items:
        raise NoAudioFilesError(folder)
    cur_artist, cur_album, candidates, rec = lookup_items(items, None)
    recommendation = _REC_MAP.get(rec, Recommendation.none)
    if not candidates:
        return RescanOutcome(
            result=None, cur_artist=cur_artist, cur_album=cur_album, recommendation=recommendation
        )
    return RescanOutcome(
        result=_map_result(items, candidates, rec, cur_artist, cur_album),
        cur_artist=cur_artist,
        cur_album=cur_album,
        recommendation=recommendation,
    )
