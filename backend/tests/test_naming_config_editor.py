import hashlib
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.beets.config_editor import read_naming, save_naming
from app.beets.library import LibraryHandle
from app.models.config_editor import (
    NamingRuleInput,
    ReplaceRuleInput,
    SaveNamingRequest,
)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_read_naming_splits_keys(beets_library: LibraryHandle) -> None:
    cfg_path = beets_library.config_path
    cfg_path.write_text(
        "directory: /tmp/music\nlibrary: library.db\n"
        "paths:\n"
        "  default: $albumartist/$album/$track $title\n"
        "  comp: Compilations/$album/$track $title\n"
        "  albumtype:soundtrack: Soundtracks/$album/$title\n"
        "replace:\n"
        "  '[?]': _\n"
    )
    cfg = read_naming(beets_library)
    assert cfg.default == "$albumartist/$album/$track $title"
    assert cfg.comp == "Compilations/$album/$track $title"
    assert cfg.singleton is None
    assert cfg.custom[0].query == "albumtype:soundtrack"
    assert cfg.replace[0].pattern == "[?]" and cfg.replace[0].replacement == "_"
    assert cfg.sha256 == _sha(cfg_path)


def test_save_naming_roundtrip_preserves_other_keys_and_comments(
    beets_library: LibraryHandle,
) -> None:
    cfg_path = beets_library.config_path
    cfg_path.write_text(
        "# my config\n"
        "directory: /tmp/music   # where music lives\n"
        "library: library.db\n"
        "plugins:\n  - musicbrainz\n"
    )
    req = SaveNamingRequest(
        rules=[NamingRuleInput(query="default", template="$albumartist/$album/$track $title")],
        replace=[ReplaceRuleInput(pattern="[?]", replacement="_")],
        base_sha256=_sha(cfg_path),
    )
    save_naming(beets_library, req)
    text = cfg_path.read_text()
    assert "# my config" in text  # comment preserved
    assert "# where music lives" in text  # inline comment preserved
    assert "- musicbrainz" in text  # unrelated key preserved
    assert "default: $albumartist/$album/$track $title" in text
    assert "'[?]': _" in text or "[?]: _" in text


def test_save_naming_empty_rules_drops_paths_key(beets_library: LibraryHandle) -> None:
    cfg_path = beets_library.config_path
    cfg_path.write_text("directory: /tmp/music\nlibrary: library.db\npaths:\n  default: $title\n")
    req = SaveNamingRequest(rules=[], replace=[], base_sha256=_sha(cfg_path))
    save_naming(beets_library, req)
    assert "paths:" not in cfg_path.read_text()


def test_save_naming_409_on_stale_sha(beets_library: LibraryHandle) -> None:
    req = SaveNamingRequest(rules=[], replace=[], base_sha256="stale")
    with pytest.raises(HTTPException) as ei:
        save_naming(beets_library, req)
    assert ei.value.status_code == 409


def test_save_naming_422_on_bad_regex(beets_library: LibraryHandle) -> None:
    cfg_path = beets_library.config_path
    req = SaveNamingRequest(
        rules=[],
        replace=[ReplaceRuleInput(pattern="(", replacement="_")],
        base_sha256=_sha(cfg_path),
    )
    with pytest.raises(HTTPException) as ei:
        save_naming(beets_library, req)
    assert ei.value.status_code == 422
