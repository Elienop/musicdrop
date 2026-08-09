"""Contract tests for the artist-image source list and reset-outcome models."""

from typing import get_args

import httpx
import pytest
from pydantic import ValidationError

from app.artwork.factory import DEEZER, FANARTTV, SPOTIFY, build_artist_image_sources
from app.config import Settings
from app.models.artist import (
    ArtistImageResetResult,
    ArtistImageSourceId,
    ArtistImageSourceList,
    ArtistImageSourceOption,
)


def test_source_ids_are_the_factory_ids_in_chain_order() -> None:
    # The Literal IS the wire contract; the factory ids are what the runtime
    # looks up. A drift between them would 422 a source the UI just offered.
    # Compared as a TUPLE, not a set: the sources endpoint is ordered and the
    # UI renders in that order, so a reshuffle here is a visible change.
    assert get_args(ArtistImageSourceId) == (FANARTTV, SPOTIFY, DEEZER)


def test_source_ids_match_what_the_factory_actually_builds() -> None:
    # app/artwork/ deliberately does not import app/models/, so nothing
    # type-checks the pair - only this test does. Every credential is set so
    # the factory omits nothing; a source added, dropped or reordered there
    # without the Literal following breaks here.
    sources = build_artist_image_sources(
        httpx.AsyncClient(),
        Settings(
            artist_image_fanarttv_api_key="K",
            artist_image_spotify_client_id="ID",
            artist_image_spotify_client_secret="SEC",
        ),
    )
    assert sources.ids() == get_args(ArtistImageSourceId)


def test_source_option_requires_every_field() -> None:
    option = ArtistImageSourceOption(id="deezer", label="Deezer", available=True, reason=None)
    assert option.model_dump() == {
        "id": "deezer",
        "label": "Deezer",
        "available": True,
        "reason": None,
    }
    with pytest.raises(ValidationError):
        ArtistImageSourceOption(id="deezer", label="Deezer", available=True)  # type: ignore[call-arg]  # proving reason is required


def test_source_option_rejects_an_unknown_id() -> None:
    with pytest.raises(ValidationError):
        ArtistImageSourceOption(
            id="lastfm",  # type: ignore[arg-type]  # proving the Literal rejects it
            label="Last.fm",
            available=True,
            reason=None,
        )


def test_source_list_round_trips() -> None:
    payload = ArtistImageSourceList(
        sources=[
            ArtistImageSourceOption(
                id="fanarttv",
                label="fanart.tv",
                available=False,
                reason="No MusicBrainz ID for this artist",
            )
        ]
    )
    assert payload.model_dump()["sources"][0]["available"] is False


def test_reset_result_reports_both_slots() -> None:
    result = ArtistImageResetResult(ok=True, cleared_override=True, cleared_auto=False)
    assert result.model_dump() == {
        "ok": True,
        "cleared_override": True,
        "cleared_auto": False,
    }
