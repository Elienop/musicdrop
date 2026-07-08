"""Shared building blocks for the single-slot background-job registries."""

from app.jobs.base import JobState, SingleSlotRegistry

__all__ = ["JobState", "SingleSlotRegistry"]
