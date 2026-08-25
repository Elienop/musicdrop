import os
from pathlib import Path

import pytest
from beets.library import Item, Library

from app.beets.naming import (
    _is_legible,
    assemble_rules,
    compile_replacements,
    render_samples,
)
from app.models.config_editor import NamingRuleInput, ReplaceRuleInput
from tests.conftest import build_library


@pytest.fixture
def naming_lib(tmp_path: Path) -> Library:
    """A normal album track, a compilation track, and a singleton."""
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

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
    assert errs[0].index == 0
    assert errs[0].pattern == "("
    assert rendered[0].sample_path == "Adele/25/Hello.flac"


def test_synthetic_fallback_on_empty_library(tmp_path: Path) -> None:
    empty = build_library(str(tmp_path / "e.db"), str(tmp_path / "m"))
    rules = [NamingRuleInput(query="default", template="$albumartist/$album/$track $title")]
    rendered, _ = render_samples(empty, rules=rules, replace=[])
    assert rendered[0].sample_path == "Adele/25/01 Hello.flac"
    assert "built-in" in rendered[0].sample_source


def test_present_but_non_bool_asciify_does_not_error_rows(naming_lib: Library) -> None:
    # beets tolerates a quoted/non-bool ``asciify_paths`` (truthy check); the
    # preview must too — it must NOT raise ConfigTypeError on every row.
    import beets

    beets.config["asciify_paths"] = "true"
    rules = [NamingRuleInput(query="default", template="$albumartist/$album/$track $title")]
    rendered, errs = render_samples(naming_lib, rules=rules, replace=[])
    assert errs == []
    assert rendered[0].error is None
    assert rendered[0].sample_path == "Adele/25/01 Hello.flac"


_PARITY_TMPL = "$albumartist/$album/$track $title"


def _parity_lib(tmp_path: Path, title: str) -> tuple[Library, Item]:
    """A one-album library whose single track carries ``title``."""
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "l.db"), str(music), path_format=_PARITY_TMPL)
    base = music / "raw"
    base.mkdir(parents=True)
    f = base / "x.flac"
    f.write_bytes(b"\x00")
    it = Item(album="Vespertine", albumartist="Björk", artist="Björk", title=title, track=1, disc=1)
    it.path = os.fsencode(str(f))
    lib.add_album([it]).store()
    return lib, it


def _saved_replace_rules() -> list[ReplaceRuleInput]:
    """The saved ``replace:`` map as draft rows, so the preview legalizes against
    the same replacements ``Library.get_replacements`` handed the real item."""
    import beets

    return [
        ReplaceRuleInput(pattern=pattern, replacement=repl or "")
        for pattern, repl in beets.config["replace"].get(dict).items()
    ]


def _destination(lib: Library, item: Item) -> str:
    with lib.music_dir_context():
        return os.fsdecode(item.destination(relative_to_libdir=True))


def test_asciify_preview_matches_beets_destination(tmp_path: Path) -> None:
    # The preview is only worth showing if it renders what an import would
    # actually write, so it must mirror ``Item.destination``. Two SEPARATE
    # separator substitutions are in play: the literal "/" in the title (handled
    # by ``evaluate_template``) and the "/" unidecode INTRODUCES expanding "½"
    # (handled inside ``asciify_path``). Miss the second and the preview sprouts
    # a directory level the import would never create.
    import beets

    beets.config["asciify_paths"] = True
    lib, item = _parity_lib(tmp_path, "Réplica/á ½")
    rules = [NamingRuleInput(query="default", template=_PARITY_TMPL)]

    rendered, errs = render_samples(lib, rules=rules, replace=_saved_replace_rules())

    assert errs == []
    assert rendered[0].error is None
    assert rendered[0].sample_path == _destination(lib, item)
    assert rendered[0].sample_path == "Bjork/Vespertine/01 Replica_a  1_2.flac"


def test_without_asciify_unicode_form_is_untouched(tmp_path: Path) -> None:
    # beets 2.13 normalizes Unicode INSIDE ``asciify_path``; with asciify off it
    # does not normalize at all, so a decomposed title must survive verbatim.
    import beets

    beets.config["asciify_paths"] = False
    decomposed = "Re\u0301plica"  # NFD: "e" + combining acute, NOT a composed "\u00e9"
    lib, item = _parity_lib(tmp_path, decomposed)
    rules = [NamingRuleInput(query="default", template=_PARITY_TMPL)]

    rendered, errs = render_samples(lib, rules=rules, replace=_saved_replace_rules())

    assert errs == []
    assert rendered[0].sample_path == _destination(lib, item)
    assert rendered[0].sample_path == f"Björk/Vespertine/01 {decomposed}.flac"


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


def test_is_legible_prefers_latin() -> None:
    assert _is_legible("Daft Punk")
    assert _is_legible("Sigur Rós")  # mostly Latin
    assert not _is_legible("سلوى القطريب")  # RTL / Arabic
    assert not _is_legible("")


def test_default_prefers_legible_sample_over_rtl(tmp_path: Path) -> None:
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "l.db"), str(music))

    def add(folder: str, fname: str, **f: object) -> Item:
        base = music / folder
        base.mkdir(parents=True, exist_ok=True)
        p = base / fname
        p.write_bytes(b"\x00")
        it = Item(**f)  # type: ignore[arg-type]  # beets Item kwargs are untyped
        it.path = os.fsencode(str(p))
        return it

    # An RTL album whose leading "(" sorts it first (the naive pick) + a Latin one.
    rtl = add(
        "rtl",
        "01 a.flac",
        album="(٢٠) ألبوم",
        albumartist="(سلوى)",
        artist="(سلوى)",
        title="أغنية",
        track=1,
        disc=1,
    )
    lib.add_album([rtl]).store()
    latin = add(
        "Daft Punk/Discovery",
        "01 One More Time.flac",
        album="Discovery",
        albumartist="Daft Punk",
        artist="Daft Punk",
        title="One More Time",
        track=1,
        disc=1,
    )
    lib.add_album([latin]).store()

    rules = [NamingRuleInput(query="default", template="$albumartist/$album/$track $title")]
    rendered, _ = render_samples(lib, rules=rules, replace=[])
    assert rendered[0].sample_path.startswith("Daft Punk/")
    assert "Daft Punk" in rendered[0].sample_source


def test_assemble_rules_orders_known_then_custom() -> None:
    rules = assemble_rules(
        default="$d",
        comp=None,
        singleton="$s",
        custom=[NamingRuleInput(query="albumtype:soundtrack", template="$x")],
    )
    assert [r.query for r in rules] == ["default", "singleton", "albumtype:soundtrack"]
