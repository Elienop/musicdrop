"""Re-look-up a parked album against a user-supplied release id or name search.

Backs the interactive import review's "search for a different release" action.
ALL beets autotag access — including the private match helpers — is contained
here, behind the adapter boundary (CLAUDE.md rule 3).

A release id is the reliable escape from beets' Various-Artists filter:
``tag_album(items, search_ids=[id])`` fetches that release directly and never
computes ``va_likely`` (autotag/match.py:274). A name search with
``force_non_va`` calls ``metadata_plugins.candidates(..., va_likely=False)``
directly + beets' own ``_add_candidate``/``_sort_candidates``/``_recommendation``
(mirroring tag_album's text tail) — public ``tag_album`` would recompute
``va_likely`` from the files and re-apply the VA filter (match.py:308).

beets imports are allowed here (inside app/beets/, CLAUDE.md rule 3).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from beets import metadata_plugins
from beets.autotag.match import (
    Recommendation,
    _add_candidate,
    _recommendation,
    _sort_candidates,
    tag_album,
)

if TYPE_CHECKING:
    from app.models.import_models import ImportSearch


def relookup_items(items: list[Any], search: ImportSearch) -> tuple[list[Any], Recommendation]:
    """Return ``(candidates, recommendation)`` for a user search over ``items``.

    ``search.release_id`` wins when present (va_likely-proof). Otherwise a name
    search: ``force_non_va`` pins ``va_likely=False``; else beets' default path.
    An unresolved id or a search with no hits yields an empty list — beets
    swallows lookup errors under the default ``raise_on_error: no``
    (``metadata_plugins._call_plugin_method``), so callers treat empty as
    "no match found", never an exception.

    Runs on the import worker thread (the same thread beets' own lookup stage
    uses). No ``music_dir_context`` binding is needed: ``tag_album``/``candidates``
    hit the metadata sources + in-memory items, never the library DB or item
    file paths.
    """
    if search.release_id and search.release_id.strip():
        _, _, proposal = tag_album(items, search_ids=[search.release_id.strip()])
        return list(proposal.candidates), proposal.recommendation
    artist = (search.artist or "").strip()
    album = (search.album or "").strip()
    if search.force_non_va:
        results: dict[Any, Any] = {}
        for info in metadata_plugins.candidates(items, artist, album, False):
            _add_candidate(items, results, info)
        ranked = list(_sort_candidates(results.values()))
        return ranked, _recommendation(ranked)
    _, _, proposal = tag_album(items, artist, album)
    return list(proposal.candidates), proposal.recommendation


def relookup(task: Any, search: ImportSearch) -> tuple[list[Any], Recommendation]:
    """`relookup_items` over a live import task's in-memory items."""
    return relookup_items(list(task.items or []), search)
