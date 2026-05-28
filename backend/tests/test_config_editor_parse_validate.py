"""Layer-3 config editor — parse + validate (line/col mapping) tests.

Per the Layer-3 plan (Task 2), these tests pin three load-bearing decisions:

* ruamel parses `yes`/`no` as bool when `yaml.version = (1, 1)` (YAML 1.1
  spec; ruamel SF #285) — verified by `test_parse_yes_no_as_bool`.
* invalid YAML surfaces as a `ruamel.yaml.YAMLError` for the caller to map
  to HTTP 422 — verified by `test_parse_invalid_yaml_raises`.
* schema errors carry a 1-based `(line, column)` derived from ruamel's
  `.lc.value(...)` so CodeMirror's lint gutter can render the marker —
  verified by `test_validate_returns_loc_with_line_col_for_known_key`.
"""

from __future__ import annotations

from pathlib import Path

from app.beets.config_editor import parse_yaml, validate_known_keys


def test_parse_yes_no_as_bool() -> None:
    data = parse_yaml("import:\n  autotag: yes\n  copy: no\n")
    assert data["import"]["autotag"] is True
    assert data["import"]["copy"] is False


def test_parse_invalid_yaml_raises() -> None:
    # NOTE: plan-verbatim used `from ruamel.yaml import YAMLError`, but the
    # ruamel.yaml stubs only expose YAMLError from `ruamel.yaml.error` (its
    # canonical home). Same class object at runtime — verified equal via
    # identity. Using the canonical path keeps mypy --strict clean without
    # a scoped type: ignore.
    from ruamel.yaml.error import YAMLError

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
    assert errors[0].column is not None and errors[0].column >= 0
