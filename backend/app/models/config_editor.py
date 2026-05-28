"""Pydantic models for the Layer-3 config editor.

``loc_to_dot_sep`` is vendored verbatim from the Pydantic Errors docs
(https://docs.pydantic.dev/latest/errors/errors/) - it's example code on that
page, not a public Pydantic export.

Per Pydantic v2 docs (https://docs.pydantic.dev/latest/concepts/models/) the
default ``extra='ignore'`` is exactly what we want: we validate, never
re-emit. Unknown beets / plugin keys survive on disk in the ruamel
``CommentedMap`` (the file-canonical posture from Layers 1+2 holds).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def loc_to_dot_sep(loc: tuple[str | int, ...]) -> str:
    """Format a Pydantic error location tuple as a dotted path.

    Vendored verbatim from https://docs.pydantic.dev/latest/errors/errors/.
    """
    path = ""
    for i, x in enumerate(loc):
        if isinstance(x, str):
            if i > 0:
                path += "."
            path += x
        elif isinstance(x, int):
            path += f"[{x}]"
        else:  # pragma: no cover
            raise TypeError("Unexpected type")
    return path


def _writable_parent(p: Path) -> Path:
    """``AfterValidator`` for ``WritablePath``.

    Per Pydantic v2 docs (Validators), ``AfterValidator`` runs after Pydantic
    has coerced the value to ``Path`` - so ``p`` is already a ``Path`` here.
    """
    parent = p.expanduser().resolve().parent
    if not parent.exists() or not os.access(parent, os.W_OK):
        raise ValueError(f"parent directory {parent} is not writable")
    return p


WritablePath = Annotated[Path, AfterValidator(_writable_parent)]


PluginName = Literal[
    "musicbrainz",
    "deezer",
    "spotify",
    "discogs",
    "beatport",
    "tidal",
    "lyrics",
    "fetchart",
    "chroma",
    "lastgenre",
    "embedart",
    "replaygain",
    "scrub",
]


class ImportSection(BaseModel):
    """Validates the ``import:`` block. ``extra='ignore'`` is explicit only for
    clarity - it's the Pydantic v2 default and we never re-emit.

    The ``copy`` field name is dictated by beets' YAML key (``import.copy``);
    it shadows ``BaseModel.copy()`` but Pydantic v2 only emits a UserWarning
    and the model still works. The type: ignore handles mypy's stricter
    objection to redefining an inherited method's type."""

    model_config = ConfigDict(extra="ignore")

    copy: bool = True  # type: ignore[assignment]  # beets YAML key; shadows BaseModel.copy()
    move: bool = False
    write: bool = True
    autotag: bool = True
    singletons: bool = False
    incremental: bool = False
    duplicate_action: Literal["skip", "keep", "remove", "merge", "ask"] = "ask"


class MatchSection(BaseModel):
    """Validates the ``match:`` block."""

    model_config = ConfigDict(extra="ignore")

    strong_rec_thresh: float = Field(default=0.04, ge=0.0, le=1.0)
    medium_rec_thresh: float = Field(default=0.25, ge=0.0, le=1.0)


class KnownKeysSchema(BaseModel):
    """Validates only the ~13 keys MusicDrop models. Default ``extra='ignore'``
    means unknown beets/plugin keys are dropped silently here - they survive
    on disk because we save the ruamel ``CommentedMap``, never re-emit from
    this model (per Pydantic v2 docs Models)."""

    model_config = ConfigDict(extra="ignore")

    directory: WritablePath
    library: Path
    plugins: list[PluginName] = Field(default_factory=list)
    import_: ImportSection = Field(default_factory=ImportSection, alias="import")
    match: MatchSection = Field(default_factory=MatchSection)


class ValidationErrorItem(BaseModel):
    """One row of the ``POST /api/config/validate`` and ``POST /api/config/save``
    error responses. ``line`` is 1-based for CodeMirror's ``state.doc.line(n)``
    (CodeMirror reference manual)."""

    loc: str
    """Dotted path, e.g. ``"import.copy"``. Empty string for YAML parse errors."""

    msg: str
    type: str
    line: int | None = None
    column: int | None = None


class SaveRequest(BaseModel):
    """Body of ``POST /api/config/save``. The two CAS fields are echoed back
    from whatever snapshot the client loaded; mismatch -> 409 with diff."""

    yaml_text: str
    base_mtime_ns: int
    base_sha256: str


class ValidateRequest(BaseModel):
    """Body of ``POST /api/config/validate``. The endpoint is CAS-free - it
    only lints, never writes."""

    yaml_text: str
