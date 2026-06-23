"""Models for the library-side 'missing tracks' completeness view.

A new module (not reusing import_models.MissingTrack {index, title}) because the
library-side report carries richer per-track fields and a status enum.
"""

from typing import Literal

from pydantic import BaseModel

#: The four outcomes of a missing-tracks lookup. "ok" = classified; the rest are
#: quiet, in-band states the FE renders as unobtrusive notes (not errors).
ReportStatus = Literal["ok", "no_musicbrainz_id", "release_unavailable", "fetch_failed"]


class MissingReleaseTrack(BaseModel):
    index: int  # album-global 1-based position (beets TrackInfo.index)
    disc: int  # disc number (TrackInfo.medium; 1 when single-disc)
    title: str
    duration_seconds: float | None
    mb_trackid: str | None


class AlbumMissingReport(BaseModel):
    status: ReportStatus
    total: int  # len(release tracks); 0 when status != "ok"
    present_count: int  # total - len(missing)
    missing: list[MissingReleaseTrack]
    source: str | None  # the provider tried, e.g. "MusicBrainz"/"Deezer"; populated
    # for "ok" + fetch-failure statuses (release_unavailable/fetch_failed), None for
    # no_musicbrainz_id (no fetch attempted)
