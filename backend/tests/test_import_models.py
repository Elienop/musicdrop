from app.models.import_models import (
    AlbumChange,
    Candidate,
    CandidateOption,
    ImportAction,
    ImportChoice,
    MissingTrack,
    ParkedAlbum,
    Recommendation,
    TrackChange,
    TrackChangeStatus,
    UnmatchedItem,
)


def test_recommendation_enum_mirrors_beets_levels() -> None:
    assert [r.value for r in Recommendation] == ["none", "low", "medium", "strong"]


def test_candidate_round_trips_through_pydantic() -> None:
    candidate = Candidate(
        confidence=96.0,
        recommendation=Recommendation.strong,
        data_source="MusicBrainz",
        data_url="https://musicbrainz.org/release/a1",
        cover_after_url="https://coverartarchive.org/release/a1/front-500",
        has_current_art=False,
        changed_fields=["album", "label"],
        album_before=AlbumChange(
            artist="Radiohead",
            album="OK Computr",
            year=None,
            label=None,
            country=None,
            media=None,
        ),
        album_after=AlbumChange(
            artist="Radiohead",
            album="OK Computer",
            year=1997,
            label="Parlophone",
            country="GB",
            media="CD",
        ),
        tracks=[
            TrackChange(
                index=1,
                status=TrackChangeStatus.changed,
                title_before="Airbag",
                title_after="Airbag",
                track_before=1,
                track_after=1,
            )
        ],
        missing=[MissingTrack(index=3, title="Subterranean")],
        unmatched=[UnmatchedItem(title="UNKNOWN LOCAL", track=2)],
        options=[
            CandidateOption(
                index=0,
                confidence=96.0,
                data_source="MusicBrainz",
                disambiguation="GB, CD, 1997",
            )
        ],
    )
    dumped = candidate.model_dump()
    assert dumped["confidence"] == 96.0
    assert dumped["recommendation"] == "strong"
    assert dumped["tracks"][0]["status"] == "changed"
    assert dumped["missing"][0]["title"] == "Subterranean"
    assert dumped["unmatched"][0]["track"] == 2
    assert dumped["options"][0]["disambiguation"] == "GB, CD, 1997"


def test_parked_album_wraps_a_candidate() -> None:
    candidate = Candidate(
        confidence=50.0,
        recommendation=Recommendation.medium,
        data_source="MusicBrainz",
        data_url=None,
        cover_after_url=None,
        has_current_art=False,
        changed_fields=[],
        album_before=AlbumChange(
            artist="A", album="B", year=None, label=None, country=None, media=None
        ),
        album_after=AlbumChange(
            artist="A", album="B", year=None, label=None, country=None, media=None
        ),
        tracks=[],
        missing=[],
        unmatched=[],
        options=[],
    )
    parked = ParkedAlbum(album_index=0, folder="/music/B", candidate=candidate)
    assert parked.album_index == 0
    assert parked.folder == "/music/B"
    assert parked.candidate.recommendation is Recommendation.medium


def test_import_choice_actions() -> None:
    assert [a.value for a in ImportAction] == [
        "apply",
        "skip",
        "asis",
        "astracks",
        "abort",
        "search",
    ]
    choice = ImportChoice(action=ImportAction.apply, candidate_index=2)
    assert choice.action is ImportAction.apply
    assert choice.candidate_index == 2
    assert ImportChoice(action=ImportAction.skip).candidate_index is None
    assert ImportChoice(action=ImportAction.abort).action is ImportAction.abort


def test_duplicate_prompt_round_trips() -> None:
    from app.models.import_models import (
        DuplicateAction,
        DuplicateDecision,
        DuplicatePrompt,
        ExistingAlbum,
        IncomingAlbum,
    )

    incoming = IncomingAlbum(
        album_artist="Radiohead",
        album="In Rainbows",
        year=2007,
        track_count=10,
        format="FLAC",
        bitrate_kbps=900,
        folder="/incoming/Radiohead - In Rainbows",
        has_current_art=True,
    )
    existing = ExistingAlbum(
        album_id=42,
        album_artist="Radiohead",
        album="In Rainbows",
        year=2007,
        track_count=9,
        format="MP3",
        bitrate_kbps=320,
        folder="/music/Radiohead/In Rainbows",
    )
    prompt = DuplicatePrompt(album_index=3, incoming=incoming, existing=[existing])
    assert prompt.model_dump()["incoming"]["album"] == "In Rainbows"
    assert prompt.existing[0].album_id == 42

    decision = DuplicateDecision(action=DuplicateAction.replace)
    assert decision.action is DuplicateAction.replace
    # All four faithful actions exist.
    assert {a.value for a in DuplicateAction} == {"skip_new", "keep_both", "replace", "merge"}


def test_needs_dup_resolution_status_exists() -> None:
    from app.models.import_models import AlbumOutcomeStatus

    assert AlbumOutcomeStatus.needs_dup_resolution.value == "needs_dup_resolution"


def test_import_options_defaults_are_todays_behavior() -> None:
    from app.models.import_models import ImportOptions

    o = ImportOptions()
    assert o.operation == "default"
    assert o.unattended is False


def test_import_options_round_trips() -> None:
    from app.models.import_models import ImportOptions

    o = ImportOptions.model_validate({"operation": "move", "unattended": True})
    assert o.model_dump() == {"operation": "move", "unattended": True, "sweep": False}


def test_import_origin_values() -> None:
    from typing import get_args

    from app.models.import_models import ImportOrigin

    assert set(get_args(ImportOrigin)) == {"manual", "inbox", "sweep", "bank_apply"}
