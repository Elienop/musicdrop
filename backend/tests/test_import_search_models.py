import pytest
from pydantic import ValidationError

from app.models.import_models import (
    AlbumChange,
    Candidate,
    ImportAction,
    ImportChoice,
    ImportSearch,
    Recommendation,
)


def _blank_change() -> AlbumChange:
    return AlbumChange(artist=None, album=None, year=None, label=None, country=None, media=None)


def test_import_search_accepts_release_id_only() -> None:
    s = ImportSearch(release_id="https://musicbrainz.org/release/abc")
    assert s.release_id == "https://musicbrainz.org/release/abc"
    assert s.force_non_va is True  # the toggle defaults on


def test_import_search_accepts_artist_and_album() -> None:
    s = ImportSearch(artist="2 Brothers on the 4th Floor", album="Dreams", force_non_va=True)
    assert s.artist == "2 Brothers on the 4th Floor"


def test_import_search_requires_id_or_complete_name_pair() -> None:
    with pytest.raises(ValidationError):
        ImportSearch()  # neither id nor a name pair
    with pytest.raises(ValidationError):
        ImportSearch(artist="Only artist")  # album missing
    with pytest.raises(ValidationError):
        ImportSearch(release_id="   ")  # blank id is not a target


def test_import_choice_search_requires_a_payload() -> None:
    with pytest.raises(ValidationError):
        ImportChoice(action=ImportAction.search)  # action=search but no search


def test_import_choice_non_search_rejects_a_payload() -> None:
    search = ImportSearch(release_id="abc")
    with pytest.raises(ValidationError):
        ImportChoice(action=ImportAction.apply, search=search)


def test_import_choice_search_round_trips() -> None:
    c = ImportChoice(action=ImportAction.search, search=ImportSearch(release_id="abc"))
    assert c.search is not None
    assert c.search.release_id == "abc"


def test_rescan_choice_carries_no_payload() -> None:
    choice = ImportChoice(action=ImportAction.rescan)
    assert choice.search is None
    assert choice.candidate_index is None


def test_rescan_choice_rejects_a_search_payload() -> None:
    search = ImportSearch(release_id="x")
    with pytest.raises(ValidationError):
        ImportChoice(action=ImportAction.rescan, search=search)


def test_candidate_search_fields_default() -> None:
    c = Candidate(
        confidence=10.0,
        recommendation=Recommendation.medium,
        data_source="MusicBrainz",
        data_url=None,
        cover_after_url=None,
        has_current_art=False,
        changed_fields=[],
        album_before=_blank_change(),
        album_after=_blank_change(),
        tracks=[],
        missing=[],
        unmatched=[],
        options=[],
    )
    assert c.search_feedback is None
    assert c.search_revision == 0
