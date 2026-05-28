"""Tests for the Layer-3 config-editor Pydantic models.

Pins the validation behavior of ``KnownKeysSchema`` and the dotted-path
helper ``loc_to_dot_sep``. The schema itself is intentionally narrow:
``extra='ignore'`` means unknown beets keys / plugin sub-configs pass through
silently (per Pydantic v2 docs Models). We validate the ~13 keys MusicDrop
models; everything else lives on disk in the ruamel ``CommentedMap``.
"""

from pathlib import Path

from pydantic import ValidationError

from app.models.config_editor import (
    KnownKeysSchema,
    loc_to_dot_sep,
)


def test_loc_to_dot_sep_mixed() -> None:
    assert loc_to_dot_sep(("import", "copy")) == "import.copy"
    assert loc_to_dot_sep(("plugins", 2)) == "plugins[2]"
    assert loc_to_dot_sep(()) == ""


def test_schema_accepts_starter_shape(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    data = {
        "directory": str(music),
        "library": str(tmp_path / "library.db"),
        "plugins": ["musicbrainz", "deezer"],
        "import": {"autotag": True, "copy": True, "write": True},
    }
    schema = KnownKeysSchema.model_validate(data)
    assert schema.plugins == ["musicbrainz", "deezer"]


def test_schema_rejects_invalid_bool(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    data = {
        "directory": str(music),
        "library": str(tmp_path / "library.db"),
        "import": {"copy": "maybe"},
    }
    try:
        KnownKeysSchema.model_validate(data)
    except ValidationError as e:
        errors = e.errors()
        assert any(err["loc"] == ("import", "copy") for err in errors)
    else:
        raise AssertionError("expected ValidationError")


def test_schema_rejects_unknown_plugin(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    data = {
        "directory": str(music),
        "library": str(tmp_path / "library.db"),
        "plugins": ["not-a-plugin"],
    }
    try:
        KnownKeysSchema.model_validate(data)
    except ValidationError:
        pass
    else:
        raise AssertionError("expected ValidationError on plugin allowlist")


def test_schema_ignores_unknown_keys(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    data = {
        "directory": str(music),
        "library": str(tmp_path / "library.db"),
        "myplugin": {"weird_key": 42},
    }
    KnownKeysSchema.model_validate(data)  # must not raise
