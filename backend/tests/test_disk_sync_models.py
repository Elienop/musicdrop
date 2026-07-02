"""Model-shape tests for the disk-sync wire contract."""

from app.models.disk_sync import (
    DiskSyncChange,
    DiskSyncOutcome,
    DiskSyncPlan,
    DiskSyncReadError,
    DiskSyncRemoval,
    DiskSyncStatus,
)


def test_plan_shape() -> None:
    plan = DiskSyncPlan(
        total_items=10,
        will_remove=2,
        will_update=1,
        emptied_albums=["blink-182 — blink-182"],
        emptied_total=1,
        removals=[
            DiskSyncRemoval(
                label="blink-182 — I Miss You", path="blink-182/blink-182/03 I Miss You.flac"
            )
        ],
        changes=[DiskSyncChange(label="Cher — Believe", fields=["title", "year"])],
        read_errors=[DiskSyncReadError(label="X — Y", error="boom")],
        truncated=False,
    )
    assert plan.will_remove == 2 and plan.changes[0].fields == ["title", "year"]


def test_status_idle_shape() -> None:
    s = DiskSyncStatus(
        phase="idle",
        job_id=None,
        total=0,
        processed=0,
        removed=0,
        updated=0,
        unchanged=0,
        read_errors=0,
        emptied_albums=0,
        current=None,
        error=None,
        failures=[],
    )
    assert s.phase == "idle" and s.failures == []


def test_outcome_defaults() -> None:
    out = DiskSyncOutcome(status="removed", label="A — B")
    assert out.fields == [] and out.error is None
