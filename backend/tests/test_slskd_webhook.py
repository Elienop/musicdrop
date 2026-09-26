"""The hardened slskd completion webhook.

Driven with ``with TestClient(app) as client:`` so the lifespan wires
``app.state.inbox_dir`` + ``app.state.acquisition_queue``. Each test swaps in a
PROBE queue (a real ``AcquisitionQueue`` that is never started) so ``enqueue``
runs its real dedupe/containment but no drain thread imports anything — making
``status().queued`` assertions deterministic. ``401`` is the ONLY non-2xx the
webhook returns; dup/unmappable/ignored are all ``200``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
    # Default empty downloads_prefix + localDirectoryName "/": used as it is, so
    # "/" itself, which is no folder inside the inbox; refused 200/ignored.
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


def test_an_over_long_folder_name_is_refused_before_the_remap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``localDirectoryName`` stops at 4096 characters (PATH_MAX).

    Unbounded, a 128 KB name held the event loop ~7 s in the remap's
    ``relative_to``. The secret still decides first: a stranger's over-long name
    gets the 401, never the 422 that names the field. The name AT the cap is the
    control that the bound is not tighter than every path slskd can report.
    """
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    app.dependency_overrides.clear()
    at_cap = "/downloads/" + "a" * (4096 - len("/downloads/"))
    event = {"type": "DownloadDirectoryComplete", "localDirectoryName": at_cap + "a"}
    with TestClient(app) as client:
        _configure(client, base_url="http://slskd:5030", token="t", webhook_secret="hook")
        stranger = client.post("/api/slskd/webhook", json=event, headers={"X-API-Key": "nope"})
        over = client.post("/api/slskd/webhook", json=event, headers={"X-API-Key": "hook"})
        event["localDirectoryName"] = at_cap
        fits = client.post("/api/slskd/webhook", json=event, headers={"X-API-Key": "hook"})
    assert stranger.status_code == 401
    assert stranger.json() == {"detail": "invalid webhook token"}
    assert over.status_code == 422
    [error] = over.json()["detail"]
    assert (error["type"], error["loc"]) == ("string_too_long", ["body", "localDirectoryName"])
    assert fits.status_code == 200


# ----- Path in slskd (the stored ``downloads_prefix``) and the miss flag -----

_PATH_IN_SLSKD = "/app/downloads"


@contextmanager
def _slskd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, path_in_slskd: str
) -> Iterator[tuple[TestClient, AcquisitionQueue, Path]]:
    """A booted app with slskd set up, a probe queue, and the inbox's real path.

    The bank is pinned to this test's tmp dir as well as ``beets_dir``, so the
    lifespan's bank reconcile can never open the dev checkout's own bank.
    """
    _write_config(tmp_path)
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path / "beets"))
    monkeypatch.setattr(settings, "bank_dir", str(tmp_path / "bank"))
    inbox = (tmp_path / "beets").resolve() / "inbox"
    inbox.mkdir()
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        probe = _probe_queue(tmp_path)
        monkeypatch.setattr(app.state, "acquisition_queue", probe)
        _configure(
            client,
            base_url="http://slskd:5030",
            token="t",
            downloads_prefix=path_in_slskd,
            webhook_secret="hook",
            auto_import=True,
        )
        yield client, probe, inbox


def _deliver(client: TestClient, folder: str, *, secret: str = "hook") -> tuple[int, object]:
    r = client.post(
        "/api/slskd/webhook",
        headers={"X-API-Key": secret},
        json={"type": "DownloadDirectoryComplete", "localDirectoryName": folder},
    )
    return r.status_code, r.json()


def _missed(client: TestClient) -> object:
    return client.get("/api/slskd/settings").json()["last_download_missed"]


def test_a_folder_inside_path_in_slskd_is_queued_under_slskds_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _slskd(tmp_path, monkeypatch, path_in_slskd=_PATH_IN_SLSKD) as (client, probe, inbox):
        (inbox / "Album").mkdir()
        assert _deliver(client, "/app/downloads/Album") == (200, {"status": "queued"})
        assert probe._dedupe == {str(inbox / "Album")}


def test_a_trailing_slash_on_path_in_slskd_matches_the_same(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _slskd(tmp_path, monkeypatch, path_in_slskd="/app/downloads/") as (client, probe, inbox):
        (inbox / "Album").mkdir()
        assert _deliver(client, "/app/downloads/Album") == (200, {"status": "queued"})
        assert probe._dedupe == {str(inbox / "Album")}


@pytest.mark.parametrize(
    ("path_in_slskd", "reported", "old_reroot"),
    [
        # Matches only as text: the old ``removeprefix`` re-rooted it to <inbox>/2/Album.
        (_PATH_IN_SLSKD, "/app/downloads2/Album", "2/Album"),
        # Outside it entirely: the old code re-rooted it to <inbox>/other/Album.
        (_PATH_IN_SLSKD, "/other/Album", "other/Album"),
        # slskd's whole folder, which maps to the inbox root.
        (_PATH_IN_SLSKD, "/app/downloads", None),
        (_PATH_IN_SLSKD, "/app/downloads/../../etc", None),
        # A relative saved value never matches slskd's absolute path.
        ("app/downloads", "/app/downloads/Album", "app/downloads/Album"),
    ],
)
def test_a_folder_that_does_not_map_is_refused_not_rerooted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path_in_slskd: str,
    reported: str,
    old_reroot: str | None,
) -> None:
    with _slskd(tmp_path, monkeypatch, path_in_slskd=path_in_slskd) as (client, probe, inbox):
        if old_reroot is not None:
            # A real folder where the old re-root pointed, so a regression queues it.
            (inbox / old_reroot).mkdir(parents=True)
        assert _deliver(client, reported) == (200, {"status": "ignored"})
        assert probe._queue.qsize() == 0
        assert probe._dedupe == set()
        assert _missed(client) is True


def test_a_symlink_inside_slskds_folder_that_points_out_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside" / "Album"
    outside.mkdir(parents=True)
    with _slskd(tmp_path, monkeypatch, path_in_slskd=_PATH_IN_SLSKD) as (client, probe, inbox):
        (inbox / "Link").symlink_to(outside, target_is_directory=True)
        assert _deliver(client, "/app/downloads/Link") == (200, {"status": "ignored"})
        assert probe._queue.qsize() == 0
        assert _missed(client) is True


def test_a_symlink_loop_inside_slskds_folder_is_refused_not_a_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _slskd(tmp_path, monkeypatch, path_in_slskd=_PATH_IN_SLSKD) as (client, probe, inbox):
        (inbox / "a").symlink_to(inbox / "b")
        (inbox / "b").symlink_to(inbox / "a")
        assert _deliver(client, "/app/downloads/a") == (200, {"status": "ignored"})
        assert probe._queue.qsize() == 0
        assert _missed(client) is True


def test_an_empty_path_in_slskd_uses_the_reported_folder_as_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _slskd(tmp_path, monkeypatch, path_in_slskd="") as (client, probe, inbox):
        (inbox / "Album").mkdir()
        assert _deliver(client, str(inbox / "Album")) == (200, {"status": "queued"})
        assert probe._dedupe == {str(inbox / "Album")}


def test_an_empty_path_in_slskd_refuses_a_folder_elsewhere_not_rerooted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _slskd(tmp_path, monkeypatch, path_in_slskd="") as (client, probe, inbox):
        # Where the old ``lstrip("/")`` re-root pointed.
        (inbox / "app" / "downloads" / "Album").mkdir(parents=True)
        assert _deliver(client, "/app/downloads/Album") == (200, {"status": "ignored"})
        assert probe._queue.qsize() == 0
        assert _missed(client) is True


def test_a_miss_logs_one_line_naming_both_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A newline in the reported name: ``%r`` keeps it on one line, ``%s`` would
    # forge a second log record.
    reported = "/app/downloads2/Al\nbum"
    with _slskd(tmp_path, monkeypatch, path_in_slskd=_PATH_IN_SLSKD) as (client, _probe, _inbox):
        caplog.set_level(logging.WARNING, logger="app.api.slskd")
        assert _deliver(client, reported) == (200, {"status": "ignored"})
    lines = [r.getMessage() for r in caplog.records if r.name == "app.api.slskd"]
    assert len(lines) == 1, lines
    assert repr(reported) in lines[0]
    assert repr(_PATH_IN_SLSKD) in lines[0]
    assert "\n" not in lines[0]


def test_a_repeated_delivery_enqueues_once_and_not_again_after_the_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim slskd's ``retry: attempts`` rests on: a re-send is harmless.

    While the folder is queued the dedupe set drops the repeat; once the drain
    has recorded it in the ledger (folder unchanged) the ledger drops it.
    """
    with _slskd(tmp_path, monkeypatch, path_in_slskd=_PATH_IN_SLSKD) as (client, probe, inbox):
        (inbox / "Album").mkdir()
        assert _deliver(client, "/app/downloads/Album") == (200, {"status": "queued"})
        assert _deliver(client, "/app/downloads/Album") == (200, {"status": "queued"})
        assert probe._queue.qsize() == 1
        # What the drain does once the import finished (``_process_one``'s tail).
        folder = probe._queue.get_nowait()
        assert folder is not None  # ``None`` is the queue's stop sentinel
        probe._ledger.mark(folder, outcome="imported")
        probe._finish(str(folder.resolve()), "imported", None)
        assert probe._dedupe == set()
        assert _deliver(client, "/app/downloads/Album") == (200, {"status": "queued"})
        assert probe._queue.qsize() == 0
        assert probe._dedupe == set()


def test_the_miss_flag_does_not_outlive_its_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reader falls back to False without a lifespan, so a True left on the
    # shared ``app.state`` would show the next lifespan-less client a miss.
    with _slskd(tmp_path, monkeypatch, path_in_slskd=_PATH_IN_SLSKD) as (client, _probe, _inbox):
        assert _deliver(client, "/other/Album")[1] == {"status": "ignored"}
        assert _missed(client) is True
    assert _missed(TestClient(app)) is False


def test_the_miss_flag_follows_only_messages_that_reach_the_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _slskd(tmp_path, monkeypatch, path_in_slskd=_PATH_IN_SLSKD) as (client, _probe, inbox):
        (inbox / "Album").mkdir()
        assert _missed(client) is False  # boot

        assert _deliver(client, "/other/Album")[1] == {"status": "ignored"}
        assert _missed(client) is True
        # Another event type, a wrong secret and auto-import off never reach the
        # mapping: they leave it.
        file_done = {"type": "DownloadFileComplete", "localDirectoryName": "/app/downloads/Album"}
        other = client.post("/api/slskd/webhook", headers={"X-API-Key": "hook"}, json=file_done)
        assert other.json() == {"status": "ignored"}
        assert _missed(client) is True
        assert _deliver(client, "/app/downloads/Album", secret="nope")[0] == 401
        assert _missed(client) is True
        # A save answers the flag too, so the panel does not lose the line on save.
        put = client.put("/api/slskd/settings", json={"auto_import": False})
        assert put.json()["last_download_missed"] is True
        assert _deliver(client, "/app/downloads/Album")[1] == {"status": "ignored"}
        assert _missed(client) is True
        _configure(client, auto_import=True)

        assert _deliver(client, "/app/downloads/Album")[1] == {"status": "queued"}
        assert _missed(client) is False
        assert _deliver(client, "/other/Album", secret="nope")[0] == 401
        assert _missed(client) is False
        _configure(client, auto_import=False)
        assert _deliver(client, "/other/Album")[1] == {"status": "ignored"}
        assert _missed(client) is False
