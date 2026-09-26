"""The Keep downloads switch and a library-job claim must never both proceed.

The switch writes config.yaml and then reloads beets the way Apply does, so a
job claimed between the write and the reload runs while beets' config is
replaced under it (``test_config_apply_claim_race.py`` says why that drops the
overlay a running import forces). Its job gate has to be atomic with holding
the swap lock, as Apply's is; these drive the same two windows Apply's do.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app import library_busy
from app.beets import config_editor
from app.beets.config_editor import JOB_RUNNING
from app.lyrics_jobs.registry import get_lyrics_backfill, lyrics_backfill_active
from app.models.config_api import SetImportOperation
from tests.test_config_apply_claim_race import _request

_REQ = SetImportOperation(keep_downloads=True, base_sha256="0" * 64)


class _EnteredWrite(Exception):
    """Raised by the stubbed write inside the swap lock, to stop the switch there."""


def test_a_claim_in_the_swap_lock_handoff_is_seen_by_the_waiting_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written: list[bool] = []

    def _stub(*_args: object, **_kwargs: object) -> None:
        written.append(library_busy.library_job_active())
        raise _EnteredWrite

    monkeypatch.setattr(config_editor, "_write_file_operation", _stub)

    async def scenario() -> tuple[bool, BaseException | None]:
        lock = asyncio.Lock()
        monkeypatch.setattr(library_busy, "_SWAP_LOCK", lock)
        await lock.acquire()  # an Apply (or another flip) holds the lock
        task = asyncio.create_task(config_editor.set_file_operation(_request(lock), _REQ))
        await asyncio.sleep(0)  # a yield, not a delay: the task runs until it waits on the lock
        assert not task.done()

        lock.release()
        assert not lock.locked()  # the handoff gap: the waiter has not resumed yet
        claimed = True
        try:
            get_lyrics_backfill().start(writes_enabled=False)
        except RuntimeError:
            claimed = False

        try:
            await task
        except (HTTPException, _EnteredWrite) as exc:
            return claimed, exc
        return claimed, None

    claimed, outcome = asyncio.run(scenario())

    assert claimed
    assert lyrics_backfill_active()
    # The switch must not write config.yaml while the job's slot is set.
    assert written == []
    assert isinstance(outcome, HTTPException)
    assert outcome.status_code == 409
    assert outcome.detail == JOB_RUNNING


def test_a_claim_made_while_the_switch_writes_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Between the write and the reload is inside the lock: no job gets in there."""
    refused: list[bool] = []

    def _stub(*_args: object, **_kwargs: object) -> None:
        try:
            get_lyrics_backfill().start(writes_enabled=False)
        except RuntimeError:
            refused.append(True)
        raise _EnteredWrite

    monkeypatch.setattr(config_editor, "_write_file_operation", _stub)

    async def scenario() -> bool:
        lock = asyncio.Lock()
        monkeypatch.setattr(library_busy, "_SWAP_LOCK", lock)
        request = _request(lock)
        with pytest.raises(_EnteredWrite):
            await config_editor.set_file_operation(request, _REQ)
        return lock.locked()

    still_locked = asyncio.run(scenario())

    assert refused == [True]
    assert not still_locked
    assert not lyrics_backfill_active()
