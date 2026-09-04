"""End-to-end tests for ``POST /api/config/save``.

Covers the full Layer-3 save flow: parse, schema-validate, SHA-256 CAS, atomic
write. The editor serves and edits the RAW ``config.yaml``, so Save writes the
submitted document verbatim (no secret-preserve merge) — the comment/secret
regressions the raw-serve fix closed are pinned here too. The CAS branch is
exercised with a stale ``base_sha256`` — SHA alone is the CAS token (mtime would
overflow JS's ``Number.MAX_SAFE_INTEGER`` and silently corrupt the round-trip).
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
    # Not a bare ``/tmp``: the fixture's beets dir sits under it, so that value
    # would also trip the containment row (app/beets/store_layout.py) and this
    # test would pass while asking a different question.
    text = "directory: /tmp/music\nlibrary: /tmp/x\nimport:\n  copy: maybe\n"
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


def test_get_serves_raw_yaml_unredacted(
    client: TestClient, beets_library_config_path: Path
) -> None:
    """The editor is seeded with the RAW file (secrets included), not the
    redacted flatten dump — so a round-trip Save can't destroy them. The
    redacted view lives in ``effective_yaml`` instead."""
    text = beets_library_config_path.read_text() + "\nspotify:\n  client_secret: REAL_SECRET_123\n"
    beets_library_config_path.write_text(text)
    snap = client.get("/api/config").json()
    # Editable doc = the raw file, real secret visible (was the redacted flatten
    # dump before the fix).
    assert snap["yaml_text"] == text
    assert "REAL_SECRET_123" in snap["yaml_text"]
    # The redacted merged view is a separate, distinct field (its masking is
    # unit-tested in test_config_snapshot).
    assert isinstance(snap["effective_yaml"], str)
    assert snap["effective_yaml"]
    assert snap["effective_yaml"] != snap["yaml_text"]


def test_save_writes_list_nested_secret_verbatim(
    client: TestClient, beets_library_config_path: Path
) -> None:
    """Regression for the list-nested secret-destruction bug: saving the raw
    document writes real credentials back verbatim — the literal ``REDACTED``
    never lands on disk (the old redacted-merge wrote it over list-nested
    secrets like ``kodi: [{pwd: ...}]``).

    The fixture is the real kodiupdate shape: that plugin registers the section
    ``kodi`` (not ``kodiupdate``) and its config is a LIST of instances."""
    text = (
        beets_library_config_path.read_text()
        + "\nkodi:\n  - host: 10.0.0.5\n    port: 8080\n    pwd: REAL_KODI_PW\n"
    )
    beets_library_config_path.write_text(text)
    sha = _cas(client)
    snap = client.get("/api/config").json()
    assert "REAL_KODI_PW" in snap["yaml_text"]  # served raw

    r = client.post(
        "/api/config/save",
        json={"yaml_text": snap["yaml_text"], "base_sha256": sha},
    )
    assert r.status_code == 200
    on_disk = beets_library_config_path.read_text()
    assert "REAL_KODI_PW" in on_disk  # the real credential survives
    assert "REDACTED" not in on_disk  # and no tombstone was written


def test_save_preserves_comments(client: TestClient, beets_library_config_path: Path) -> None:
    """Regression for the flatten-dump bug: the editor edits the raw file, so a
    Save keeps the user's hand-authored comments instead of replacing the file
    with a comment-free, every-default-pinned dump."""
    original = beets_library_config_path.read_text()
    edited = "# my hand-authored note\n" + original
    sha = _cas(client)
    r = client.post(
        "/api/config/save",
        json={"yaml_text": edited, "base_sha256": sha},
    )
    assert r.status_code == 200
    assert "# my hand-authored note" in beets_library_config_path.read_text()
