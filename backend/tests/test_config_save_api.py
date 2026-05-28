"""End-to-end tests for ``POST /api/config/save``.

Covers the full Layer-3 save flow: parse, schema-validate, SHA-256 CAS,
secret preserve, atomic write. The CAS branch is exercised with a
stale ``base_sha256`` — SHA alone is the CAS token (mtime would overflow
JS's ``Number.MAX_SAFE_INTEGER`` and silently corrupt the round-trip).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient


def _cas(client: TestClient) -> str:
    return str(client.get("/api/config").json()["sha256"])


def test_save_happy_path(client: TestClient, beets_library_config_path: Path) -> None:
    sha = _cas(client)
    new_text = beets_library_config_path.read_text() + "\n# trailing comment\n"
    r = client.post(
        "/api/config/save",
        json={
            "yaml_text": new_text,
            "base_sha256": sha,
        },
    )
    assert r.status_code == 200
    assert r.json()["apply_pending"] is True
    assert "# trailing comment" in beets_library_config_path.read_text()


def test_save_normalizes_yes_no_to_true_false(
    client: TestClient, beets_library_config_path: Path
) -> None:
    # Starter has ``autotag: yes`` — submitting unchanged should still cause
    # ruamel to emit ``true``/``false`` per the maintainer's invariant.
    sha = _cas(client)
    r = client.post(
        "/api/config/save",
        json={
            "yaml_text": beets_library_config_path.read_text(),
            "base_sha256": sha,
        },
    )
    assert r.status_code == 200
    text = beets_library_config_path.read_text()
    assert "autotag: true" in text
    assert "autotag: yes" not in text


def test_save_422_on_invalid_yaml(client: TestClient) -> None:
    sha = _cas(client)
    r = client.post(
        "/api/config/save",
        json={
            "yaml_text": "not: valid: yaml: :",
            "base_sha256": sha,
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ""


def test_save_422_on_schema_error(client: TestClient) -> None:
    sha = _cas(client)
    text = "directory: /tmp\nlibrary: /tmp/x\nimport:\n  copy: maybe\n"
    r = client.post(
        "/api/config/save",
        json={
            "yaml_text": text,
            "base_sha256": sha,
        },
    )
    assert r.status_code == 422
    assert any(d["loc"] == "import.copy" for d in r.json()["detail"])


def test_save_409_on_sha_change(client: TestClient, beets_library_config_path: Path) -> None:
    """An on-disk content change between snapshot and save triggers 409 with
    the fresh on-disk YAML so the merge view can render the diff."""
    sha = _cas(client)
    original = beets_library_config_path.read_text()
    # Modify content out-of-band — the user's editor doesn't see this yet.
    beets_library_config_path.write_text(original + "\n# external edit\n")
    r = client.post(
        "/api/config/save",
        json={
            "yaml_text": original,
            "base_sha256": sha,
        },
    )
    assert r.status_code == 409
    body = r.json()["detail"]
    assert "current_yaml_text" in body
    assert "current_sha256" in body
    assert "# external edit" in body["current_yaml_text"]


def test_save_409_carries_fresh_cas_token(
    client: TestClient, beets_library_config_path: Path
) -> None:
    """The 409's ``current_sha256`` must match the new on-disk bytes so the
    Overwrite-anyway path can re-save without a second snapshot fetch.

    Uses valid YAML to make sure CAS (step 3) is what fires, not the
    schema-validate step (step 2)."""
    original = beets_library_config_path.read_text()
    sha = _cas(client)
    new_bytes = (original + "\n# external\n").encode()
    beets_library_config_path.write_bytes(new_bytes)
    r = client.post(
        "/api/config/save",
        json={"yaml_text": original, "base_sha256": sha},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["current_sha256"] == hashlib.sha256(new_bytes).hexdigest()


def test_save_preserves_secret_when_unchanged(
    client: TestClient, beets_library_config_path: Path
) -> None:
    # Set a fake secret on disk, then reload ``beets.config`` so the in-memory
    # snapshot (which the GET endpoint renders) actually contains the new
    # ``spotify`` block — the user's editor view must show ``REDACTED`` at the
    # secret path before the "submit unchanged" round-trip can exercise the
    # preserve branch. In production this state arrives via the Apply endpoint
    # (Task 8); here we simulate it inline.
    import beets

    text = beets_library_config_path.read_text() + "\nspotify:\n  client_secret: REAL_SECRET_123\n"
    beets_library_config_path.write_text(text)
    beets.config.reload()  # pick up the new on-disk section into beets.config
    sha = _cas(client)
    snap = client.get("/api/config").json()
    # The displayed yaml_text has REDACTED at spotify.client_secret (the
    # SECRET_KEY_PATTERN safety-net in config_snapshot masks ``client_secret``
    # by name, even when the plugin isn't loaded).
    assert "REAL_SECRET_123" not in snap["yaml_text"]
    assert "client_secret: REDACTED" in snap["yaml_text"]
    # Submit unchanged.
    r = client.post(
        "/api/config/save",
        json={
            "yaml_text": snap["yaml_text"],
            "base_sha256": sha,
        },
    )
    assert r.status_code == 200
    # On disk, the real secret survives.
    assert "REAL_SECRET_123" in beets_library_config_path.read_text()
