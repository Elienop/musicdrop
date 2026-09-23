"""The hardened slskd completion webhook.

Driven with ``with TestClient(app) as client:`` so the lifespan wires
``app.state.inbox_dir`` + ``app.state.acquisition_queue``. Each test swaps in a
PROBE queue (a real ``AcquisitionQueue`` that is never started) so ``enqueue``
runs its real dedupe/containment but no drain thread imports anything — making
``status().queued`` assertions deterministic. ``401`` is the ONLY non-2xx the
webhook returns; dup/unmappable/ignored are all ``200``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.concurrency import run_in_threadpool
from fastapi.testclient import TestClient

from app.acquisition.ledger import AcquisitionLedger
from app.acquisition.queue import AcquisitionQueue
from app.config import settings
from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import ImportJobRegistry
from app.main import app

_VALID = {"type": "DownloadDirectoryComplete", "localDirectoryName": "/downloads/Artist/Album"}


def _write_config(tmp_path: Path) -> Path:
    """Write a hermetic beets config and return the BEETSDIR it lives in.

    The beets dir is a SIBLING of the music dir, never its parent:
    ``app.beets.store_layout`` refuses a beets data directory that contains the
    music library, and the real lifespan these tests boot runs that check.
    """
    music = tmp_path / "music"
    music.mkdir()
    beets = tmp_path / "beets"
    beets.mkdir()
    (beets / "config.yaml").write_text(
        f"directory: {music}\nlibrary: library.db\nplugins:\n  - musicbrainz\n"
    )
    return beets


def _probe_queue(tmp_path: Path) -> AcquisitionQueue:
    return AcquisitionQueue(
        import_registry=ImportJobRegistry(runner=FakeImportRunner()),
        ledger=AcquisitionLedger(tmp_path / "probe-ledger.json"),
    )


def _configure(client: TestClient, **kwargs: object) -> None:
    client.put("/api/slskd/settings", json=kwargs)


def test_webhook_missing_token_returns_401(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        _configure(client, base_url="http://slskd:5030", token="t", webhook_secret="hook")
        r = client.post("/api/slskd/webhook", json=_VALID)
    assert r.status_code == 401


def test_webhook_wrong_token_returns_401(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        _configure(client, base_url="http://slskd:5030", token="t", webhook_secret="hook")
        r = client.post("/api/slskd/webhook", headers={"X-API-Key": "nope"}, json=_VALID)
    assert r.status_code == 401


def test_webhook_queues_valid_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    (tmp_path / "beets" / "inbox" / "Artist" / "Album").mkdir(parents=True)
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        probe = _probe_queue(tmp_path)
        monkeypatch.setattr(app.state, "acquisition_queue", probe)
        _configure(
            client,
            base_url="http://slskd:5030",
            token="t",
            downloads_prefix="/downloads",
            webhook_secret="hook",
            auto_import=True,
        )
        r = client.post("/api/slskd/webhook", headers={"X-API-Key": "hook"}, json=_VALID)
        assert r.status_code == 200
        assert r.json() == {"status": "queued"}
        expected = (tmp_path / "beets").resolve() / "inbox" / "Artist" / "Album"
        assert probe.status().queued == 1
        assert str(expected) in probe._dedupe


def test_webhook_offloads_blocking_fs_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """contain / coalesce_album_root / enqueue must not run on the event loop.

    Spied at ``app.api.acquisition``'s threadpool call rather than this
    module's, because the three hops go through ``inbox_read`` - which proves
    two things at once: they are still offloaded, and they are offloaded UNDER
    ``_INBOX_SCAN_SLOTS``. This route is the only unauthenticated producer of
    inbox filesystem work (the webhook is in the auth gate's exempt set), so N
    concurrent posts against a hung mount used to take N of anyio's 40
    process-wide tokens and starve the scrypt derive behind sign-in - while the
    limiter's own note claimed the acquisition surface cost at most 5 of 40.
    """
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    (tmp_path / "beets" / "inbox" / "Artist" / "Album").mkdir(parents=True)
    app.dependency_overrides.clear()

    import app.api.acquisition as acq_mod
    import app.api.slskd as slskd_mod

    # ``vars`` because mypy's no-implicit-reexport forbids reading the
    # re-imported name off the module object.
    assert vars(slskd_mod)["inbox_read"] is acq_mod.inbox_read  # the capped seam

    offloaded: list[str] = []
    peak = 0

    async def spy(func: Callable[..., object], *args: object, **kwargs: object) -> object:
        nonlocal peak
        # ``partial`` carries no ``__name__``; the wrapped callable does.
        offloaded.append(getattr(getattr(func, "func", func), "__name__", repr(func)))
        peak = max(peak, int(acq_mod._INBOX_SCAN_SLOTS.borrowed_tokens))
        return await run_in_threadpool(func, *args, **kwargs)

    monkeypatch.setattr(acq_mod, "run_in_threadpool", spy)
    with TestClient(app) as client:
        monkeypatch.setattr(app.state, "acquisition_queue", _probe_queue(tmp_path))
        _configure(
            client,
            base_url="http://slskd:5030",
            token="t",
            downloads_prefix="/downloads",
            webhook_secret="hook",
            auto_import=True,
        )
        r = client.post("/api/slskd/webhook", headers={"X-API-Key": "hook"}, json=_VALID)
        assert r.json() == {"status": "queued"}
    assert {"contain", "coalesce_album_root", "enqueue"} <= set(offloaded)
    # A token was held while each ran - a bare ``run_in_threadpool`` holds none.
    assert peak == 1, peak


def test_webhook_ignores_non_directory_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        probe = _probe_queue(tmp_path)
        monkeypatch.setattr(app.state, "acquisition_queue", probe)
        _configure(
            client,
            base_url="http://slskd:5030",
            token="t",
            downloads_prefix="/downloads",
            webhook_secret="hook",
            auto_import=True,
        )
        r = client.post(
            "/api/slskd/webhook",
            headers={"X-API-Key": "hook"},
            json={"type": "DownloadFileComplete", "localDirectoryName": "/downloads/Artist/Album"},
        )
        assert r.status_code == 200
        assert r.json() == {"status": "ignored"}
        assert probe.status().queued == 0


def test_webhook_ignores_when_auto_import_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    (tmp_path / "beets" / "inbox" / "Artist" / "Album").mkdir(parents=True)
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        probe = _probe_queue(tmp_path)
        monkeypatch.setattr(app.state, "acquisition_queue", probe)
        _configure(
            client,
            base_url="http://slskd:5030",
            token="t",
            downloads_prefix="/downloads",
            webhook_secret="hook",
            auto_import=False,
        )
        r = client.post("/api/slskd/webhook", headers={"X-API-Key": "hook"}, json=_VALID)
        assert r.status_code == 200
        assert r.json() == {"status": "ignored"}
        assert probe.status().queued == 0


def test_webhook_ignores_path_escaping_inbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        probe = _probe_queue(tmp_path)
        monkeypatch.setattr(app.state, "acquisition_queue", probe)
        _configure(
            client,
            base_url="http://slskd:5030",
            token="t",
            downloads_prefix="/downloads",
            webhook_secret="hook",
            auto_import=True,
        )
        r = client.post(
            "/api/slskd/webhook",
            headers={"X-API-Key": "hook"},
            json={
                "type": "DownloadDirectoryComplete",
                "localDirectoryName": "/downloads/../../../etc",
            },
        )
        assert r.status_code == 200
        assert r.json() == {"status": "ignored"}
        assert probe.status().queued == 0


def test_webhook_ignores_empty_remainder_inbox_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # localDirectoryName == downloads_prefix -> empty remainder -> the inbox ROOT.
    # A whole-inbox MOVE would sweep in unrelated siblings, so it is refused with a
    # 200/ignored (never queued), not allowed through like a real album drop.
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    (tmp_path / "beets" / "inbox").mkdir()
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        probe = _probe_queue(tmp_path)
        monkeypatch.setattr(app.state, "acquisition_queue", probe)
        _configure(
            client,
            base_url="http://slskd:5030",
            token="t",
            downloads_prefix="/downloads",
            webhook_secret="hook",
            auto_import=True,
        )
        r = client.post(
            "/api/slskd/webhook",
            headers={"X-API-Key": "hook"},
            json={"type": "DownloadDirectoryComplete", "localDirectoryName": "/downloads"},
        )
        assert r.status_code == 200
        assert r.json() == {"status": "ignored"}
        assert probe.status().queued == 0


def test_webhook_ignores_root_slash_under_empty_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Default empty downloads_prefix + localDirectoryName "/" -> lstrip("/") -> ""
    # -> the inbox ROOT again. Same whole-inbox MOVE; refused 200/ignored.
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    (tmp_path / "beets" / "inbox").mkdir()
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        probe = _probe_queue(tmp_path)
        monkeypatch.setattr(app.state, "acquisition_queue", probe)
        _configure(
            client,
            base_url="http://slskd:5030",
            token="t",
            downloads_prefix="",
            webhook_secret="hook",
            auto_import=True,
        )
        r = client.post(
            "/api/slskd/webhook",
            headers={"X-API-Key": "hook"},
            json={"type": "DownloadDirectoryComplete", "localDirectoryName": "/"},
        )
        assert r.status_code == 200
        assert r.json() == {"status": "ignored"}
        assert probe.status().queued == 0


def test_webhook_remaps_prefix_to_inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    (tmp_path / "beets" / "inbox" / "Beatles" / "Abbey Road").mkdir(parents=True)
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        probe = _probe_queue(tmp_path)
        monkeypatch.setattr(app.state, "acquisition_queue", probe)
        _configure(
            client,
            base_url="http://slskd:5030",
            token="t",
            downloads_prefix="/data/downloads",
            webhook_secret="hook",
            auto_import=True,
        )
        r = client.post(
            "/api/slskd/webhook",
            headers={"X-API-Key": "hook"},
            json={
                "type": "DownloadDirectoryComplete",
                "localDirectoryName": "/data/downloads/Beatles/Abbey Road",
            },
        )
        assert r.json() == {"status": "queued"}
        expected = (tmp_path / "beets").resolve() / "inbox" / "Beatles" / "Abbey Road"
        assert str(expected) in probe._dedupe


def test_webhook_duplicate_event_enqueues_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    (tmp_path / "beets" / "inbox" / "Artist" / "Album").mkdir(parents=True)
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        probe = _probe_queue(tmp_path)
        monkeypatch.setattr(app.state, "acquisition_queue", probe)
        _configure(
            client,
            base_url="http://slskd:5030",
            token="t",
            downloads_prefix="/downloads",
            webhook_secret="hook",
            auto_import=True,
        )
        r1 = client.post("/api/slskd/webhook", headers={"X-API-Key": "hook"}, json=_VALID)
        r2 = client.post("/api/slskd/webhook", headers={"X-API-Key": "hook"}, json=_VALID)
        assert r1.json() == {"status": "queued"}
        assert r2.json() == {"status": "queued"}
        assert probe.status().queued == 1
        assert probe._queue.qsize() == 1


def test_webhook_coalesces_multidisc_to_album(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    (tmp_path / "beets" / "inbox" / "Artist" / "Album" / "CD1").mkdir(parents=True)
    (tmp_path / "beets" / "inbox" / "Artist" / "Album" / "CD2").mkdir(parents=True)
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        probe = _probe_queue(tmp_path)
        monkeypatch.setattr(app.state, "acquisition_queue", probe)
        _configure(
            client,
            base_url="http://slskd:5030",
            token="t",
            downloads_prefix="/downloads",
            webhook_secret="hook",
            auto_import=True,
        )
        for disc in ("CD1", "CD2"):
            r = client.post(
                "/api/slskd/webhook",
                headers={"X-API-Key": "hook"},
                json={
                    "type": "DownloadDirectoryComplete",
                    "localDirectoryName": f"/downloads/Artist/Album/{disc}",
                },
            )
            assert r.json() == {"status": "queued"}
        # Both disc events coalesced to the album parent + the dedupe collapsed them.
        album = (tmp_path / "beets").resolve() / "inbox" / "Artist" / "Album"
        assert probe.status().queued == 1
        assert str(album) in probe._dedupe


def test_a_non_ascii_api_key_is_refused_not_a_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``hmac.compare_digest`` on ``str`` raises the moment either side is non-ASCII.

    ``TypeError: comparing strings with non-ASCII characters is not supported``
    — and uvicorn decodes header bytes as latin-1, so any caller could put one
    accented character in ``X-API-Key`` and turn this guard into an unhandled
    500. That matters more here than almost anywhere: this path is EXEMPT from
    the session gate, so an anonymous stranger reaches it, and the bare 500
    Starlette synthesises is built outside all user middleware and therefore
    carries none of the security headers.

    The header is passed as BYTES because httpx refuses to encode a non-ASCII
    ``str`` header at all — which is also why no existing test caught this.
    """
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    app.dependency_overrides.clear()
    with TestClient(app, raise_server_exceptions=False) as client:
        _configure(client, base_url="http://slskd:5030", token="t", webhook_secret="hook")
        r = client.post(
            "/api/slskd/webhook",
            json=_VALID,
            # BYTES, not str: httpx refuses to encode a non-ASCII str header
            # at all, which is also why no existing test ever reached this.
            headers={b"X-API-Key": "kéy".encode()},
        )
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid webhook token"}


def test_a_wrong_shaped_body_cannot_read_the_contract_without_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """401 before 422 — the secret decides before the body is validated.

    An anonymous caller posting nonsense used to get FastAPI's full validation
    detail back, naming every field this webhook expects. On a gate-exempt path
    that is the request contract handed to a stranger, and it contradicts this
    module's own "401 is the only non-2xx" pin. The fix is a route DEPENDENCY:
    FastAPI solves those before it validates the body.
    """
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        _configure(client, base_url="http://slskd:5030", token="t", webhook_secret="hook")
        r = client.post(
            "/api/slskd/webhook",
            json={"not": "the right shape"},
            headers={"X-API-Key": "wrong"},
        )
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid webhook token"}


def test_an_authenticated_caller_still_gets_its_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: validation was REORDERED, not removed.

    Without this, deleting body validation entirely would pass the test above.
    """
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        _configure(client, base_url="http://slskd:5030", token="t", webhook_secret="hook")
        r = client.post(
            "/api/slskd/webhook",
            json={"not": "the right shape"},
            headers={"X-API-Key": "hook"},
        )
    assert r.status_code == 422
