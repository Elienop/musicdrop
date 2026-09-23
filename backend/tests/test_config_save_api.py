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
import logging
import os
import stat
from pathlib import Path

import pytest
import yaml as pyyaml
from fastapi.testclient import TestClient

from app.beets.setup import read_config_document
from tests.conftest import answer_before_a_fifo_blocks


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


def test_save_writes_no_as_a_bool_under_a_document_marker(
    client: TestClient, beets_library_config_path: Path
) -> None:
    """``---`` made ruamel read ``no`` as a string, which Save wrote as ``'no'``.

    confuse reads that string as TRUE, so ``auto: no`` turned fetchart on.
    """
    cfg = beets_library_config_path
    music = cfg.read_text(encoding="utf-8").splitlines()[0]
    sha = _cas(client)
    text = f"---\n{music}\nlibrary: library.db\nimport:\n  write: no\nfetchart:\n  auto: no\n"

    r = client.post("/api/config/save", json={"yaml_text": text, "base_sha256": sha})

    assert r.status_code == 200, r.text
    assert cfg.read_text(encoding="utf-8") == (
        f"{music}\nlibrary: library.db\nimport:\n  write: false\nfetchart:\n  auto: false\n"
    )
    document = read_config_document(cfg)
    flags = (document["import"]["write"], document["fetchart"]["auto"])
    assert flags == (False, False)


def test_save_writes_an_octal_back_as_it_was_written(
    client: TestClient, beets_library_config_path: Path
) -> None:
    """Without ``yaml.version`` on the dump, ``0644`` came back as ``!!int '0o644'``."""
    cfg = beets_library_config_path
    music = cfg.read_text(encoding="utf-8").splitlines()[0]
    text = f"{music}\nlibrary: library.db\npermissions:\n  file: 0644\n  dir: 0755\n"

    r = client.post("/api/config/save", json={"yaml_text": text, "base_sha256": _cas(client)})

    assert r.status_code == 200, r.text
    assert cfg.read_text(encoding="utf-8") == text


def test_save_writes_one_letter_replacements_as_beets_reads_them(
    client: TestClient, beets_library_config_path: Path
) -> None:
    """ruamel's own 1.1 table read ``n`` / ``y`` as bools, so a Save wrote
    ``false`` / ``true``; after Apply every beets path raised ``TypeError``."""
    cfg = beets_library_config_path
    music = cfg.read_text(encoding="utf-8").splitlines()[0]
    text = f"{music}\nlibrary: library.db\nreplace:\n  'ñ': n\n  'ý': y\n"

    r = client.post("/api/config/save", json={"yaml_text": text, "base_sha256": _cas(client)})

    assert r.status_code == 200, r.text
    assert cfg.read_text(encoding="utf-8") == text
    assert read_config_document(cfg)["replace"] == {"ñ": "n", "ý": "y"}


def _link_to_dotfile(cfg: Path, mode: int) -> Path:
    """Move ``cfg`` into a ``dotfiles`` folder and leave a RELATIVE link to it."""
    dotfile = cfg.parent / "dotfiles" / "config.yaml"
    dotfile.parent.mkdir()
    dotfile.write_text(cfg.read_text(encoding="utf-8"), encoding="utf-8")
    dotfile.chmod(mode)
    cfg.unlink()
    cfg.symlink_to(Path("dotfiles") / "config.yaml")
    return dotfile


# umask 000 shows a mode taken from the umask, 077 one the create narrowed.
_UMASK_PROOF = pytest.mark.parametrize("umask", [0o000, 0o077], ids=["umask000", "umask077"])


@_UMASK_PROOF
@pytest.mark.parametrize("mode", [0o600, 0o444], ids=["0600", "0444"])
def test_save_over_a_symlinked_config_writes_the_target_and_keeps_the_link(
    client: TestClient, beets_library_config_path: Path, mode: int, umask: int
) -> None:
    """Measured before: the link was replaced by a regular file, so a dotfiles copy
    stopped getting edits (owner ruling, vault decisions #59)."""
    cfg = beets_library_config_path
    music = cfg.read_text(encoding="utf-8").splitlines()[0]
    dotfile = _link_to_dotfile(cfg, mode)
    text = f"{music}\nlibrary: library.db\n# saved\n"

    old_umask = os.umask(umask)
    try:
        r = client.post("/api/config/save", json={"yaml_text": text, "base_sha256": _cas(client)})
    finally:
        os.umask(old_umask)

    assert r.status_code == 200, r.text
    assert r.json()["apply_pending"] is True
    assert os.readlink(cfg) == os.path.join("dotfiles", "config.yaml")
    assert dotfile.read_text(encoding="utf-8") == text
    assert stat.S_IMODE(dotfile.stat().st_mode) == mode
    assert sorted(os.listdir(dotfile.parent)) == ["config.yaml"]
    snapshot = client.get("/api/config").json()
    assert snapshot["yaml_text"] == text
    assert snapshot["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits this test sets")
@pytest.mark.parametrize("shape", ["link", "regular"])
def test_save_refuses_when_config_yaml_cannot_be_written_and_changes_nothing(
    client: TestClient, beets_library_config_path: Path, shape: str
) -> None:
    """Measured before: a 500 for a regular file, and a 200 that replaced a link
    with a regular file. The folder the temp goes in is read-only: the link
    target's for a link, config.yaml's own for a regular file."""
    cfg = beets_library_config_path
    target = _link_to_dotfile(cfg, 0o600) if shape == "link" else cfg
    before = (target.read_bytes(), stat.S_IMODE(target.stat().st_mode))
    listing = sorted(os.listdir(target.parent))
    sha = _cas(client)

    target.parent.chmod(0o555)
    try:
        r = client.post(
            "/api/config/save", json={"yaml_text": before[0].decode() + "# x\n", "base_sha256": sha}
        )
    finally:
        target.parent.chmod(0o755)

    assert r.status_code == 422, r.text
    assert r.json() == {
        "detail": [
            {
                "loc": "",
                "msg": "config.yaml could not be written: Permission denied.",
                "type": "config_on_disk",
                "line": None,
                "column": None,
            }
        ]
    }
    assert os.path.islink(cfg) is (shape == "link")
    if shape == "link":
        assert os.readlink(cfg) == os.path.join("dotfiles", "config.yaml")
    after = (target.read_bytes(), stat.S_IMODE(target.stat().st_mode))
    assert after == before
    assert sorted(os.listdir(target.parent)) == listing


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


_REUSED_ANCHOR = (
    "directory: /tmp/music\nlibrary: /tmp/x\nplex:\n  token: &a Zq7Secret\n  user: &a u\n"
)


def test_save_refuses_a_reused_anchor_and_writes_nothing(
    client: TestClient, beets_library_config_path: Path
) -> None:
    """Measured before: 200, and the next start refused the file beets cannot load."""
    before = beets_library_config_path.read_bytes()

    r = client.post(
        "/api/config/save", json={"yaml_text": _REUSED_ANCHOR, "base_sha256": _cas(client)}
    )

    assert r.status_code == 422, r.text
    # The row Validate paints, pinned whole in test_config_validate_api.
    lint = client.post("/api/config/validate", json={"yaml_text": _REUSED_ANCHOR}).json()
    assert r.json() == {"detail": lint["errors"]}
    assert lint["errors"][0]["type"] == "yaml_parse"
    assert beets_library_config_path.read_bytes() == before


def test_a_reused_anchor_reaches_no_log_or_stream(
    client: TestClient,
    beets_library_config_path: Path,
    recwarn: pytest.WarningsRecorder,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ruamel's warning quoted both lines, token included, and the default filters
    print a warning to stderr. The recorder sees every warning, printed or not."""
    caplog.set_level(logging.DEBUG)
    cfg = beets_library_config_path
    sha = _cas(client)
    on_disk = cfg.read_text(encoding="utf-8") + "plex:\n  token: &a Zq7Secret\n  user: &a u\n"

    statuses = [
        client.post("/api/config/validate", json={"yaml_text": _REUSED_ANCHOR}).status_code,
        client.post(
            "/api/config/save", json={"yaml_text": _REUSED_ANCHOR, "base_sha256": sha}
        ).status_code,
    ]
    cfg.write_text(on_disk, encoding="utf-8")
    statuses += [
        client.get("/api/config/naming").status_code,
        client.post(
            "/api/config/naming/save",
            json={
                "rules": [],
                "replace": [],
                "base_sha256": hashlib.sha256(on_disk.encode()).hexdigest(),
            },
        ).status_code,
    ]

    assert [str(w.message) for w in recwarn.list] == []
    out, err = capfd.readouterr()
    assert "Zq7" not in out + err
    assert "Zq7" not in caplog.text
    assert statuses == [200, 422, 422, 422]


def test_a_reused_anchor_on_a_mapping_is_refused_and_reaches_no_log_or_stream(
    client: TestClient,
    beets_library_config_path: Path,
    recwarn: pytest.WarningsRecorder,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Measured: a composer that refused only a reused anchor on a scalar passed
    every scalar test, and printed the ``plex:`` line below to stderr."""
    caplog.set_level(logging.DEBUG)
    text = "directory: /tmp/music\nlibrary: /tmp/x\nplex: &a {token: Zq7Secret}\nother: &a [1]\n"
    with pytest.raises(pyyaml.YAMLError, match="found duplicate anchor"):
        pyyaml.safe_load(text)
    before = beets_library_config_path.read_bytes()

    lint = client.post("/api/config/validate", json={"yaml_text": text})
    r = client.post("/api/config/save", json={"yaml_text": text, "base_sha256": _cas(client)})

    assert lint.status_code == 200
    assert [(e["type"], e["line"]) for e in lint.json()["errors"]] == [("yaml_parse", 4)]
    assert (r.status_code, r.json()) == (422, {"detail": lint.json()["errors"]})
    assert beets_library_config_path.read_bytes() == before
    assert [str(w.message) for w in recwarn.list] == []
    out, err = capfd.readouterr()
    assert "Zq7" not in out + err
    assert "Zq7" not in caplog.text


def _row(loc: str, msg: str, type_: str) -> dict[str, object]:
    return {"loc": loc, "msg": msg, "type": type_, "line": None, "column": None}


_NO_SETTINGS = [
    _row("directory", "Field required", "missing"),
    _row("library", "Field required", "missing"),
]
_NOT_A_MAPPING = [_row("", "config.yaml must be a mapping of settings.", "model_type")]


@pytest.mark.parametrize(
    ("text", "rows"),
    [
        ("", _NO_SETTINGS),
        ("# my beets config\n\n# more\n", _NO_SETTINGS),
        ("{}\n", _NO_SETTINGS),
        ("~\n", _NOT_A_MAPPING),
        ("[]\n", _NOT_A_MAPPING),
        ("false\n", _NOT_A_MAPPING),
        ("hello\n", _NOT_A_MAPPING),
        # beets reads it as {}; the Naming routes refuse it too.
        ("---\n...\n", _NOT_A_MAPPING),
    ],
    ids=[
        "empty",
        "comment-only",
        "empty-mapping",
        "null",
        "empty-list",
        "false",
        "scalar",
        "empty-document",
    ],
)
def test_validate_and_save_answer_a_top_level_with_no_settings_in_our_words(
    client: TestClient, beets_library_config_path: Path, text: str, rows: list[dict[str, object]]
) -> None:
    """Measured before: one row, "Input should be a valid dictionary or instance of
    KnownKeysSchema", for every one of these."""
    before = beets_library_config_path.read_bytes()

    lint = client.post("/api/config/validate", json={"yaml_text": text})
    r = client.post("/api/config/save", json={"yaml_text": text, "base_sha256": _cas(client)})

    assert (lint.status_code, lint.json()) == (200, {"errors": rows, "advisories": []})
    assert (r.status_code, r.json()) == (422, {"detail": rows})
    assert beets_library_config_path.read_bytes() == before


@pytest.mark.parametrize(
    ("section", "row"),
    [
        (
            "import: 5\n",
            {
                "loc": "import",
                "msg": "must be a mapping of settings.",
                "type": "model_type",
                "line": 3,
                "column": 8,
            },
        ),
        (
            "match: []\n",
            {
                "loc": "match",
                "msg": "must be a mapping of settings.",
                "type": "model_type",
                "line": 3,
                "column": 7,
            },
        ),
        # A key with no value: YAML null, placed on the line after it.
        (
            "import:\n",
            {
                "loc": "import",
                "msg": "must be a mapping of settings.",
                "type": "model_type",
                "line": 4,
                "column": 0,
            },
        ),
        # Controls: rows of every other type keep Pydantic's text.
        (
            "import:\n  copy: 5\n",
            {
                "loc": "import.copy",
                "msg": "Input should be a valid boolean, unable to interpret input",
                "type": "bool_parsing",
                "line": 4,
                "column": 8,
            },
        ),
        (
            "plugins: 5\n",
            {
                "loc": "plugins",
                "msg": "Input should be a valid list",
                "type": "list_type",
                "line": 3,
                "column": 9,
            },
        ),
    ],
    ids=["import-int", "match-list", "import-null", "control-bool", "control-list"],
)
def test_a_section_that_is_not_a_mapping_is_named_without_a_class(
    client: TestClient, beets_library_config_path: Path, section: str, row: dict[str, object]
) -> None:
    """Measured before: "Input should be a valid dictionary or instance of ImportSection"."""
    cfg = beets_library_config_path
    text = f"{cfg.read_text(encoding='utf-8').splitlines()[0]}\nlibrary: library.db\n{section}"
    before = cfg.read_bytes()

    lint = client.post("/api/config/validate", json={"yaml_text": text})
    r = client.post("/api/config/save", json={"yaml_text": text, "base_sha256": _cas(client)})

    assert (lint.status_code, lint.json()) == (200, {"errors": [row], "advisories": []})
    assert (r.status_code, r.json()) == (422, {"detail": [row]})
    assert cfg.read_bytes() == before


def test_save_422_on_schema_error(client: TestClient, tmp_path: Path) -> None:
    sha = _cas(client)
    # The fixture's own music dir, and a library under ``tmp_path``: a fixed path
    # such as ``/tmp/x`` made the answer depend on whether it existed.
    text = f"directory: {tmp_path / 'music'}\nlibrary: {tmp_path / 'x'}\nimport:\n  copy: maybe\n"
    r = client.post(
        "/api/config/save",
        json={
            "yaml_text": text,
            "base_sha256": sha,
        },
    )
    assert r.status_code == 422
    assert any(d["loc"] == "import.copy" for d in r.json()["detail"])


def test_save_refuses_write_n_and_writes_nothing(
    client: TestClient, beets_library_config_path: Path, tmp_path: Path
) -> None:
    """Round 10 made the Save keep ``write: n`` as written, where it used to
    rewrite it as ``false``. beets' ``.get(bool)`` refuses the string, so every
    import would fail."""
    before = beets_library_config_path.read_bytes()
    text = f"directory: {tmp_path / 'music'}\nlibrary: {tmp_path / 'x'}\nimport:\n  write: n\n"

    r = client.post("/api/config/save", json={"yaml_text": text, "base_sha256": _cas(client)})

    assert r.status_code == 422
    assert r.json()["detail"] == [
        {
            "loc": "import.write",
            "msg": "Value error, must be a bool: write yes or no, without quotes",
            "type": "value_error",
            "line": 4,
            "column": 9,
        }
    ]
    assert beets_library_config_path.read_bytes() == before


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


@pytest.mark.parametrize("base", ["served", "wrong"])
def test_save_refuses_a_utf16_config_whatever_the_base(
    client: TestClient, beets_library_config_path: Path, base: str
) -> None:
    """beets reads a UTF-16 file with a BOM; the editor opens it empty.

    Measured before: the served sha, or the one a 409 handed to "Overwrite
    anyway", let this Save replace the whole file, a Plex token included.
    """
    cfg = beets_library_config_path
    text = cfg.read_text(encoding="utf-8") + "plex:\n  token: Zq7Secret\n"
    raw = text.encode("utf-16")
    cfg.write_bytes(raw)
    assert read_config_document(cfg)["plex"] == {"token": "Zq7Secret"}
    snap = client.get("/api/config").json()
    assert (snap["yaml_text"], snap["sha256"]) == ("", hashlib.sha256(raw).hexdigest())
    sha = snap["sha256"] if base == "served" else hashlib.sha256(b"").hexdigest()

    r = client.post(
        "/api/config/save",
        json={"yaml_text": f"{text.splitlines()[0]}\nlibrary: library.db\n", "base_sha256": sha},
    )

    assert r.status_code == 422, r.text
    assert r.json() == {
        "detail": [
            {
                "loc": "",
                "msg": "config.yaml is not UTF-8.",
                "type": "config_on_disk",
                "line": None,
                "column": None,
            }
        ]
    }
    # The Naming Save's row. It too refuses before its sha compare.
    naming = client.post(
        "/api/config/naming/save",
        json={"rules": [], "replace": [], "base_sha256": snap["sha256"]},
    )
    assert (naming.status_code, naming.json()["detail"][0]["msg"]) == (
        422,
        "config.yaml is not UTF-8.",
    )
    assert cfg.read_bytes() == raw


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


_NOT_A_REGULAR_FILE = "config.yaml is not a regular file."


@pytest.mark.parametrize(
    ("shape", "message"),
    [
        ("absent", "config.yaml could not be read: No such file or directory."),
        ("directory", _NOT_A_REGULAR_FILE),
        ("fifo", _NOT_A_REGULAR_FILE),
        ("device-link", _NOT_A_REGULAR_FILE),
        pytest.param(
            "permission",
            "config.yaml could not be read: Permission denied.",
            marks=pytest.mark.skipif(
                os.geteuid() == 0, reason="root ignores the permission bits this test sets"
            ),
        ),
    ],
)
def test_save_refuses_a_config_it_cannot_read_and_writes_nothing(
    client: TestClient, beets_library_config_path: Path, shape: str, message: str
) -> None:
    """Measured before: a bare 500; a FIFO blocked while holding ``_SAVE_LOCK``.

    The base is the hash of no bytes, which ``/dev/null`` reads as, so a Save
    that opened the link would pass the CAS and replace it with a file.
    """
    cfg = beets_library_config_path
    text = cfg.read_text(encoding="utf-8")
    cfg.unlink()
    if shape == "directory":
        cfg.mkdir()
    elif shape == "fifo":
        os.mkfifo(cfg)
    elif shape == "device-link":
        cfg.symlink_to("/dev/null")
    elif shape == "permission":
        cfg.write_text(text, encoding="utf-8")
        cfg.chmod(0)
    listing = sorted(os.listdir(cfg.parent))

    r = answer_before_a_fifo_blocks(
        lambda: client.post(
            "/api/config/save",
            json={"yaml_text": text, "base_sha256": hashlib.sha256(b"").hexdigest()},
        ),
        cfg,
    )

    assert r.status_code == 422, r.text
    assert r.json() == {
        "detail": [
            {"loc": "", "msg": message, "type": "config_on_disk", "line": None, "column": None}
        ]
    }
    assert sorted(os.listdir(cfg.parent)) == listing
    assert os.path.lexists(cfg) is (shape != "absent")
    assert os.path.islink(cfg) is (shape == "device-link")
    if shape == "directory":
        assert os.listdir(cfg) == []
    if shape == "permission":
        cfg.chmod(0o600)
        assert cfg.read_text(encoding="utf-8") == text
