from typing import Literal

from pydantic import BaseModel


class LibraryChangedEvent(BaseModel):
    """An SSE change event telling open tabs to refetch.

    ``library:changed`` = list/metadata data changed (refetch queries).
    ``art:changed`` = image BYTES changed (cover install, artist-image override);
    the frontend remounts ``<img>`` elements only on this variant so routine
    edits don't flicker the roster.

    ``scope`` names WHICH asset changed — ``"album:123"`` / ``"artist:Radiohead"``
    — so a tab remounts just that image instead of every ``<img>`` on the page
    (a browse grid can hold 192 covers). ``None`` = library-wide: bump
    everything, which is what a multi-artist sweep or a reconnect catch-up needs.
    """

    type: Literal["library:changed", "art:changed"] = "library:changed"
    scope: str | None = None
