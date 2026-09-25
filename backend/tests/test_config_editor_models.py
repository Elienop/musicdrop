"""Tests for the Layer-3 config-editor Pydantic models.

Pins the validation behavior of ``KnownKeysSchema`` and the dotted-path
helper ``loc_to_dot_sep``. The schema itself is intentionally narrow: it holds
only MusicDrop's own policies (``directory:``/``library:`` and the match
thresholds); beets' typed reads judge the rest (``app/beets/config_check.py``).
``extra='ignore'`` means every other key passes through silently (per Pydantic
v2 docs Models), and Save writes the submitted text, not this model.
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
    assert schema.directory == music


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


def test_schema_leaves_import_to_beets(tmp_path: Path) -> None:
    """``import.copy: maybe`` is refused by beets' own ``.get(bool)``
    (``app/beets/config_check.py``), in beets' words; the schema has no row."""
    music = tmp_path / "music"
    music.mkdir()
    data = {
        "directory": str(music),
        "library": str(tmp_path / "library.db"),
        "import": {"copy": "maybe"},
    }
    KnownKeysSchema.model_validate(data)


def test_schema_accepts_any_plugin_name(tmp_path: Path) -> None:
    """The 13-name allowlist is gone: beets decides (owner ruling 2026-09-25)."""
    music = tmp_path / "music"
    music.mkdir()
    data = {
        "directory": str(music),
        "library": str(tmp_path / "library.db"),
        "plugins": ["the", "inline", "not-a-plugin"],
    }
    KnownKeysSchema.model_validate(data)


def test_schema_refuses_a_threshold_over_one(tmp_path: Path) -> None:
    """MusicDrop's own policy: beets reads any number (``autotag/match.py:285``)."""
    music = tmp_path / "music"
    music.mkdir()
    data = {
        "directory": str(music),
        "library": str(tmp_path / "library.db"),
        "match": {"strong_rec_thresh": 5.0},
    }
    with pytest.raises(ValidationError) as excinfo:
        KnownKeysSchema.model_validate(data)
    assert [err["loc"] for err in excinfo.value.errors()] == [("match", "strong_rec_thresh")]


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
    # Save writes the submitted text, so ``myplugin`` stays on disk while the
    # schema drops it; we never round-trip through the schema. Pin the drop
    # here so a future ``extra='allow'`` flip is caught.
    assert "myplugin" not in schema.model_dump()
