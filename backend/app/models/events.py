from typing import Literal

from pydantic import BaseModel


class LibraryChangedEvent(BaseModel):
    """An SSE change event telling open tabs to refetch.

    ``library:changed`` = list/metadata data changed (refetch queries).
    ``art:changed`` = image BYTES changed (cover install, artist-image override);
    the frontend remounts ``<img>`` elements only on this variant so routine
    edits don't flicker the roster.
    """

    type: Literal["library:changed", "art:changed"] = "library:changed"
