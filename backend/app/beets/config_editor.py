"""Layer-3 config editor — write-side helpers.

ruamel.yaml is used ONLY for the write path (load -> mutate -> dump).
Per the maintainer (Anthon van der Neut, https://yaml.dev/doc/ruamel.yaml/detail/),
the default ``YAML()`` is ``typ='rt'`` — round-trip — which preserves comments,
key order, block style, scalar quoting style, and anchors. Booleans always
emit as ``true``/``false`` regardless of the parsed form; this is the
maintainer's documented invariant, so the editor does not try to fight it.

The default ``extra='ignore'`` on Pydantic (per Pydantic v2 docs § Models)
is correct here: we never round-trip through the schema, only validate.
Unknown beets/plugin keys live on disk in the ruamel ``CommentedMap``.

Currently exports: ``parse_yaml``, ``validate_known_keys``. Later tasks add
``walk_get``/``walk_set``, ``merge_preserve_secrets``, ``atomic_write``,
``save``, and ``apply`` to the same module.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from app.models.config_editor import (
    KnownKeysSchema,
    ValidationErrorItem,
    loc_to_dot_sep,
)

__all__ = ["parse_yaml", "validate_known_keys"]


def _yaml() -> YAML:
    """Construct the canonical round-trip ``YAML`` instance.

    Settings mirror the spec & plan:

    * default ``typ='rt'`` (do NOT pass it explicitly — maintainer warns
      against it).
    * ``yaml.version = (1, 1)`` so ``yes`` / ``no`` parse as bool (ruamel
      SF #285 — https://sourceforge.net/p/ruamel-yaml/tickets/285/ — and
      YAML 1.1 spec).
    * ``preserve_quotes = True`` so the user's quoting style survives a
      round-trip.
    * ``indent(mapping=2, sequence=4, offset=2)`` — ruamel-recommended block
      indent for the cleanest dump (see ``YAML.indent`` docs).
    * ``width = 4096`` so long strings don't get rewrapped.
    """
    yaml = YAML()
    yaml.version = (1, 1)
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
    return yaml


def parse_yaml(text: str) -> CommentedMap:
    """Parse YAML text into a ruamel ``CommentedMap``.

    Raises:
        ruamel.yaml.YAMLError: on parse failure. Callers map this to HTTP 422
            with the ``problem_mark`` line/column (see ``api/config_.py``).
    """
    # ruamel.yaml's `YAML.load` returns `Any` in the bundled stubs; in
    # round-trip mode the result is a `CommentedMap` for a YAML mapping (the
    # only shape the editor accepts at the root). The cast pins the type
    # without changing runtime behaviour.
    return cast(CommentedMap, _yaml().load(text))


def _line_col_for_path(
    root: CommentedMap, path: tuple[str | int, ...]
) -> tuple[int, int] | tuple[None, None]:
    """Resolve a Pydantic error ``loc`` to a 1-based ``(line, column)``.

    Walks ``root`` along ``path[:-1]`` to land on the parent node, then
    asks ruamel's line-column tracker for the offending child's position
    (per https://yaml.dev/doc/ruamel.yaml/detail/). The accessor differs
    by container type:

    * ``CommentedMap``  -> ``parent.lc.value(key)`` for a ``str`` key.
    * ``CommentedSeq``  -> ``parent.lc.item(idx)``  for an ``int`` index.

    Both return ``(line0, col0)`` (0-based). We bump the line by 1 because
    CodeMirror's ``state.doc.line(n)`` is 1-based (CodeMirror reference
    manual § ``Text.line``). The original implementation always called
    ``.lc.value(...)`` which raises ``IndexError`` on a sequence parent —
    so e.g. ``('plugins', 2)`` silently lost its gutter marker.

    Defensively swallows missing keys, missing ``.lc``, and non-map nodes
    — these can happen mid-edit when the schema and the parsed doc
    disagree on shape. Returning ``(None, None)`` lets the response carry
    the ``loc`` string without a gutter marker.
    """
    if not path:
        return (None, None)
    try:
        node: Any = root
        for key in path[:-1]:
            node = node[key]
        if hasattr(node, "lc") and node.lc.data is not None:
            last = path[-1]
            if isinstance(node, CommentedSeq) and isinstance(last, int):
                line_col = node.lc.item(last)
            else:
                line_col = node.lc.value(last)
            if line_col is not None:
                line0, col0 = line_col
                return (line0 + 1, col0)
    except (KeyError, IndexError, AttributeError, TypeError):
        pass
    return (None, None)


def validate_known_keys(
    data: CommentedMap | dict[str, Any],
) -> list[ValidationErrorItem]:
    """Run ``KnownKeysSchema`` and map each Pydantic error to a line/col.

    Returns an empty list on success. On failure, each
    ``ValidationErrorItem`` carries:

    * ``loc`` — the Pydantic loc tuple rendered as a dotted path (e.g.
      ``"import.copy"``).
    * ``msg`` / ``type`` — verbatim from Pydantic.
    * ``line`` / ``column`` — resolved via ``_line_col_for_path`` when
      ``data`` is a ``CommentedMap`` (i.e. it came from ``parse_yaml``).
      When ``data`` is a plain ``dict`` (e.g. callers that already
      deserialized elsewhere), both are ``None``.

    Expects a root from ``parse_yaml()``; hand-built ``CommentedMap``s
    will silently lose line/col because their ``.lc.data`` is ``None``
    until ruamel populates it during parse.
    """
    try:
        KnownKeysSchema.model_validate(data)
        return []
    except ValidationError as e:
        out: list[ValidationErrorItem] = []
        root = data if isinstance(data, CommentedMap) else None
        for err in e.errors():
            line: int | None = None
            col: int | None = None
            if root is not None:
                line, col = _line_col_for_path(root, err["loc"])
            out.append(
                ValidationErrorItem(
                    loc=loc_to_dot_sep(err["loc"]),
                    msg=str(err["msg"]),
                    type=str(err["type"]),
                    line=line,
                    column=col,
                )
            )
        return out
