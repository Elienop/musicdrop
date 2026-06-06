import os
from pathlib import Path

import pytest
from beets.library import Item, Library

from app.beets.naming import assemble_rules, compile_replacements, render_samples
from app.models.config_editor import NamingRuleInput, ReplaceRuleInput


@pytest.fixture
def naming_lib(tmp_path: Path) -> Library:
    """A normal album track, a compilation track, and a singleton."""
    music = tmp_path / "music"
    lib = Library(
        str(tmp_path / "library.db"),
        directory=str(music),
        path_formats=[("default", "$albumartist/$album/$track $title")],
    )

    def add(folder: str, fname: str, **fields: object) -> Item:
        base = music / folder
        base.mkdir(parents=True, exist_ok=True)
        f = base / fname
        f.write_bytes(b"\x00")
        it = Item(**fields)  # type: ignore[arg-type]  # **fields are flex tag attrs, not the lib positional
        it.path = os.fsencode(str(f))
        return it

    a = add(
        "Adele/25",
        "01 Hello.flac",
        album="25",
        albumartist="Adele",
        artist="Adele",
        title="Hello",
        track=1,
        disc=1,
    )
    lib.add_album([a]).store()
    c = add(
        "VA/Now",
        "04 Song.flac",
        album="Now 100",
        albumartist="Various Artists",
        artist="Some One",
        title="Song",
        track=4,
        disc=1,
        comp=True,
    )
    lib.add_album([c]).store()
    s = add("loose", "z.flac", albumartist="Moby", artist="Moby", title="Porcelain", track=0)
    lib.add(s)
    return lib


def test_default_renders_real_album_track(naming_lib: Library) -> None:
    rules = [NamingRuleInput(query="default", template="$albumartist/$album/$track $title")]
    rendered, errs = render_samples(naming_lib, rules=rules, replace=[])
    assert errs == []
    assert rendered[0].sample_path == "Adele/25/01 Hello.flac"
    assert "Adele" in rendered[0].sample_source


def test_comp_and_singleton_pick_fitting_samples(naming_lib: Library) -> None:
    rules = [
        NamingRuleInput(query="comp", template="Compilations/$album/$track $title"),
        NamingRuleInput(query="singleton", template="Singletons/$artist - $title"),
    ]
    rendered, _ = render_samples(naming_lib, rules=rules, replace=[])
    assert rendered[0].sample_path == "Compilations/Now 100/04 Song.flac"
    assert rendered[1].sample_path == "Singletons/Moby - Porcelain.flac"


def test_draft_replace_is_applied(naming_lib: Library) -> None:
    rules = [NamingRuleInput(query="default", template="$albumartist/$album/$title")]
    rendered, errs = render_samples(
        naming_lib, rules=rules, replace=[ReplaceRuleInput(pattern="l", replacement="L")]
    )
    assert errs == []
    assert rendered[0].sample_path == "AdeLe/25/HeLLo.flac"


def test_bad_replace_regex_reported_and_excluded(naming_lib: Library) -> None:
    rules = [NamingRuleInput(query="default", template="$albumartist/$album/$title")]
    rendered, errs = render_samples(
        naming_lib, rules=rules, replace=[ReplaceRuleInput(pattern="(", replacement="_")]
    )
    assert len(errs) == 1
    assert errs[0].index == 0 and errs[0].pattern == "("
    assert rendered[0].sample_path == "Adele/25/Hello.flac"


def test_synthetic_fallback_on_empty_library(tmp_path: Path) -> None:
    empty = Library(
        str(tmp_path / "e.db"),
        directory=str(tmp_path / "m"),
        path_formats=[("default", "$albumartist/$album/$track $title")],
    )
    rules = [NamingRuleInput(query="default", template="$albumartist/$album/$track $title")]
    rendered, _ = render_samples(empty, rules=rules, replace=[])
    assert rendered[0].sample_path == "Adele/25/01 Hello.flac"
    assert "built-in" in rendered[0].sample_source


def test_compile_replacements_splits_valid_and_bad() -> None:
    valid, errs = compile_replacements(
        [
            ReplaceRuleInput(pattern="[?]", replacement="_"),
            ReplaceRuleInput(pattern="(", replacement="x"),
            ReplaceRuleInput(pattern="", replacement="ignored"),
        ]
    )
    assert len(valid) == 1  # empty pattern skipped, "(" errored
    assert [e.index for e in errs] == [1]


def test_assemble_rules_orders_known_then_custom() -> None:
    rules = assemble_rules(
        default="$d",
        comp=None,
        singleton="$s",
        custom=[NamingRuleInput(query="albumtype:soundtrack", template="$x")],
    )
    assert [r.query for r in rules] == ["default", "singleton", "albumtype:soundtrack"]
