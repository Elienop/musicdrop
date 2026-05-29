"""Test-only ImportRunner that drives the real ImportBridge with canned data.

Models the SEQUENTIAL import: on its own daemon thread it first emits the canned
``applied`` outcomes (auto-applied strong albums), then parks each canned
``ParkedAlbum`` ONE AT A TIME via ``bridge.park`` (which blocks until a choice
arrives, exactly like the real worker's choose_match), emitting a needs_review
outcome before each park, then calls ``on_finish``. With ``fail_with`` set it
reports an error immediately. Shared by the registry + API tests. No beets, no
network.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from app.beets.import_session import ImportBridge
from app.models.import_models import (
    AlbumOutcome,
    AlbumOutcomeStatus,
    DuplicatePrompt,
    ParkedAlbum,
    Recommendation,
)


class FakeImportRunner:
    """A canned ImportRunner for hermetic lifecycle/API tests."""

    def __init__(
        self,
        parked: list[ParkedAlbum] | None = None,
        applied: list[AlbumOutcome] | None = None,
        fail_with: str | None = None,
        art_sources: dict[int, str] | None = None,
        duplicates: list[DuplicatePrompt] | None = None,
    ) -> None:
        self._parked = parked or []
        self._applied = applied or []
        self._fail_with = fail_with
        # Per-album current-files art source path, recorded on the real bridge at
        # park (mirrors the worker's choose_match). Keyed by album_index.
        self._art_sources = art_sources or {}
        self._duplicates = duplicates or []

    def run(
        self,
        path: str,
        bridge: ImportBridge,
        on_finish: Callable[[], None],
        on_error: Callable[[str], None],
    ) -> None:
        def target() -> None:
            if self._fail_with is not None:
                on_error(self._fail_with)
                return
            try:
                # Strong albums auto-apply first (no parking) — emit their feed
                # outcomes, mirroring the real worker's note_outcome.
                for outcome in self._applied:
                    bridge.note_outcome(outcome)
                # Then each uncertain album, ONE AT A TIME: emit needs_review,
                # then park() which BLOCKS until the consumer pushes a choice.
                for album in self._parked:
                    bridge.note_outcome(
                        AlbumOutcome(
                            album_index=album.album_index,
                            folder=album.folder,
                            artist=album.candidate.album_after.artist,
                            album=album.candidate.album_after.album,
                            recommendation=album.candidate.recommendation,
                            confidence=album.candidate.confidence,
                            status=AlbumOutcomeStatus.needs_review,
                        )
                    )
                    bridge.park(album, art_source=self._art_sources.get(album.album_index))
                # Then each canned duplicate prompt, ONE AT A TIME: emit the
                # needs_dup_resolution outcome (reusing the prompt's index), then
                # park_duplicate which BLOCKS until the consumer pushes a decision.
                for prompt in self._duplicates:
                    bridge.note_outcome(
                        AlbumOutcome(
                            album_index=prompt.album_index,
                            folder=prompt.incoming.folder,
                            artist=prompt.incoming.album_artist,
                            album=prompt.incoming.album,
                            recommendation=Recommendation.strong,
                            confidence=0.0,
                            status=AlbumOutcomeStatus.needs_dup_resolution,
                        )
                    )
                    bridge.park_duplicate(
                        prompt, art_source=self._art_sources.get(prompt.album_index)
                    )
            # Broad by design: mirror the real worker's guard so a canned-data
            # bug surfaces as a failed job rather than a silent dead thread.
            except Exception as exc:
                on_error(str(exc) or exc.__class__.__name__)
                return
            on_finish()

        threading.Thread(target=target, name="fake-import", daemon=True).start()
