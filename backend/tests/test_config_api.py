"""Tests for the read-only Config view endpoint (``GET /api/config``).

The router reads ``request.app.state.beets_library`` directly (no FastAPI
dependency to override), so the ``client`` fixture in ``conftest.py`` wires
``app.state.beets_library`` to a real ``setup_beets()`` handle before yielding
the TestClient. Using a real handle (not ``make_test_handle``) is required
because ``build_config_snapshot`` calls ``handle.config_path.stat()`` — the
sentinel placeholder would raise.
"""

import os
import time
from pathlib import Path

import beets
import pytest
import yaml
from fastapi.testclient import TestClient


def test_get_config_returns_snapshot(client: TestClient) -> None:
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    for key in (
        "yaml_text",
        "config_path",
        "loaded_at",
        "file_modified_at",
        "sha256",
        "apply_pending",
    ):
        assert key in body
    # mtime_ns is intentionally NOT in the snapshot: JSON numbers can't carry
    # a CPython st_mtime_ns losslessly through JavaScript (exceeds
    # Number.MAX_SAFE_INTEGER), and the field had no purpose other than CAS.
    assert "mtime_ns" not in body
    assert isinstance(body["yaml_text"], str)
    assert body["yaml_text"]  # non-empty
    assert body["apply_pending"] is False
    assert isinstance(body["sha256"], str)
    assert len(body["sha256"]) == 64


def test_get_config_reflects_mtime_change(
    client: TestClient, beets_library_config_path: Path
) -> None:
    """Second GET after a touch should show apply_pending + new mtime,
    but yaml_text should be unchanged (we serve the in-memory snapshot)."""
    first = client.get("/api/config").json()
    time.sleep(0.01)  # ensure st_mtime advances by at least the FS granularity
    os.utime(
        beets_library_config_path,
        (time.time() + 5, time.time() + 5),
    )
    second = client.get("/api/config").json()
    assert second["apply_pending"] is True
    assert second["yaml_text"] == first["yaml_text"]
    assert second["file_modified_at"] != first["file_modified_at"]
    # sha256 stays the same because the content is identical (only mtime moved).
    assert second["sha256"] == first["sha256"]


def test_get_config_redacts_numeric_secret_in_list_shaped_plugin_config(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The served body must not carry an unquoted numeric password in cleartext.

    The shape under test is a REAL one, mirrored from ``beetsplug/kodiupdate.py``
    rather than invented: that plugin registers the section ``kodi``
    (``super().__init__("kodi")``, :35) and adds a LIST as its default
    (``self.config.add([{"host": ..., "user": ..., "pwd": ...}])``, :38), then
    sets ``self.config["pwd"].redact = True``. A user's config.yaml therefore
    reads ``kodi:`` followed by a list of instances — grepping it for
    ``kodiupdate:`` finds nothing.

    That list shape is where MusicDrop's regex safety-net is the ONLY defence:
    confuse's ``View.flatten(redact=True)`` raises ``ConfigTypeError`` on a
    non-mapping view, falls back to ``view.get()`` and dumps the subtree with
    every ``.redact`` flag ignored. Verified against the installed
    confuse/beets:

        beets.config["kodi"].flatten(redact=True)
        -> ConfigTypeError: kodi must be a dict, not list
        beets.config.flatten(redact=True)["kodi"]
        -> [{'host': 'kodi.local', 'user': 'kodi', 'pwd': 4815162342}]

    Route-level on purpose: the user-visible claim ("with secrets redacted") is
    made by the pane that renders THIS response body.
    """
    beets.config["kodi"].set(
        [{"host": "kodi.local", "port": 8080, "user": "kodi", "pwd": 4815162342}]
    )
    monkeypatch.setattr(
        beets.config["kodi"]["pwd"], "redact", True
    )  # what KodiUpdate.__init__ does

    r = client.get("/api/config")

    assert r.status_code == 200
    effective = r.json()["effective_yaml"]
    assert "4815162342" not in effective
    parsed = yaml.safe_load(effective)
    assert parsed["kodi"][0]["pwd"] == "REDACTED"
    # ...and the neighbouring non-secret fields still render.
    assert parsed["kodi"][0]["host"] == "kodi.local"
    assert parsed["kodi"][0]["port"] == 8080


def test_get_config_survives_non_string_yaml_key(client: TestClient) -> None:
    """A non-``str`` mapping key with a ``str`` value must not 500 the endpoint.

    ``substitute: {112: One Twelve}`` is a legitimate config (112 is a band); the
    key guard used to hand that ``int`` to ``re.Pattern.search`` and raise
    ``TypeError``. Settings is the default landing page AND the only in-app
    editor, so the 500 survived restart and locked the user out of the fix.

    The value must be a ``str`` — an int value never reached the regex.
    """
    beets.config["substitute"].set({112: "One Twelve"})

    r = client.get("/api/config")

    assert r.status_code == 200
    parsed = yaml.safe_load(r.json()["effective_yaml"])
    assert parsed["substitute"] == {112: "One Twelve"}
