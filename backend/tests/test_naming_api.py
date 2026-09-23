import hashlib
import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.beets.library import LibraryHandle
from tests.conftest import answer_before_a_fifo_blocks


def _cfg_sha(client: TestClient) -> str:
    body = client.get("/api/config/naming").json()
    return str(body["sha256"])


def test_get_naming_returns_split_and_previews(client: TestClient) -> None:
    r = client.get("/api/config/naming")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {
        "default",
        "comp",
        "singleton",
        "custom",
        "replace",
        "sha256",
        "previews",
    }


def test_preview_renders_against_synthetic_when_empty(client: TestClient) -> None:
    r = client.post(
        "/api/config/naming/preview",
        json={
            "rules": [{"query": "default", "template": "$albumartist/$album/$track $title"}],
            "replace": [],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["rendered"][0]["sample_path"] == "Adele/25/01 Hello.flac"
    assert body["replace_errors"] == []


def test_preview_reports_bad_replace_regex(client: TestClient) -> None:
    r = client.post(
        "/api/config/naming/preview",
        json={
            "rules": [{"query": "default", "template": "$album/$title"}],
            "replace": [{"pattern": "(", "replacement": "_"}],
        },
    )
    body = r.json()
    assert body["replace_errors"][0]["index"] == 0


def test_save_naming_writes_and_sets_apply_pending(client: TestClient) -> None:
    sha = _cfg_sha(client)
    r = client.post(
        "/api/config/naming/save",
        json={
            "rules": [{"query": "default", "template": "$albumartist/$album/$track $title"}],
            "replace": [{"pattern": "[?]", "replacement": "_"}],
            "base_sha256": sha,
        },
    )
    assert r.status_code == 200
    assert r.json()["apply_pending"] is True
    assert client.get("/api/config/naming").json()["default"].endswith("$track $title")


def test_save_naming_409_on_stale_sha(client: TestClient) -> None:
    r = client.post(
        "/api/config/naming/save",
        json={"rules": [], "replace": [], "base_sha256": "stale"},
    )
    assert r.status_code == 409


_UNCLOSED_TEXT = (
    "while parsing a flow sequence\n"
    '  in "<unicode string>", line 2, column 4:\n'
    "    x: [unclosed\n"
    "       ^ (line: 2)\n"
    "expected ',' or ']', but got '<stream end>'\n"
    '  in "<unicode string>", line 3, column 1:\n'
    "    \n"
    "    ^ (line: 3)"
)

_BROKEN_ON_DISK = pytest.mark.parametrize(
    ("text", "error"),
    [("a: 1\nx: [unclosed\n", _UNCLOSED_TEXT), ("a: 1\nx: !!bool ture\n", "KeyError: 'ture'")],
    ids=["syntax", "mistyped-tag"],
)


@_BROKEN_ON_DISK
def test_get_naming_names_the_parse_error_of_the_file_on_disk(
    client: TestClient, beets_library: LibraryHandle, text: str, error: str
) -> None:
    """Measured before: a bare 500 for both, while ``GET /api/config`` answered 200."""
    beets_library.config_path.write_text(text, encoding="utf-8")

    r = client.get("/api/config/naming")

    assert r.status_code == 422, r.text
    assert r.json() == {"detail": f"config.yaml does not parse: {error}"}
    assert client.get("/api/config").status_code == 200


@_BROKEN_ON_DISK
def test_save_naming_names_the_parse_error_of_the_file_on_disk(
    client: TestClient, beets_library: LibraryHandle, text: str, error: str
) -> None:
    """The base hash matches the broken file, so the save reaches its re-read."""
    beets_library.config_path.write_text(text, encoding="utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    r = client.post(
        "/api/config/naming/save",
        json={"rules": [], "replace": [], "base_sha256": sha},
    )

    assert r.status_code == 422, r.text
    assert r.json() == {
        "detail": [
            {"loc": "", "msg": f"config.yaml does not parse: {error}", "type": "config_on_disk"}
        ]
    }
    assert beets_library.config_path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("text", ["", "# nothing set here\n"], ids=["empty", "comment-only"])
def test_get_naming_reads_an_empty_file_as_beets_defaults(
    client: TestClient, beets_library: LibraryHandle, text: str
) -> None:
    """beets' loader reads these as no settings; before, ``None.get`` was a bare 500."""
    beets_library.config_path.write_text("a: 1\n", encoding="utf-8")
    defaults = client.get("/api/config/naming").json()
    beets_library.config_path.write_text(text, encoding="utf-8")

    r = client.get("/api/config/naming")

    assert r.status_code == 200, r.text
    assert {k: v for k, v in r.json().items() if k != "sha256"} == {
        k: v for k, v in defaults.items() if k != "sha256"
    }


_NOT_A_MAPPING = pytest.mark.parametrize(
    "text",
    ["- a\n", "hello\n", "~\n", "# a\n~\n# b\n", "[]\n", "false\n", "0\n", "''\n"],
    ids=[
        "list",
        "scalar",
        "null",
        "null-with-comments",
        "empty-list",
        "false",
        "zero",
        "empty-str",
    ],
)


@_NOT_A_MAPPING
def test_get_naming_refuses_a_file_that_is_not_a_mapping(
    client: TestClient, beets_library: LibraryHandle, text: str
) -> None:
    """beets refuses the first two and reads the falsy ones as no settings.

    Refused here all the same: a save could not keep a falsy file's comments.
    """
    beets_library.config_path.write_text(text, encoding="utf-8")

    r = client.get("/api/config/naming")

    assert r.status_code == 422, r.text
    assert r.json() == {"detail": "config.yaml must be a mapping of settings."}


@_NOT_A_MAPPING
def test_save_naming_refuses_a_file_that_is_not_a_mapping(
    client: TestClient, beets_library: LibraryHandle, text: str
) -> None:
    beets_library.config_path.write_text(text, encoding="utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    r = client.post(
        "/api/config/naming/save",
        json={"rules": [], "replace": [], "base_sha256": sha},
    )

    assert r.status_code == 422, r.text
    assert r.json() == {
        "detail": [
            {
                "loc": "",
                "msg": "config.yaml must be a mapping of settings.",
                "type": "config_on_disk",
            }
        ]
    }
    assert beets_library.config_path.read_text(encoding="utf-8") == text


def test_save_naming_writes_into_an_empty_file(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    beets_library.config_path.write_text("", encoding="utf-8")
    sha = hashlib.sha256(b"").hexdigest()

    r = client.post(
        "/api/config/naming/save",
        json={
            "rules": [{"query": "default", "template": "$artist/$title"}],
            "replace": [],
            "base_sha256": sha,
        },
    )

    assert r.status_code == 200, r.text
    assert beets_library.config_path.read_text(encoding="utf-8") == (
        "paths:\n  default: $artist/$title\n"
    )


_SAVED_RULE = {"query": "default", "template": "$artist/$title"}


@pytest.mark.parametrize(
    ("text", "rules", "written"),
    [
        ("# a\n# b\n", [_SAVED_RULE], "# a\n# b\npaths:\n  default: $artist/$title\n"),
        ("# a\n# b\n", [], "# a\n# b\n{}\n"),
        ("# a", [_SAVED_RULE], "# a\npaths:\n  default: $artist/$title\n"),
        ("%YAML 1.1\n---\n# a\n", [_SAVED_RULE], "# a\npaths:\n  default: $artist/$title\n"),
    ],
    ids=["comments", "comments-no-rules", "no-final-newline", "markers"],
)
def test_save_naming_keeps_the_comments_of_a_file_with_no_settings(
    client: TestClient,
    beets_library: LibraryHandle,
    text: str,
    rules: list[dict[str, str]],
    written: str,
) -> None:
    """Measured before: the 200 wrote only the new keys and every comment was gone."""
    from app.beets.setup import read_config_document

    cfg = beets_library.config_path
    cfg.write_text(text, encoding="utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    r = client.post(
        "/api/config/naming/save",
        json={"rules": rules, "replace": [], "base_sha256": sha},
    )

    assert r.status_code == 200, r.text
    assert cfg.read_text(encoding="utf-8") == written
    assert read_config_document(cfg) == ({"paths": {"default": "$artist/$title"}} if rules else {})


def test_save_naming_writes_no_as_a_bool_under_a_document_marker(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """``---`` made ruamel read ``no`` as a string, which the save wrote as ``'no'``.

    beets then refused ``import.write`` as "must be a bool, not str" on Apply.
    """
    from app.beets.setup import read_config_document

    cfg = beets_library.config_path
    text = "---\nimport: {write: no, copy: yes, move: no}\n"
    cfg.write_text(text, encoding="utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    r = client.post(
        "/api/config/naming/save",
        json={"rules": [_SAVED_RULE], "replace": [], "base_sha256": sha},
    )

    assert r.status_code == 200, r.text
    assert cfg.read_text(encoding="utf-8") == (
        "import: {write: false, copy: true, move: false}\npaths:\n  default: $artist/$title\n"
    )
    assert read_config_document(cfg) == {
        "import": {"write": False, "copy": True, "move": False},
        "paths": {"default": "$artist/$title"},
    }


def test_save_naming_quotes_a_question_mark_in_a_flow_mapping(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """Without ``yaml.version`` on the dump, ``\\?`` was written bare: beets could not parse it."""
    from app.beets.setup import read_config_document

    cfg = beets_library.config_path
    text = "{directory: /music, library: library.db}\n"
    cfg.write_text(text, encoding="utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    r = client.post(
        "/api/config/naming/save",
        json={
            "rules": [_SAVED_RULE],
            "replace": [{"pattern": "\\?", "replacement": "_"}],
            "base_sha256": sha,
        },
    )

    assert r.status_code == 200, r.text
    assert cfg.read_text(encoding="utf-8") == (
        "{directory: /music, library: library.db, paths: {default: $artist/$title},"
        " replace: {'\\?': _}}\n"
    )
    assert read_config_document(cfg) == {
        "directory": "/music",
        "library": "library.db",
        "paths": {"default": "$artist/$title"},
        "replace": {"\\?": "_"},
    }


def test_naming_reads_and_saves_one_letter_replacements_as_beets_reads_them(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """ruamel's own 1.1 table read ``n`` / ``y`` as bools: the GET served
    ``False`` / ``True``, and the save wrote the strings ``'False'`` / ``'True'``."""
    from app.beets.setup import read_config_document

    cfg = beets_library.config_path
    text = "---\nreplace:\n  'ñ': n\n  'ý': y\n"
    cfg.write_text(text, encoding="utf-8")
    rows = [{"pattern": "ñ", "replacement": "n"}, {"pattern": "ý", "replacement": "y"}]

    body = client.get("/api/config/naming").json()

    assert body["replace"] == rows
    r = client.post(
        "/api/config/naming/save",
        json={"rules": [], "replace": body["replace"], "base_sha256": body["sha256"]},
    )
    assert r.status_code == 200, r.text
    assert cfg.read_text(encoding="utf-8") == "replace:\n  ñ: n\n  ý: y\n"
    assert read_config_document(cfg) == {"replace": {"ñ": "n", "ý": "y"}}


def _chain_to_dotfile(cfg: Path, mode: int) -> Path:
    """``cfg`` -> ``hop.yaml`` -> ``dotfiles/config.yaml``, both links absolute."""
    dotfile = cfg.parent / "dotfiles" / "config.yaml"
    dotfile.parent.mkdir()
    dotfile.write_text(cfg.read_text(encoding="utf-8"), encoding="utf-8")
    dotfile.chmod(mode)
    hop = cfg.parent / "hop.yaml"
    hop.symlink_to(dotfile)
    cfg.unlink()
    cfg.symlink_to(hop)
    return dotfile


@pytest.mark.parametrize("umask", [0o000, 0o077], ids=["umask000", "umask077"])
@pytest.mark.parametrize("mode", [0o600, 0o444], ids=["0600", "0444"])
def test_save_naming_over_a_symlinked_config_writes_the_target_and_keeps_the_link(
    client: TestClient, beets_library: LibraryHandle, mode: int, umask: int
) -> None:
    """The Naming twin of the Save test, over a chain of two links."""
    cfg = beets_library.config_path
    dotfile = _chain_to_dotfile(cfg, mode)
    hop = cfg.parent / "hop.yaml"
    sha = _cfg_sha(client)

    old_umask = os.umask(umask)
    try:
        r = client.post(
            "/api/config/naming/save",
            json={"rules": [_SAVED_RULE], "replace": [], "base_sha256": sha},
        )
    finally:
        os.umask(old_umask)

    assert r.status_code == 200, r.text
    assert (os.readlink(cfg), os.readlink(hop)) == (str(hop), str(dotfile))
    text = dotfile.read_text(encoding="utf-8")
    assert text.endswith("paths:\n  default: $artist/$title\n")
    assert stat.S_IMODE(dotfile.stat().st_mode) == mode
    assert sorted(os.listdir(dotfile.parent)) == ["config.yaml"]
    body = client.get("/api/config/naming").json()
    assert body["default"] == "$artist/$title"
    assert body["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits this test sets")
def test_save_naming_refuses_when_config_yaml_cannot_be_written_and_changes_nothing(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """Measured before: a bare 500."""
    cfg = beets_library.config_path
    dotfile = _chain_to_dotfile(cfg, 0o600)
    before = dotfile.read_bytes()
    sha = _cfg_sha(client)

    dotfile.parent.chmod(0o555)
    try:
        r = client.post(
            "/api/config/naming/save",
            json={"rules": [_SAVED_RULE], "replace": [], "base_sha256": sha},
        )
    finally:
        dotfile.parent.chmod(0o755)

    assert r.status_code == 422, r.text
    assert r.json() == {
        "detail": [
            {
                "loc": "",
                "msg": "config.yaml could not be written: Permission denied.",
                "type": "config_on_disk",
            }
        ]
    }
    assert os.readlink(cfg) == str(cfg.parent / "hop.yaml")
    assert dotfile.read_bytes() == before
    assert stat.S_IMODE(dotfile.stat().st_mode) == 0o600
    assert sorted(os.listdir(dotfile.parent)) == ["config.yaml"]


def _unreadable_config(cfg: Path, shape: str) -> str:
    """Make ``cfg`` unusable as ``shape``; return the sentence the Naming routes answer."""
    cfg.unlink()
    if shape == "directory":
        cfg.mkdir()
    elif shape == "fifo":
        os.mkfifo(cfg)
    elif shape == "device-link":
        cfg.symlink_to("/dev/null")
    elif shape == "permission":
        cfg.write_text("a: 1\n", encoding="utf-8")
        cfg.chmod(0)
        return "config.yaml could not be read: Permission denied."
    else:
        return "config.yaml could not be read: No such file or directory."
    return "config.yaml is not a regular file."


_UNREADABLE = pytest.mark.parametrize(
    "shape",
    [
        "absent",
        "directory",
        "fifo",
        "device-link",
        pytest.param(
            "permission",
            marks=pytest.mark.skipif(
                os.geteuid() == 0, reason="root ignores the permission bits this test sets"
            ),
        ),
    ],
)


@_UNREADABLE
def test_get_naming_refuses_a_config_it_cannot_read(
    client: TestClient, beets_library: LibraryHandle, shape: str
) -> None:
    """Before, a bare 500, and a FIFO blocked; ``GET /api/config`` answers 200 for the same file."""
    cfg = beets_library.config_path
    message = _unreadable_config(cfg, shape)

    r = answer_before_a_fifo_blocks(lambda: client.get("/api/config/naming"), cfg)

    assert r.status_code == 422, r.text
    assert r.json() == {"detail": message}
    assert answer_before_a_fifo_blocks(lambda: client.get("/api/config"), cfg).status_code == 200


@_UNREADABLE
def test_save_naming_refuses_a_config_it_cannot_read(
    client: TestClient, beets_library: LibraryHandle, shape: str
) -> None:
    """A FIFO blocked the save while it held ``_SAVE_LOCK``, so every later save waited."""
    cfg = beets_library.config_path
    message = _unreadable_config(cfg, shape)

    r = answer_before_a_fifo_blocks(
        lambda: client.post(
            "/api/config/naming/save",
            json={
                "rules": [_SAVED_RULE],
                "replace": [],
                "base_sha256": hashlib.sha256(b"").hexdigest(),
            },
        ),
        cfg,
    )

    assert r.status_code == 422, r.text
    assert r.json() == {"detail": [{"loc": "", "msg": message, "type": "config_on_disk"}]}
    assert os.path.lexists(cfg) is (shape != "absent")
    assert os.path.islink(cfg) is (shape == "device-link")
    assert answer_before_a_fifo_blocks(lambda: client.get("/api/config"), cfg).status_code == 200
