"""Layer-3 config editor — parse + validate (line/col mapping) tests.

Per the Layer-3 plan (Task 2), these tests pin three load-bearing decisions:

* ruamel parses `yes`/`no` as bool, as YAML 1.1 and beets' PyYAML do
  (`_Yaml11Resolver`) — verified by `test_parse_yes_no_as_bool`.
* invalid YAML surfaces as a `ruamel.yaml.YAMLError` for the caller to map
  to HTTP 422 — verified by `test_parse_invalid_yaml_raises`.
* schema errors carry a 1-based `(line, column)` derived from ruamel's
  `.lc.value(...)` so CodeMirror's lint gutter can render the marker —
  verified by `test_validate_returns_loc_with_line_col_for_known_key`.
"""

from __future__ import annotations

import re
from pathlib import Path

import beets
import pytest
import yaml as pyyaml

# NOTE: plan-verbatim used `from ruamel.yaml import YAMLError`, but the
# ruamel.yaml stubs only expose YAMLError from `ruamel.yaml.error` (its
# canonical home). Same class object at runtime — verified via identity.
# Using the canonical path keeps mypy --strict clean without a scoped
# suppression directive.
from ruamel.yaml.error import YAMLError

from app.beets.config_editor import _yaml, atomic_write, parse_yaml, validate_known_keys
from app.beets.setup import read_config_document


def test_parse_yes_no_as_bool() -> None:
    data = parse_yaml("import:\n  autotag: yes\n  copy: no\n")
    assert data["import"]["autotag"] is True
    assert data["import"]["copy"] is False


def test_parse_invalid_yaml_raises() -> None:
    try:
        parse_yaml("this: is: not [valid YAML")
    except YAMLError:
        pass
    else:
        raise AssertionError("expected YAMLError")


def test_validate_returns_empty_on_valid(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    text = (
        f"directory: {music}\n"
        f"library: {tmp_path / 'library.db'}\n"
        "plugins:\n  - musicbrainz\n  - deezer\n"
        "import:\n  autotag: yes\n  copy: yes\n"
    )
    errors = validate_known_keys(parse_yaml(text))
    assert errors == []


def test_validate_returns_loc_with_line_col_for_known_key(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    text = (
        f"directory: {music}\n"
        f"library: {tmp_path / 'library.db'}\n"
        "import:\n"
        "  autotag: yes\n"
        "  copy: maybe\n"
    )
    errors = validate_known_keys(parse_yaml(text))
    assert len(errors) == 1
    assert errors[0].loc == "import.copy"
    assert errors[0].line == 5  # 1-based, the `copy:` line
    assert errors[0].column is not None


def test_validate_returns_line_col_for_invalid_plugin_in_list(tmp_path: Path) -> None:
    """Sequence-index errors (e.g. `plugins[1]` = unknown plugin) must carry
    a line/col so CodeMirror's gutter marker lands on the offending item.
    Regression for the original `_line_col_for_path` which always called
    `parent.lc.value(key)`; on a `CommentedSeq` that raises ``IndexError``
    and the marker silently disappeared. Fix uses `parent.lc.item(idx)` for
    sequence parents."""
    music = tmp_path / "music"
    music.mkdir()
    text = (
        f"directory: {music}\n"
        f"library: {tmp_path / 'library.db'}\n"
        "plugins:\n"
        "  - musicbrainz\n"
        "  - not-a-real-plugin\n"
    )
    errors = validate_known_keys(parse_yaml(text))
    assert any(e.loc == "plugins[1]" and e.line is not None for e in errors)


# (the value as written, what beets' loader reads, what a save writes back)
_SCALARS = [
    ("y", "y", "y"),
    ("N", "N", "N"),
    ("1e400", "1e400", "1e400"),
    ("0e5", "0e5", "0e5"),
    ("+_1_", "+_1_", "+_1_"),
    ("._5", "._5", "._5"),
    ("no", False, "false"),
    ("0644", 420, "0644"),
    ("1.5e+3", 1500.0, "1.5e+3"),
]


@pytest.mark.parametrize("marker", ["", "---\n"], ids=["plain", "document-marker"])
@pytest.mark.parametrize(("written", "read", "saved"), _SCALARS, ids=[row[0] for row in _SCALARS])
def test_a_scalar_reads_and_saves_as_beets_reads_it(
    tmp_path: Path, marker: str, written: str, read: object, saved: str
) -> None:
    """ruamel's own 1.1 table read ``y`` as True, ``1e400`` as a float and
    ``+_1_`` as 1, and a save wrote ``true``, ``.inf``, ``0e0`` and ``1_``."""
    text = f"{marker}k: {written}\n"
    assert pyyaml.load(text, Loader=beets.config.loader) == {"k": read}
    parsed = parse_yaml(text)["k"]
    assert parsed == read
    assert isinstance(parsed, type(read))
    cfg = tmp_path / "config.yaml"

    atomic_write(cfg, parse_yaml(text), _yaml())

    assert cfg.read_text(encoding="utf-8") == f"k: {saved}\n"
    assert read_config_document(cfg) == {"k": read}


def test_the_resolver_table_beets_reads_with_keeps_its_shape() -> None:
    """Tripwire for the table ``_Yaml11Resolver`` copies: PyYAML's, through beets' loader.

    ``None`` must stay absent: ruamel appends that key's list to a first
    character's list IN PLACE (``ruamel/yaml/resolver.py:357-358``), which
    would grow the copy on every matching scalar of one load or dump.
    """
    table = beets.config.loader.yaml_implicit_resolvers
    copied = _yaml().Resolver().versioned_resolver
    assert copied == table
    assert copied is not table
    assert isinstance(table, dict)
    assert None not in table
    assert all(isinstance(first, str) and len(first) <= 1 for first in table)
    pairs = [pair for entries in table.values() for pair in entries]
    assert all(isinstance(tag, str) and isinstance(regex, re.Pattern) for tag, regex in pairs)
    assert {tag for tag, _ in pairs} >= {
        f"tag:yaml.org,2002:{kind}" for kind in ("bool", "int", "float", "null", "timestamp")
    }


def test_a_parse_and_dump_never_change_beets_loader_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PyYAML lets any library register a catch-all (``first=None``) resolver.
    With one registered, a parse and dump used to grow beets' own lists, and
    beets' own load of its default config slowed with every Save.

    A subclass, so the real loader's table is never touched."""

    class CatchAll(beets.config.loader):  # type: ignore[misc,name-defined]  # an untyped attribute
        pass

    never = re.compile(r"(?!)")
    CatchAll.add_implicit_resolver("tag:example.com,2026:never", never, None)
    monkeypatch.setattr(beets.config, "loader", CatchAll)
    before = {first: list(pairs) for first, pairs in CatchAll.yaml_implicit_resolvers.items()}
    text = "directory: /music\nimport: {write: no, copy: yes}\npaths:\n  default: $album/$title\n"

    for _ in range(3):
        atomic_write(tmp_path / "config.yaml", parse_yaml(text), _yaml())

    assert CatchAll.yaml_implicit_resolvers == before
    # ...and the table is still read from beets' loader, not held on our side.
    assert _yaml().Resolver().versioned_resolver[None] == [("tag:example.com,2026:never", never)]
