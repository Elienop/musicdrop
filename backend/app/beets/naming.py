"""Pure preview renderer for the Naming panel (re-apply ``paths:``/``replace:``).

All beets access for the feature lives here + ``config_editor.py`` (CLAUDE.md
rule 3). Rendering is PURE — it never mutates ``beets.config`` or the library —
so it is concurrency-safe against Reorganize previews and any other reads.

Each rule previews its OWN template directly (``Item.evaluate_template`` +
``util.legalize_path``), against an auto-picked sample item that fits the rule's
query, falling back to a built-in synthetic ``Item`` on an empty / non-matching
library. ``functemplate`` is lenient (a malformed template does not raise); the
only hard failure is a malformed ``replace:`` regex, reported per-row and
excluded from the render so paths still preview.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from typing import Any

import beets
import confuse
from beets import util
from beets.library import Item
from beets.util.functemplate import template

from app.models.config_editor import (
    NamingRuleInput,
    RenderedRule,
    ReplaceError,
    ReplaceRuleInput,
)

#: Map the three well-known path keys to the beets query that selects a fitting
#: sample. A custom query (anything else) passes through unchanged.
_SAMPLE_QUERY = {"comp": "comp:true", "singleton": "singleton:true"}


def assemble_rules(
    *,
    default: str | None,
    comp: str | None,
    singleton: str | None,
    custom: list[NamingRuleInput],
) -> list[NamingRuleInput]:
    """Flatten the panel's split fields into the ordered rule list the renderer +
    save path consume: default, comp, singleton (when set), then custom rows."""
    rules: list[NamingRuleInput] = []
    if default is not None:
        rules.append(NamingRuleInput(query="default", template=default))
    if comp is not None:
        rules.append(NamingRuleInput(query="comp", template=comp))
    if singleton is not None:
        rules.append(NamingRuleInput(query="singleton", template=singleton))
    rules.extend(custom)
    return rules


def compile_replacements(
    replace: list[ReplaceRuleInput],
) -> tuple[list[tuple[re.Pattern[str], str]], list[ReplaceError]]:
    """Compile draft replace rows like beets' ``get_replacements()`` does.

    Empty-pattern rows are skipped. A row whose pattern fails ``re.compile`` is
    collected as a :class:`ReplaceError` (and omitted from the compiled list) so
    one bad regex never sinks the whole preview.
    """
    compiled: list[tuple[re.Pattern[str], str]] = []
    errors: list[ReplaceError] = []
    for i, row in enumerate(replace):
        if not row.pattern:
            continue
        try:
            compiled.append((re.compile(row.pattern), row.replacement or ""))
        except re.error as exc:
            errors.append(ReplaceError(index=i, pattern=row.pattern, message=str(exc)))
    return compiled, errors


def _synthetic_item(query: str) -> tuple[Any, str]:
    """A detached built-in sample for an empty / non-matching library. Detached
    items render fine; ``%aunique{}`` returns ``""`` (no db)."""
    if _SAMPLE_QUERY.get(query, query) == "comp:true":
        it = Item(
            album="Now 100",
            albumartist="Various Artists",
            artist="Some One",
            title="Song",
            track=4,
            disc=1,
            comp=True,
        )
        return it, "built-in compilation sample"
    if _SAMPLE_QUERY.get(query, query) == "singleton:true":
        it = Item(albumartist="Moby", artist="Moby", title="Porcelain", track=0)
        return it, "built-in singleton sample"
    it = Item(album="25", albumartist="Adele", artist="Adele", title="Hello", track=1, disc=1)
    return it, "built-in sample"


def _pick_sample(lib: Any, query: str) -> tuple[Any, str]:
    """First library item fitting the rule's query, else a synthetic sample.

    ``default`` -> first item that belongs to an album (``album_id`` set) and is
    not a compilation; ``comp``/``singleton`` -> the mapped beets query; a custom
    query -> itself.
    """
    sel = _SAMPLE_QUERY.get(query, query)
    try:
        if query == "default":
            for it in lib.items():
                if it.album_id is not None and not it.comp:
                    return it, f"{it.albumartist or it.artist} — {it.album}"
        else:
            for it in lib.items(sel):
                artist = it.albumartist or it.artist or "Unknown"
                return it, f"{artist} — {it.album or it.title}"
    except Exception:
        # A malformed custom query string (beets ParsingError) -> synthetic.
        pass
    return _synthetic_item(query)


def _render_one(item: Any, tmpl: str, replacements: list[tuple[re.Pattern[str], str]]) -> str:
    """Render one template against ``item`` -> a legalized relative path string.

    Mirrors the tail of ``Item.destination``: evaluate template -> Unicode
    normalize -> asciify (only when the user's ``asciify_paths`` is on; a
    read-only global read) -> ``legalize_path`` with the DRAFT replacements.
    """
    sub = item.evaluate_template(template(tmpl), True)
    sub = unicodedata.normalize("NFD" if sys.platform == "darwin" else "NFC", sub)
    if beets.config["asciify_paths"].get(confuse.TypeTemplate(bool, default=False)):
        sub = util.asciify_path(sub, beets.config["path_sep_replace"].as_str())
    legal, _ = util.legalize_path(sub, replacements, ".flac")
    return legal


def render_samples(
    lib: Any, *, rules: list[NamingRuleInput], replace: list[ReplaceRuleInput]
) -> tuple[list[RenderedRule], list[ReplaceError]]:
    """Render every rule against an apt sample under the DRAFT replace map.

    Read-only; binds ``music_dir_context`` for parity with the rest of the
    adapter (cheap insurance; the renders are scalar field reads). Returns the
    per-rule previews and any malformed-regex errors (shared across rules)."""
    replacements, replace_errors = compile_replacements(replace)
    rendered: list[RenderedRule] = []
    with lib.music_dir_context():
        for rule in rules:
            item, source = _pick_sample(lib, rule.query)
            try:
                sample_path = _render_one(item, rule.template, replacements)
                err: str | None = None
            except Exception as exc:  # defensive — functemplate is lenient
                sample_path = ""
                err = str(exc) or exc.__class__.__name__
            rendered.append(
                RenderedRule(
                    query=rule.query, sample_path=sample_path, sample_source=source, error=err
                )
            )
    return rendered, replace_errors
