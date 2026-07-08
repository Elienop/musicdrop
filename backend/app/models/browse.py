"""Pydantic contract for the faceted Browse page (genre · decade · format).

Plain data shapes; the beets adapter derives one representative value per album
per facet, so counts sum to the album total and AND/OR filtering is unambiguous.
"""

from pydantic import BaseModel


class FacetValue(BaseModel):
    """One facet option + how many albums carry it (whole-library count)."""

    value: str
    count: int


class BrowseFacets(BaseModel):
    """Available filter values for the Browse page."""

    genres: list[FacetValue]
    decades: list[FacetValue]
    formats: list[FacetValue]
    album_types: list[FacetValue]
    sources: list[FacetValue]
    media: list[FacetValue]
    countries: list[FacetValue]
    lyrics: list[FacetValue]
    tracks: list[FacetValue]
