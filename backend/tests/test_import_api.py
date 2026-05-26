from app.models.import_api import (
    ImportAlbumStatus,
    ImportAlbumSummary,
    ImportJobState,
    ImportPhase,
    ImportProgress,
    StartImportRequest,
    StartImportResponse,
)
from app.models.import_models import Recommendation


def test_import_phase_values() -> None:
    assert [p.value for p in ImportPhase] == [
        "scanning",
        "reviewing",
        "applying",
        "done",
        "failed",
    ]


def test_album_status_values() -> None:
    assert [s.value for s in ImportAlbumStatus] == [
        "needs_review",
        "decided",
        "applied",
        "skipped",
    ]


def test_start_request_defaults_options_to_none() -> None:
    req = StartImportRequest(path="/music/incoming")
    assert req.path == "/music/incoming"
    assert req.options is None


def test_start_response_carries_job_id() -> None:
    resp = StartImportResponse(job_id="abc123")
    assert resp.job_id == "abc123"


def test_job_state_round_trips() -> None:
    state = ImportJobState(
        job_id="j1",
        phase=ImportPhase.reviewing,
        progress=ImportProgress(applied=1, needs_review=1),
        albums=[
            ImportAlbumSummary(
                index=0,
                folder="/music/incoming/Radiohead - OK Computer",
                artist="Radiohead",
                album="OK Computer",
                recommendation=Recommendation.strong,
                confidence=99.0,
                status=ImportAlbumStatus.applied,
            ),
            ImportAlbumSummary(
                index=1,
                folder="/music/incoming/Unknown",
                artist="Radiohead",
                album="OK Computer",
                recommendation=Recommendation.medium,
                confidence=75.5,
                status=ImportAlbumStatus.needs_review,
            ),
        ],
        summary=None,
        error=None,
    )
    dumped = state.model_dump(mode="json")
    assert dumped["phase"] == "reviewing"
    assert dumped["progress"] == {"applied": 1, "needs_review": 1}
    assert dumped["albums"][0]["status"] == "applied"
    assert dumped["albums"][1]["status"] == "needs_review"
    assert dumped["summary"] is None
    assert dumped["error"] is None
