import hashlib
from pathlib import Path

import beets
import pytest
from fastapi import HTTPException

from app.beets.config_editor import (
    _beets_default_naming,
    read_naming,
    save,
    save_naming,
)
from app.beets.library import LibraryHandle
from app.models.config_editor import (
    NamingRuleInput,
    ReplaceRuleInput,
    SaveNamingRequest,
    SaveRequest,
)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_config_saves_hold_the_save_lock_during_write(
    beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """save/save_naming must run their CAS read->write under _SAVE_LOCK so a
    concurrent save can't pass the same-base check and clobber the other."""
    import app.beets.config_editor as ce

    cfg_path = beets_library.config_path
    cfg_path.write_text("directory: /tmp/music\nlibrary: library.db\n")
    orig = ce.atomic_write
    locked_during: list[bool] = []

    def spy(*args: object, **kwargs: object) -> None:
        locked_during.append(ce._SAVE_LOCK.locked())
        orig(*args, **kwargs)  # type: ignore[arg-type]  # transparent spy passthrough

    monkeypatch.setattr(ce, "atomic_write", spy)

    save_naming(beets_library, SaveNamingRequest(rules=[], replace=[], base_sha256=_sha(cfg_path)))
    save(
        beets_library,
        SaveRequest(yaml_text=cfg_path.read_text() + "\n# x\n", base_sha256=_sha(cfg_path)),
    )
    assert locked_during == [True, True]  # both writes ran under the lock


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
    # singleton has no explicit override -> falls back to the bundled default
    # (paths is merged per-key in beets).
    assert cfg.singleton == "Non-Album/$artist/$title"
    assert cfg.custom[0].query == "albumtype:soundtrack"
    # An explicit replace: block wholly replaces beets' defaults (no merge).
    assert [(r.pattern, r.replacement) for r in cfg.replace] == [("[?]", "_")]
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


def test_read_naming_tolerates_non_mapping_paths(beets_library: LibraryHandle) -> None:
    # A hand-corrupted config (paths: scalar) must not 500 — coerce to empty,
    # then the per-key fallback fills in beets' bundled defaults.
    cfg_path = beets_library.config_path
    cfg_path.write_text("directory: /tmp/music\nlibrary: library.db\npaths: just-a-string\n")
    cfg = read_naming(beets_library)
    assert cfg.custom == []
    assert cfg.default == "$albumartist/$album%aunique{}/$track $title"
    assert cfg.comp == "Compilations/$album%aunique{}/$track $title"
    assert cfg.singleton == "Non-Album/$artist/$title"


def test_read_naming_falls_back_to_bundled_defaults(beets_library: LibraryHandle) -> None:
    # The common case: a config with no paths:/replace: blocks at all. The panel
    # must pre-fill with beets' effective built-in naming, not blanks.
    cfg_path = beets_library.config_path
    cfg_path.write_text("directory: /tmp/music\nlibrary: library.db\n")
    cfg = read_naming(beets_library)
    assert cfg.default == "$albumartist/$album%aunique{}/$track $title"
    assert cfg.comp == "Compilations/$album%aunique{}/$track $title"
    assert cfg.singleton == "Non-Album/$artist/$title"
    assert cfg.custom == []
    rows = {r.pattern: r.replacement for r in cfg.replace}
    assert len(cfg.replace) == 9
    assert rows["^-"] == "_"
    assert rows["\\s+$"] == ""  # the whitespace strippers replace with nothing
    # The CAS sha stays the on-disk bytes hash — the default file is never
    # folded in, or the Save round-trip would break.
    assert cfg.sha256 == _sha(cfg_path)


def test_read_naming_per_key_path_fallback(beets_library: LibraryHandle) -> None:
    # An explicit default only -> comp/singleton fall back to bundled defaults.
    cfg_path = beets_library.config_path
    cfg_path.write_text(
        "directory: /tmp/music\nlibrary: library.db\n"
        "paths:\n"
        "  default: $albumartist/$album/$track. $title\n"
    )
    cfg = read_naming(beets_library)
    assert cfg.default == "$albumartist/$album/$track. $title"
    assert cfg.comp == "Compilations/$album%aunique{}/$track $title"
    assert cfg.singleton == "Non-Album/$artist/$title"


def test_read_naming_explicit_replace_suppresses_defaults(
    beets_library: LibraryHandle,
) -> None:
    # A present replace: block WHOLLY replaces beets' defaults (no merge).
    cfg_path = beets_library.config_path
    cfg_path.write_text("directory: /tmp/music\nlibrary: library.db\nreplace:\n  '[?]': _\n")
    cfg = read_naming(beets_library)
    assert [(r.pattern, r.replacement) for r in cfg.replace] == [("[?]", "_")]
    # Path keys still fall back since there is no paths: block.
    assert cfg.default == "$albumartist/$album%aunique{}/$track $title"


def test_beets_default_naming_known_values_and_missing_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, replace = _beets_default_naming()
    assert paths["default"] == "$albumartist/$album%aunique{}/$track $title"
    assert paths["comp"] == "Compilations/$album%aunique{}/$track $title"
    assert paths["singleton"] == "Non-Album/$artist/$title"
    assert len(replace) == 9
    # Degrades to empty dicts when the bundled file can't be read, so
    # read_naming never 500s.
    monkeypatch.setattr(beets, "__file__", "/no/such/dir/beets/__init__.py")
    assert _beets_default_naming() == ({}, {})


def test_save_naming_skips_empty_query_custom_row(beets_library: LibraryHandle) -> None:
    cfg_path = beets_library.config_path
    cfg_path.write_text("directory: /tmp/music\nlibrary: library.db\n")
    req = SaveNamingRequest(
        rules=[NamingRuleInput(query="", template="$title")],
        replace=[],
        base_sha256=_sha(cfg_path),
    )
    save_naming(beets_library, req)
    text = cfg_path.read_text()
    assert "''" not in text  # no stray empty-query key written
    assert "paths:" not in text  # the empty-query row was the only rule -> key dropped
