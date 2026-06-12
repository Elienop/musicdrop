"""Tests for the Layer-3 config-editor Pydantic models.

Pins the validation behavior of ``KnownKeysSchema`` and the dotted-path
helper ``loc_to_dot_sep``. The schema itself is intentionally narrow:
``extra='ignore'`` means unknown beets keys / plugin sub-configs pass through
silently (per Pydantic v2 docs Models). We validate the ~13 keys MusicDrop
models; everything else lives on disk in the ruamel ``CommentedMap``.
"""

from pathlib import Path

import pytest
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


def test_schema_accepts_existing_dir_with_readonly_parent(tmp_path: Path) -> None:
    """The Docker norm: the library is a volume mounted at the root (/library),
    whose parent (/) is never writable by the app user. An existing writable
    directory must validate on its own merits, not its parent's."""
    parent = tmp_path / "root"
    music = parent / "library"
    music.mkdir(parents=True)
    parent.chmod(0o555)
    try:
        data = {
            "directory": str(music),
            "library": str(tmp_path / "library.db"),
        }
        schema = KnownKeysSchema.model_validate(data)
        assert schema.directory == music
    finally:
        parent.chmod(0o755)


def test_schema_rejects_unwritable_existing_dir(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    music.chmod(0o555)
    try:
        data = {
            "directory": str(music),
            "library": str(tmp_path / "library.db"),
        }
        with pytest.raises(ValidationError) as excinfo:
            KnownKeysSchema.model_validate(data)
        assert any(err["loc"] == ("directory",) for err in excinfo.value.errors())
    finally:
        music.chmod(0o755)


def test_schema_rejects_missing_dir_under_readonly_parent(tmp_path: Path) -> None:
    """When the directory doesn't exist yet, the parent must be writable so
    beets can create it — the original rule, still enforced."""
    parent = tmp_path / "root"
    parent.mkdir()
    parent.chmod(0o555)
    try:
        data = {
            "directory": str(parent / "newdir"),
            "library": str(tmp_path / "library.db"),
        }
        with pytest.raises(ValidationError) as excinfo:
            KnownKeysSchema.model_validate(data)
        assert any(err["loc"] == ("directory",) for err in excinfo.value.errors())
    finally:
        parent.chmod(0o755)


def test_schema_rejects_file_at_directory_path(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "music"
    not_a_dir.write_text("oops")
    data = {
        "directory": str(not_a_dir),
        "library": str(tmp_path / "library.db"),
    }
    with pytest.raises(ValidationError) as excinfo:
        KnownKeysSchema.model_validate(data)
    assert any(err["loc"] == ("directory",) for err in excinfo.value.errors())


def test_schema_rejects_invalid_bool(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    data = {
        "directory": str(music),
        "library": str(tmp_path / "library.db"),
        "import": {"copy": "maybe"},
    }
    with pytest.raises(ValidationError) as excinfo:
        KnownKeysSchema.model_validate(data)
    assert any(err["loc"] == ("import", "copy") for err in excinfo.value.errors())


def test_schema_rejects_unknown_plugin(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    data = {
        "directory": str(music),
        "library": str(tmp_path / "library.db"),
        "plugins": ["not-a-plugin"],
    }
    with pytest.raises(ValidationError) as excinfo:
        KnownKeysSchema.model_validate(data)
    # Pin the location too: a future regression that accepts "not-a-plugin" by
    # dropping it silently would otherwise still satisfy a bare ``raises``.
    assert any(err["loc"][:1] == ("plugins",) for err in excinfo.value.errors())


def test_schema_ignores_unknown_keys(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    data = {
        "directory": str(music),
        "library": str(tmp_path / "library.db"),
        "myplugin": {"weird_key": 42},
    }
    schema = KnownKeysSchema.model_validate(data)
    # Pydantic v2 ``extra='ignore'`` (default) DROPS unknown keys at validation.
    # The save flow relies on this: the ruamel CommentedMap on disk keeps
    # ``myplugin``, the schema doesn't, and we never round-trip through the
    # schema. Pin the drop here so a future ``extra='allow'`` flip is caught.
    assert "myplugin" not in schema.model_dump()
