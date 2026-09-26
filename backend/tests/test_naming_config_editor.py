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
from app.config import Settings
from app.models.config_editor import (
    NamingRuleInput,
    ReplaceRuleInput,
    SaveNamingRequest,
    SaveRequest,
)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_config_saves_hold_the_save_lock_during_write(
    beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """save/save_naming must run their CAS read->write under _SAVE_LOCK so a
    concurrent save can't pass the same-base check and clobber the other."""
    import app.beets.config_editor as ce

    cfg_path = beets_library.config_path
    cfg_path.write_text(f"directory: {tmp_path / 'music'}\nlibrary: library.db\n")
    orig = ce.atomic_write
    locked_during: list[bool] = []

    def spy(*args: object, **kwargs: object) -> None:
        locked_during.append(ce._SAVE_LOCK.locked())
        orig(*args, **kwargs)  # type: ignore[arg-type]  # transparent spy passthrough

    monkeypatch.setattr(ce, "atomic_write", spy)

    save_naming(
        beets_library,
        SaveNamingRequest(rules=[], replace=[], base_sha256=_sha(cfg_path)),
        settings=Settings(),
    )
    save(
        beets_library,
        SaveRequest(yaml_text=cfg_path.read_text() + "\n# x\n", base_sha256=_sha(cfg_path)),
        # Default settings: trash/origins resolve under the fixture's own beets
        # dir, which holds no music, so the containment check passes and this
        # test keeps asking only about the lock.
        settings=Settings(),
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
    save_naming(beets_library, req, settings=Settings())
    text = cfg_path.read_text()
    assert "# my config" in text  # comment preserved
    assert "# where music lives" in text  # inline comment preserved
    assert "- musicbrainz" in text  # unrelated key preserved
    assert "default: $albumartist/$album/$track $title" in text
    assert "'[?]': _" in text or "[?]: _" in text


def test_save_naming_keeps_boolean_token_replace_rules_as_strings(
    beets_library: LibraryHandle,
) -> None:
    # Regression: save_naming builds a FRESH CommentedMap of plain-str values, so
    # their quoting is decided by the dumper's YAML-version resolver, NOT by
    # preserve_quotes. A bool-token pattern/replacement ("no"/"off") must be
    # emitted QUOTED so it reloads as a string. If the dumper used the 1.2 resolver
    # (what dropping both ``_Yaml11Resolver`` and ``yaml.version`` does; either
    # alone still quotes them), these dump BARE, then confuse/PyYAML
    # re-type them to bool — beets' re.compile(False) crashes on Apply for a bool
    # KEY, and a bool VALUE is silently dropped (`repl or ''`). "y" is a bool only
    # in ruamel's own 1.1 table: beets reads it bare as a string.
    import yaml as pyyaml

    cfg_path = beets_library.config_path
    cfg_path.write_text("directory: /tmp/music\nlibrary: library.db\n")
    req = SaveNamingRequest(
        rules=[NamingRuleInput(query="default", template="$title")],
        replace=[
            ReplaceRuleInput(pattern="no", replacement="_"),  # bool-token KEY
            ReplaceRuleInput(pattern="[<>]", replacement="off"),  # bool-token VALUE
            ReplaceRuleInput(pattern="ñ", replacement="y"),  # a bool in ruamel's table only
        ],
        base_sha256=_sha(cfg_path),
    )
    save_naming(beets_library, req, settings=Settings())
    text = cfg_path.read_text()

    assert "%YAML" not in text  # directive prologue stripped (M5)
    # read_naming (beets' own loader) reads them back as strings, not bool tokens.
    rules = [(r.pattern, r.replacement) for r in read_naming(beets_library).replace]
    assert rules == [("no", "_"), ("[<>]", "off"), ("ñ", "y")]
    # confuse/beets load path is PyYAML (YAML 1.1): keys AND values must be str —
    # a bare token would parse as bool and break re.compile / drop the replacement.
    loaded = pyyaml.safe_load(text)["replace"]
    assert loaded == {"no": "_", "[<>]": "off", "ñ": "y"}
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in loaded.items())


def test_save_naming_empty_rules_drops_paths_key(beets_library: LibraryHandle) -> None:
    cfg_path = beets_library.config_path
    cfg_path.write_text("directory: /tmp/music\nlibrary: library.db\npaths:\n  default: $title\n")
    req = SaveNamingRequest(rules=[], replace=[], base_sha256=_sha(cfg_path))
    save_naming(beets_library, req, settings=Settings())
    assert "paths:" not in cfg_path.read_text()


def test_save_naming_409_on_stale_sha(beets_library: LibraryHandle) -> None:
    req = SaveNamingRequest(rules=[], replace=[], base_sha256="stale")
    settings = Settings()
    with pytest.raises(HTTPException) as ei:
        save_naming(beets_library, req, settings=settings)
    assert ei.value.status_code == 409


def test_save_naming_422_on_bad_regex(beets_library: LibraryHandle) -> None:
    cfg_path = beets_library.config_path
    req = SaveNamingRequest(
        rules=[],
        replace=[ReplaceRuleInput(pattern="(", replacement="_")],
        base_sha256=_sha(cfg_path),
    )
    settings = Settings()
    with pytest.raises(HTTPException) as ei:
        save_naming(beets_library, req, settings=settings)
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
    save_naming(beets_library, req, settings=Settings())
    text = cfg_path.read_text()
    assert "''" not in text  # no stray empty-query key written
    assert "paths:" not in text  # the empty-query row was the only rule -> key dropped


# The old starter config's replace: block: five typographic rules and nothing else,
# so installs made with it lack beets' own rules, '[\\/]': _ among them.
OLD_STARTER_REPLACE = (
    "replace:\n"
    "  '[\\u2010\\u2011\\u2212]': '-'\n"
    "  '[\\u2013\\u2014]': '-'\n"
    "  '[\\u2018\\u2019\\u02bc]': \"'\"\n"
    "  '[\\u201c\\u201d]': '_'\n"
    "  '\\u2026': '...'\n"
)


def beets_file_replace_order() -> list[tuple[str, str]]:
    """beets' ``replace:`` rows in the order the INSTALLED file lists them.

    Read from PyYAML's node graph (``yaml.compose``), a layer below the
    constructor ``_beets_default_naming`` reads with, so this is an independent
    oracle for order and not the code under test read a second time.
    """
    import yaml as pyyaml

    text = (Path(beets.__file__).parent / "config_default.yaml").read_text(encoding="utf-8")
    root = pyyaml.compose(text)
    assert isinstance(root, pyyaml.MappingNode)
    block = next(v for k, v in root.value if k.value == "replace")
    assert isinstance(block, pyyaml.MappingNode)
    return [(str(k.value), str(v.value)) for k, v in block.value]


def test_the_beets_order_oracle_can_tell_a_scrambled_order() -> None:
    """Control: the order checks below prove something only if beets' file order
    is neither sorted nor a palindrome, so a sorted or reversed list differs."""
    order = beets_file_replace_order()
    assert order[0] == ("[<>:\\?\\*\\|]", "_")
    assert order[-1] == ("^\\s+", "")
    assert ("[\\\\/]", "_") in order  # the path-separator rule the old starter lost
    assert order != sorted(order)
    assert order != list(reversed(order))


def test_read_naming_sends_beets_replace_in_beets_order_beside_an_explicit_block(
    beets_library: LibraryHandle,
) -> None:
    beets_library.config_path.write_text(
        "directory: /tmp/music\nlibrary: library.db\n" + OLD_STARTER_REPLACE,
        encoding="utf-8",
    )

    cfg = read_naming(beets_library)

    assert [(r.pattern, r.replacement) for r in cfg.beets_replace] == beets_file_replace_order()
    # replace is unchanged: the explicit rows verbatim, none of beets' own.
    assert [(r.pattern, r.replacement) for r in cfg.replace] == [
        ("[\\u2010\\u2011\\u2212]", "-"),
        ("[\\u2013\\u2014]", "-"),
        ("[\\u2018\\u2019\\u02bc]", "'"),
        ("[\\u201c\\u201d]", "_"),
        ("\\u2026", "..."),
    ]


def test_read_naming_sends_beets_replace_when_config_has_no_replace_block(
    beets_library: LibraryHandle,
) -> None:
    beets_library.config_path.write_text(
        "directory: /tmp/music\nlibrary: library.db\n", encoding="utf-8"
    )

    cfg = read_naming(beets_library)

    order = beets_file_replace_order()
    assert [(r.pattern, r.replacement) for r in cfg.beets_replace] == order
    # replace is unchanged: with no block it shows beets' defaults, as before.
    assert [(r.pattern, r.replacement) for r in cfg.replace] == order


def test_read_naming_sends_no_beets_replace_when_beets_defaults_cannot_be_read(
    beets_library: LibraryHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    beets_library.config_path.write_text(
        "directory: /tmp/music\nlibrary: library.db\n" + OLD_STARTER_REPLACE,
        encoding="utf-8",
    )
    monkeypatch.setattr(beets, "__file__", "/no/such/dir/beets/__init__.py")

    cfg = read_naming(beets_library)

    assert cfg.beets_replace == []
    assert len(cfg.replace) == 5  # the explicit rows still read
