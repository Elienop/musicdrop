"""Offline re-lookup for banked folders — the bank's "search for a different release".

Reads a banked folder's audio files into DETACHED beets Items (``Item.from_path``,
no library DB — the trash_manage pattern), runs the same lookup the attended
import's search uses (:func:`relookup_items`), and maps the winner through the
same mappers the sweep uses, so the payload is exactly what the review screens
render. Preview-only: no file moves, no library writes, no beets config
mutation (``search_ids`` is a ``tag_album`` argument, never set on config).

Thread-safety: may run on an API threadpool thread while a live import's own
lookups are in flight. beets 2.12's HTTP layer makes that safe by construction:
``TimeoutAndRetrySession`` is a singleton ``requests.Session`` whose
``RateLimitAdapter`` paces every request under its own ``threading.Lock``
(beetsplug/_utils/requests.py).

beets imports are allowed here (inside app/beets/, CLAUDE.md rule 3).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from beets.library import Item as Item  # explicit re-export: tests patch res.Item.from_path
from beets.util import get_most_common_tags

from app.beets.import_mapping import (
    _REC_MAP,
    _confidence,
    _opt_str,
    embedded_art,
    map_album_match,
    map_candidate_options,
)
from app.beets.relookup import relookup_items
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


def research_folder(folder: str, search: ImportSearch) -> ResearchResult | None:
    """Re-look-up ``folder`` against ``search``; None = nothing usable found.

    None covers both "no readable audio in the folder" and "the lookup
    returned no candidates" — the caller reports either as found=False.
    """
    items = _read_items(Path(folder))
    if not items:
        return None
    candidates, rec = relookup_items(items, search)
    if not candidates:
        return None
    likelies, _consensus = get_most_common_tags(items)
    cur_artist = _opt_str(likelies["artist"])
    cur_album = _opt_str(likelies["album"])
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
