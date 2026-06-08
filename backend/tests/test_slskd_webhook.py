"""The hardened slskd completion webhook.

Driven with ``with TestClient(app) as client:`` so the lifespan wires
``app.state.inbox_dir`` + ``app.state.acquisition_queue``. Each test swaps in a
PROBE queue (a real ``AcquisitionQueue`` that is never started) so ``enqueue``
runs its real dedupe/containment but no drain thread imports anything — making
``status().queued`` assertions deterministic. ``401`` is the ONLY non-2xx the
webhook returns; dup/unmappable/ignored are all ``200``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.acquisition.ledger import AcquisitionLedger
from app.acquisition.queue import AcquisitionQueue
from app.config import settings
from app.import_jobs.fakes import FakeImportRunner
from app.import_jobs.registry import ImportJobRegistry
from app.main import app

_VALID = {"type": "DownloadDirectoryComplete", "localDirectoryName": "/downloads/Artist/Album"}


def _write_config(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (tmp_path / "config.yaml").write_text(
        f"directory: {music}\nlibrary: library.db\nplugins:\n  - musicbrainz\n"
    )


def _probe_queue(tmp_path: Path) -> AcquisitionQueue:
    return AcquisitionQueue(
        import_registry=ImportJobRegistry(runner=FakeImportRunner()),
        ledger=AcquisitionLedger(tmp_path / "probe-ledger.json"),
    )


def _configure(client: TestClient, **kwargs: object) -> None:
    client.put("/api/slskd/settings", json=kwargs)


def test_webhook_missing_token_returns_401(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path))
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        _configure(client, base_url="http://slskd:5030", token="t", webhook_secret="hook")
        r = client.post("/api/slskd/webhook", json=_VALID)
    assert r.status_code == 401


def test_webhook_wrong_token_returns_401(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path))
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        _configure(client, base_url="http://slskd:5030", token="t", webhook_secret="hook")
        r = client.post("/api/slskd/webhook", headers={"X-API-Key": "nope"}, json=_VALID)
    assert r.status_code == 401


def test_webhook_queues_valid_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path))
    (tmp_path / "inbox" / "Artist" / "Album").mkdir(parents=True)
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
        expected = tmp_path.resolve() / "inbox" / "Artist" / "Album"
        assert probe.status().queued == 1
        assert str(expected) in probe._dedupe


def test_webhook_ignores_non_directory_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path))
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
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path))
    (tmp_path / "inbox" / "Artist" / "Album").mkdir(parents=True)
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
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path))
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
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path))
    (tmp_path / "inbox").mkdir()
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
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path))
    (tmp_path / "inbox").mkdir()
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
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path))
    (tmp_path / "inbox" / "Beatles" / "Abbey Road").mkdir(parents=True)
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
        expected = tmp_path.resolve() / "inbox" / "Beatles" / "Abbey Road"
        assert str(expected) in probe._dedupe


def test_webhook_duplicate_event_enqueues_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path))
    (tmp_path / "inbox" / "Artist" / "Album").mkdir(parents=True)
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
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path))
    (tmp_path / "inbox" / "Artist" / "Album" / "CD1").mkdir(parents=True)
    (tmp_path / "inbox" / "Artist" / "Album" / "CD2").mkdir(parents=True)
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
        album = tmp_path.resolve() / "inbox" / "Artist" / "Album"
        assert probe.status().queued == 1
        assert str(album) in probe._dedupe
