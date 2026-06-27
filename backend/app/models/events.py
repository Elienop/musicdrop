from typing import Literal

from pydantic import BaseModel


class LibraryChangedEvent(BaseModel):
    """The (only) SSE event today: 'something in the library changed, refetch'."""

    type: Literal["library:changed"] = "library:changed"
