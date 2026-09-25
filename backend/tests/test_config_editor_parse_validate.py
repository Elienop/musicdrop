"""Layer-3 config editor — parse + check (line/col mapping) tests.

Two parsers, two jobs:

* beets' own loader reads what Validate and Save check
  (:func:`app.beets.config_check.load_config_text`): a reused anchor is refused
  there, as it is at boot, and a falsy top level is no settings.
* ruamel parses and dumps only where the Naming save edits a file in place, and
  reads `yes`/`no` as bool, as YAML 1.1 and beets' PyYAML do (`_Yaml11Resolver`).

Rows carry a 1-based `(line, column)` from the text composed with beets' loader,
so CodeMirror's lint gutter can render the marker.
"""

from __future__ import annotations

import re
from pathlib import Path

import beets
import confuse
import pytest
import yaml as pyyaml

# NOTE: plan-verbatim used `from ruamel.yaml import YAMLError`, but the
# ruamel.yaml stubs only expose YAMLError from `ruamel.yaml.error` (its
# canonical home). Same class object at runtime — verified via identity.
# Using the canonical path keeps mypy --strict clean without a scoped
# suppression directive.
from ruamel.yaml.error import YAMLError

from app.beets.config_check import NotAMapping, check_config_text, load_config_text
from app.beets.config_editor import _yaml, atomic_write, dumped, parse_yaml
from app.beets.setup import read_config_document
from app.config import Settings
from app.models.config_editor import ValidationErrorItem


def _check(text: str) -> list[ValidationErrorItem]:
    """The rows Validate and Save give ``text``; no handle, so no store-layout rows."""
    return check_config_text(text, settings=Settings(), handle=None).errors


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


_REUSED_ANCHOR = "a: &x 1\nb: *x\nplex:\n  token: &x Zq7Secret\n  user: &x u\n"


def test_the_beets_loader_refuses_a_reused_anchor(recwarn: pytest.WarningsRecorder) -> None:
    """beets' PyYAML refuses the file; ruamel only warned, quoting both lines,
    which is why Validate and Save no longer read the text with ruamel.

    An anchor reused AFTER its alias still refuses. The mark is on ``reason``.
    """
    with pytest.raises(confuse.ConfigReadError) as caught:
        load_config_text(_REUSED_ANCHOR)

    reason = caught.value.reason
    assert isinstance(reason, pyyaml.MarkedYAMLError)
    assert (reason.problem, reason.problem_mark.line + 1) == ("second occurrence", 4)
    assert recwarn.list == []


def test_parse_keeps_anchors_and_aliases_that_are_not_reused() -> None:
    """The control: one anchor per name, aliased and merged."""
    text = "a: &x 1\nb: *x\nc: &m {k: v}\nd:\n  <<: *m\n"

    assert parse_yaml(text) == {"a": 1, "b": 1, "c": {"k": "v"}, "d": {"k": "v"}}


@pytest.mark.parametrize("text", ["", "# only a comment\n", "~\n", "[]\n", "false\n", "---\n...\n"])
def test_a_falsy_top_level_is_no_settings_as_beets_reads_it(text: str) -> None:
    """``load_yaml(...) or {}`` (``confuse/sources.py:101``): beets starts on its
    defaults, so only MusicDrop's own required keys refuse these."""
    assert load_config_text(text) == {}
    assert [(row.loc, row.msg) for row in _check(text)] == [
        ("directory", "Field required"),
        ("library", "Field required"),
    ]


@pytest.mark.parametrize("text", ["- a\n", "hello\n", "5\n"])
def test_a_list_or_a_scalar_top_level_is_refused_in_our_words(text: str) -> None:
    """``YamlSource`` raises a ``TypeError`` naming ``<class 'list'>``
    (``confuse/sources.py:103-107``); the row says it without the class."""
    with pytest.raises(NotAMapping):
        load_config_text(text)
    assert [row.model_dump() for row in _check(text)] == [
        {
            "loc": "",
            "msg": "config.yaml must be a mapping of settings.",
            "type": "model_type",
            "line": None,
            "column": None,
        }
    ]


def test_check_returns_empty_on_valid(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    text = (
        f"directory: {music}\n"
        f"library: {tmp_path / 'library.db'}\n"
        "plugins:\n  - musicbrainz\n  - deezer\n"
        "import:\n  autotag: yes\n  copy: yes\n"
    )
    assert _check(text) == []


def test_check_returns_loc_with_line_col_for_known_key(tmp_path: Path) -> None:
    """confuse's sentence, printed once: ``loc: msg`` reads ``import.copy: must be…``."""
    music = tmp_path / "music"
    music.mkdir()
    text = (
        f"directory: {music}\n"
        f"library: {tmp_path / 'library.db'}\n"
        "import:\n"
        "  autotag: yes\n"
        "  copy: maybe\n"
    )
    assert [row.model_dump() for row in _check(text)] == [
        {
            "loc": "import.copy",
            "msg": "must be a bool, not str",
            "type": "beets_read",
            "line": 5,  # 1-based, the `copy:` line
            "column": 8,  # 0-based, the value
        }
    ]


def test_any_plugin_name_is_clean_because_beets_decides(tmp_path: Path) -> None:
    """The 13-name allowlist is gone (owner ruling 2026-09-25): ``the`` and
    ``inline`` are real beets plugins it refused, and a name beets cannot load
    is one beets skips at start-up."""
    music = tmp_path / "music"
    music.mkdir()
    text = (
        f"directory: {music}\n"
        f"library: {tmp_path / 'library.db'}\n"
        "plugins:\n"
        "  - musicbrainz\n"
        "  - the\n"
        "  - inline\n"
        "  - not-a-real-plugin\n"
    )
    assert _check(text) == []


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

    atomic_write(cfg, dumped(parse_yaml(text), _yaml()))

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
        atomic_write(tmp_path / "config.yaml", dumped(parse_yaml(text), _yaml()))

    assert CatchAll.yaml_implicit_resolvers == before
    # ...and the table is still read from beets' loader, not held on our side.
    assert _yaml().Resolver().versioned_resolver[None] == [("tag:example.com,2026:never", never)]
