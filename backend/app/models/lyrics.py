"""Models for the lyrics completeness view (presence + fetch-into-files).

MusicDrop never renders lyrics text — these carry the per-track fetch OUTCOME
and album/library coverage, not the lyrics themselves.
"""

from typing import Literal

from pydantic import BaseModel

#: Per-track fetch outcome. found -> written (if writes on); the rest leave the
#: file untouched. skipped_existing = already had lyrics (skip-existing default);
#: skipped_no_metadata = no usable artist/title to search.
ItemLyricsStatus = Literal[
    "found", "not_found", "fetch_failed", "skipped_existing", "skipped_no_metadata"
]


class ItemLyricsOutcome(BaseModel):
    item_id: int
    status: ItemLyricsStatus
    source: str | None  # backend name (e.g. "lrclib") on found; else None
    written: bool  # try_write() ran (writes enabled AND found)


class AlbumLyricsResult(BaseModel):
    album_id: int
    fetched: int
    not_found: int
    failed: int
    skipped: int
    items: list[ItemLyricsOutcome]
    writes_enabled: bool  # the resolved should_write() at request time
