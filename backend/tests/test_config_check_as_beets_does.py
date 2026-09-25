"""Validate and Save check config.yaml the way beets reads it (owner rulings 2026-09-23/25).

Every case of the BACKLOG entry "Save accepts a config that stops MusicDrop
starting", and the ones ``research-map.md`` §6 added, at BOTH routes: Validate
lists the row, Save answers 422 with exactly those rows and writes nothing. The
texts beets accepts save as typed.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import beets
import beetsplug
import pytest
from beets import plugins
from fastapi.testclient import TestClient

from app.beets.library import LibraryHandle
from app.beets.setup import read_config_document


def _cas(client: TestClient) -> str:
    return str(client.get("/api/config").json()["sha256"])


def _head(beets_library: LibraryHandle) -> str:
    """Lines 1-2: a ``directory:`` and ``library:`` every policy accepts."""
    return f"directory: {beets_library.lib.directory.decode()}\nlibrary: library.db\n"


def _read(loc: str, msg: str, line: int | None, column: int | None) -> dict[str, object]:
    return {"loc": loc, "msg": msg, "type": "beets_read", "line": line, "column": column}


def _refused(
    client: TestClient, beets_library: LibraryHandle, text: str
) -> list[dict[str, object]]:
    """Validate's rows for ``text``; asserts Save refuses with the same rows and writes nothing."""
    before = beets_library.config_path.read_bytes()
    lint = client.post("/api/config/validate", json={"yaml_text": text})
    r = client.post("/api/config/save", json={"yaml_text": text, "base_sha256": _cas(client)})
    assert lint.status_code == 200, lint.text
    errors: list[dict[str, object]] = lint.json()["errors"]
    assert errors, "Validate was clean"
    assert (r.status_code, r.json()) == (422, {"detail": errors})
    assert beets_library.config_path.read_bytes() == before
    # No Python class name reaches a row (``<class 'pathlib.Path'>`` did).
    assert not [e for e in errors if "<class" in str(e["msg"])], errors
    return errors


# A typed read beets makes at start-up, or of an ``import.*`` switch. Line 3 is
# the first line after ``_head``. The row is confuse's or beets' own sentence.
_TYPED = [
    ("musicbrainz: no\n", _read("", "musicbrainz must be a dict, not bool", 3, 13)),
    (
        "plugins: 5\n",
        _read("plugins", "must be a whitespace-separated string or a list", 3, 9),
    ),
    (
        "pluginpath: 5\n",
        _read("pluginpath", "must be a whitespace-separated string or a list", 3, 12),
    ),
    (
        "disabled_plugins: 5\n",
        _read("disabled_plugins", "must be a whitespace-separated string or a list", 3, 18),
    ),
    (
        "plugins: [musicbrainz, deezer]\ndeezer: no\n",
        _read("", "deezer must be a collection, not bool", 4, 8),
    ),
    ("verbose: x\n", _read("verbose", "must be a number", 3, 9)),
    ("timeout: x\n", _read("timeout", "must be numeric, not str", 3, 9)),
    ("replace: 5\n", _read("replace", "must be a Mapping, not int", 3, 9)),
    ("replace:\n  '[': _\n", _read("", "Malformed regular expression in replace: [", 4, 2)),
    (
        "create_backup_before_migrations: 5\n",
        _read("create_backup_before_migrations", "must be a bool, not int", 3, 33),
    ),
    ("import:\n  write: 1\n", _read("import.write", "must be a bool, not int", 4, 9)),
    ("import:\n  copy: 1\n", _read("import.copy", "must be a bool, not int", 4, 8)),
    (
        "import:\n  reflink: bad\n",
        _read("import.reflink", "must be one of ['auto', 'True', 'False'], not 'bad'", 4, 11),
    ),
    (
        "import:\n  resume: later\n",
        _read("import.resume", "must be one of ['True', 'False', 'ask'], not 'later'", 4, 10),
    ),
    (
        "import:\n  duplicate_action: banana\n",
        _read(
            "import.duplicate_action",
            "must be one of ['skip', 'merge', 'remove', 'keep', 'ask', 'upgrade'], not 'banana'",
            4,
            20,
        ),
    ),
    (
        "match:\n  strong_rec_thresh: high\n",
        _read("match.strong_rec_thresh", "must be numeric, not str", 4, 21),
    ),
]


@pytest.mark.parametrize(
    ("section", "row"),
    _TYPED,
    ids=[
        "musicbrainz-no",
        "plugins-int",
        "pluginpath-int",
        "disabled-plugins-int",
        "deezer-no",
        "verbose-str",
        "timeout-str",
        "replace-int",
        "replace-malformed",
        "backup-int",
        "write-int",
        "copy-int",
        "reflink-bad",
        "resume-bad",
        "duplicate-action-bad",
        "threshold-str",
    ],
)
def test_a_value_beets_reads_typed_is_refused_in_beets_words(
    client: TestClient, beets_library: LibraryHandle, section: str, row: dict[str, object]
) -> None:
    """Measured before: each one clean at Validate and saved, then the next
    start, Apply or import refused it. One row per problem."""
    assert _refused(client, beets_library, _head(beets_library) + section) == [row]


@pytest.mark.parametrize(
    ("text", "row"),
    [
        (
            "directory: 5\nlibrary: library.db\n",
            _read("directory", "must be a filename, not int", 1, 11),
        ),
        (
            "directory:\nlibrary: library.db\n",
            _read("directory", "must be a filename, not NoneType", 1, 10),
        ),
        ("DIR\nlibrary: [a]\n", _read("library", "must be a filename, not list", 2, 9)),
    ],
    ids=["directory-int", "directory-empty", "library-list"],
)
def test_a_path_beets_cannot_read_is_one_row_without_a_class_name(
    client: TestClient, beets_library: LibraryHandle, text: str, row: dict[str, object]
) -> None:
    """Measured before: "Input is not a valid path for <class 'pathlib.Path'>".
    The schema's own row for the same key is dropped: one problem, one row."""
    text = text.replace("DIR", f"directory: {beets_library.lib.directory.decode()}")
    assert _refused(client, beets_library, text) == [row]


def test_a_merged_key_gets_the_line_it_is_written_on(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """``<<: *b`` brings ``timeout:`` in; the row goes on the anchor's line."""
    text = _head(beets_library) + "_b: &b\n  timeout: x\n<<: *b\n"
    assert _refused(client, beets_library, text) == [
        _read("timeout", "must be numeric, not str", 4, 11)
    ]


@pytest.mark.parametrize(
    ("section", "line"),
    [
        ("x: !!python/object/apply:os.system ['true']\n", 3),
        ("? [a]\n: 1\n", 3),
        ("k: =\n", 3),
        ("k: <<\n", 3),
        ("k: &x.y 1\n", 3),
        ("k: &é 1\n", 3),
        ("k: &a&b 1\n", 3),
        ("k: &a:b 1\nm: *a:b\n", 4),
        ("k: &t:x 1\nm: &t:y 2\n", 4),
        ("import: !!omap [copy: yes]\n", 3),
        ("x: !!float abc\n", None),
        ("x: !!bool ture\n", None),
    ],
    ids=[
        "python-tag",
        "complex-key",
        "bare-equals",
        "bare-merge",
        "anchor-dot",
        "anchor-e-acute",
        "anchor-ampersand",
        "colon-anchor-aliased",
        "colon-anchor-reused",
        "omap-sequence",
        "float-tag",
        "bool-tag",
    ],
)
def test_yaml_beets_loader_refuses_is_refused(
    client: TestClient, beets_library: LibraryHandle, section: str, line: int | None
) -> None:
    """ruamel accepted every one of these; beets' loader, the one boot uses, does not."""
    [row] = _refused(client, beets_library, _head(beets_library) + section)
    assert (row["loc"], row["type"], row["line"]) == ("", "yaml_parse", line)


def test_a_float_tag_prints_nothing(
    client: TestClient, beets_library: LibraryHandle, capfd: pytest.CaptureFixture[str]
) -> None:
    """ruamel's warning printed the whole value of ``!!float Zq7Secrt`` to stderr.
    beets' loader prints nothing; the row quotes it back to the operator who typed it."""
    text = _head(beets_library) + "plex: !!float Zq7Secrt\n"
    capfd.readouterr()

    [row] = _refused(client, beets_library, text)

    out, err = capfd.readouterr()
    assert "zq7" not in (out + err).lower()
    assert row["msg"] == "ValueError: could not convert string to float: 'zq7secrt'"


@pytest.mark.parametrize(
    "section",
    [
        "k: &a:b 1\n",
        "import: !!omap {copy: yes}\n",
        "lastgenre:\n  count: 1\n  count: 2\n",
        "paths:\n  default: %the{$albumartist}/$album/$title\n",
        "permissions:\n  file: -0644\n",
        "plugins: [musicbrainz, the, inline]\n",
        "match:\n  strong_rec_thresh: 0.5\n",
        "# a comment\nimport:\n  autotag: yes   # kept\n",
    ],
    ids=[
        "colon-anchor-unused",
        "omap-mapping",
        "duplicate-key",
        "percent-plain",
        "negative-octal",
        "plugins-beets-has",
        "threshold-in-range",
        "comments",
    ],
)
def test_what_beets_loads_is_clean_and_saves_as_typed(
    client: TestClient, beets_library: LibraryHandle, section: str
) -> None:
    """The other direction: ruamel refused the first four, the allowlist refused
    ``the`` and ``inline``, and the dump rewrote ``-0644`` into a value beets
    refuses. What lands on disk is the text beets was asked about."""
    text = _head(beets_library) + section

    lint = client.post("/api/config/validate", json={"yaml_text": text})
    r = client.post("/api/config/save", json={"yaml_text": text, "base_sha256": _cas(client)})

    assert lint.json()["errors"] == []
    assert r.status_code == 200, r.text
    assert beets_library.config_path.read_bytes() == text.encode("utf-8")
    read_config_document(beets_library.config_path)


def test_a_skipped_include_is_refused_at_save_too(
    client: TestClient, beets_library: LibraryHandle
) -> None:
    """Validate and Save only advised on it; Apply and boot refuse it."""
    text = _head(beets_library) + "include:\n  - not-written-yet.yaml\n"

    assert _refused(client, beets_library, text) == [
        {
            "loc": "include",
            "msg": "beets would skip the include 'not-written-yet.yaml': No such file or directory",
            "type": "include_skipped",
            "line": 4,
            "column": 2,
        }
    ]


def test_a_check_leaves_the_live_beets_state_as_it_found_it(
    client: TestClient, beets_library: LibraryHandle, tmp_path: Path
) -> None:
    """``get_plugin_names`` adds to ``sys.path``, ``beetsplug.__path__`` and the
    global config (``beets/plugins.py:478-486``); the check replays its reads on
    a candidate instead, and loads no plugin."""
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (tmp_path / "overlay.yaml").write_text("timeout: 7\n", encoding="utf-8")
    text = (
        _head(beets_library)
        + f"pluginpath: [{plugin_dir}]\n"
        + "plugins: [musicbrainz, deezer, the, fetchart]\n"
        + "disabled_plugins: [the]\n"
        + "musicbrainz: {enabled: yes}\n"
        + f"include: [{tmp_path / 'overlay.yaml'}]\n"
    )
    sources = list(beets.config.sources)
    flat = beets.config.flatten()
    path, namespace = list(sys.path), list(beetsplug.__path__)
    instances = list(plugins.find_plugins())

    lint = client.post("/api/config/validate", json={"yaml_text": text})
    r = client.post("/api/config/save", json={"yaml_text": text, "base_sha256": _cas(client)})

    assert lint.json()["errors"] == []
    assert r.status_code == 200, r.text
    assert beets.config.sources == sources
    assert all(a is b for a, b in zip(beets.config.sources, sources, strict=True))
    assert beets.config.flatten() == flat
    assert (list(sys.path), list(beetsplug.__path__)) == (path, namespace)
    assert list(plugins.find_plugins()) == instances
    assert hashlib.sha256(text.encode()).hexdigest() == _cas(client)
