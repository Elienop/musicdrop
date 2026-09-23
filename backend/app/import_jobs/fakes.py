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

from app.beets.import_session import ImportAbortError, ImportBridge
from app.models.bank import BankApplyDirective
from app.models.import_models import (
    AlbumOutcome,
    AlbumOutcomeStatus,
    DuplicatePrompt,
    ImportOptions,
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
        published_duplicates: list[DuplicatePrompt] | None = None,
    ) -> None:
        self._parked = parked or []
        self._applied = applied or []
        self._fail_with = fail_with
        # Per-album current-files art source path, recorded on the real bridge at
        # park (mirrors the worker's choose_match). Keyed by album_index.
        self._art_sources = art_sources or {}
        self._duplicates = duplicates or []
        # Prompts pushed non-blocking (ImportBridge.publish_duplicate) after the
        # parked ones, modelling a refusal that republishes what it saw.
        self._published_duplicates = published_duplicates or []
        # The ImportOptions forwarded by the registry's start(), recorded so the
        # plumbing tests can assert start -> runner.run threading (None = manual).
        self.received_options: ImportOptions | None = None
        # The BankApplyDirective forwarded by the registry's start() (None =
        # not an apply run), recorded for the threading tests.
        self.received_directive: BankApplyDirective | None = None
        # Guard seam: tests set validate_error to make start() refuse like the
        # real runner; validate_calls records the (path, options) it saw.
        self.validate_error: Exception | None = None
        self.validate_calls: list[tuple[list[str], ImportOptions | None]] = []
        # The forgiven-root seam: the real runner returns the empty library root
        # it let through, and the registry logs it once the slot is claimed.
        self.validate_forgiven: str | None = None
        # The paths the registry handed run() — the multi-path contract's seam.
        self.received_paths: list[str] | None = None
        # Spawn-failure seam: tests set run_error to make run() raise
        # SYNCHRONOUSLY (no worker thread, no callback) — modelling the real
        # runner's threading.Thread(...).start() failing under exhaustion.
        self.run_error: Exception | None = None

    def validate(self, paths: list[str], options: ImportOptions | None = None) -> str | None:
        self.validate_calls.append((list(paths), options))
        if self.validate_error is not None:
            raise self.validate_error
        return self.validate_forgiven

    def run(
        self,
        paths: list[str],
        bridge: ImportBridge,
        on_finish: Callable[[], None],
        on_error: Callable[[str], None],
        options: ImportOptions | None = None,
        directive: BankApplyDirective | None = None,
    ) -> None:
        if self.run_error is not None:
            # Fail like the real runner's synchronous session build /
            # Thread.start() — the slot is already claimed, no thread spawns.
            raise self.run_error
        self.received_paths = list(paths)
        self.received_options = options
        self.received_directive = directive

        def target() -> None:
            if self._fail_with is not None:
                on_error(self._fail_with)
                return
            try:
                self._emit_canned(bridge)
            except ImportAbortError:
                # A stop, raised out of a park. beets' own run() catches this and
                # returns normally, so this run ends the same way: on_finish
                # below, phase done — not a failure.
                pass
            # Broad by design: mirror the real worker's guard so a canned-data
            # bug surfaces as a failed job rather than a silent dead thread.
            except Exception as exc:
                on_error(str(exc) or exc.__class__.__name__)
                return
            on_finish()

        threading.Thread(target=target, name="fake-import", daemon=True).start()

    def _emit_canned(self, bridge: ImportBridge) -> None:
        """Push every canned outcome and park, in the real worker's order.

        Runs on the worker thread; the parks BLOCK, so a stop unwinds out of
        here through ``ImportAbortError`` exactly as beets' own run() does.
        """
        # Strong albums auto-apply first (no parking) — emit their feed
        # outcomes, mirroring the real worker's note_outcome.
        for outcome in self._applied:
            bridge.note_outcome(outcome)
        # Then each uncertain album, ONE AT A TIME: emit needs_review, then
        # park() which BLOCKS until the consumer pushes a choice.
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
            bridge.park_duplicate(prompt, art_source=self._art_sources.get(prompt.album_index))
        # Prompts published WITHOUT a park: what a refusing Replace does when
        # the stored decision no longer fits the library, so the row the user
        # re-opens shows the live collision. Nobody answers these, so they must
        # not block or be reported as awaited.
        for prompt in self._published_duplicates:
            bridge.publish_duplicate(prompt)
