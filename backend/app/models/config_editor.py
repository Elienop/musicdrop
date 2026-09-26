"""Pydantic models for the Layer-3 config editor.

``loc_to_dot_sep`` is vendored verbatim from the Pydantic Errors docs
(https://docs.pydantic.dev/latest/errors/errors/) — it's example code on that
page, not a public Pydantic export.

Per Pydantic v2 docs (https://docs.pydantic.dev/latest/concepts/models/) the
default ``extra='ignore'`` is exactly what we want: we validate, never
re-emit. Save writes the submitted text itself, so every key survives on disk.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
)


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


#: What resolving an operator-supplied path raises. The same three
#: ``app.beets.store_layout`` names, spelled again rather than imported: these
#: models sit below the beets adapter and pull nothing from it. Pydantic turns a
#: ``ValueError`` from a validator into a row and lets the other two escape, so
#: every one of them has to become a ``ValueError`` here or the route answers
#: 500 — measured: a ``directory:`` that is a self-referencing symlink raised
#: ``RuntimeError`` out of the schema check, and both
#: ``POST /api/config/validate`` and ``POST /api/config/save`` answered 500.
_UNRESOLVABLE = (OSError, RuntimeError, ValueError)


def _resolve_or_row(p: Path) -> Path:
    """``p`` resolved, or a ``ValueError`` Pydantic can paint as a row."""
    try:
        return p.expanduser().resolve()
    except _UNRESOLVABLE as exc:
        raise ValueError(f"{str(p)!r} could not be resolved: {type(exc).__name__}: {exc}.") from exc


def _writable_path(p: Path) -> Path:
    """``AfterValidator`` for ``WritablePath`` — ``p`` is already a ``Path``.

    An existing directory passes on its OWN writability: the Docker norm is a
    volume at the root, whose parent ``/`` the app user can never write. The
    parent check applies only to a directory beets would have to create.

    Every filesystem call sits inside :func:`_resolve_or_row` or the ``try``
    below: this runs BEFORE the layout gate, so its failures reach the client.
    """
    resolved = _resolve_or_row(p)
    try:
        exists = resolved.exists()
        is_dir = resolved.is_dir()
        parent = resolved.parent
        parent_ok = parent.exists() and os.access(parent, os.W_OK)
        writable = os.access(resolved, os.W_OK)
    except _UNRESOLVABLE as exc:
        raise ValueError(
            f"{str(resolved)!r} could not be examined: {type(exc).__name__}: {exc}"
        ) from exc
    if exists:
        if not is_dir:
            raise ValueError(f"{str(resolved)!r} is not a directory")
        if not writable:
            raise ValueError(f"directory {str(resolved)!r} is not writable")
    elif not parent_ok:
        raise ValueError(f"parent directory {str(parent)!r} is not writable")
    return p


def _library_file(p: Path) -> Path:
    """``AfterValidator`` for ``library:`` — the beets DATABASE FILE.

    beets hands this path to SQLite. Two values pass every other check and make
    that open fail: an empty one (Pydantic coerces ``""`` to ``Path(".")``) and
    one naming an existing directory. Measured before this validator: both linted
    clean, Save wrote them, Apply answered 500 ("unable to open database file"),
    and the next cold start died inside beets' ``_create_connection``.

    RESIDUAL (shared with :func:`_writable_path`): a RELATIVE value resolves
    against the process CWD here and against the beets data dir in confuse, so
    such a value is caught here only when the two coincide. Apply covers it.
    """
    if str(p) == ".":
        raise ValueError(
            "library: names the beets database file, and an empty value names the"
            " beets data directory itself, which beets cannot open as a database"
        )
    resolved = _resolve_or_row(p)
    try:
        is_dir = resolved.is_dir()
    except _UNRESOLVABLE as exc:
        raise ValueError(
            f"{str(resolved)!r} could not be examined: {type(exc).__name__}: {exc}"
        ) from exc
    if is_dir:
        raise ValueError(
            f"{str(resolved)!r} is a directory; library: names the beets database file"
        )
    return p


WritablePath = Annotated[Path, AfterValidator(_writable_path)]
LibraryFile = Annotated[Path, AfterValidator(_library_file)]


class ImportSection(BaseModel):
    """Validates the ``import:`` block, and reads it for :func:`import_advisories`.
    ``extra='ignore'`` is explicit only for clarity — it's the Pydantic v2 default
    and we never re-emit.

    Its refusal is what Validate and Save show for the flags beets tests with a
    bare ``if`` (``autotag``, ``singletons``, ``incremental``, ``link``,
    ``hardlink``). For the keys beets reads typed, beets' own read refuses first
    and this row is dropped (``app/beets/config_check.py``). A value this refuses
    gets no advisory.

    beets' key ``import.copy`` is read into ``copy_`` through Pydantic's alias,
    because a field named ``copy`` shadows ``BaseModel.copy()`` and Pydantic
    warns about it on every start."""

    model_config = ConfigDict(extra="ignore")

    @field_validator(
        "copy_",
        "move",
        "write",
        "autotag",
        "singletons",
        "incremental",
        "delete",
        "link",
        "hardlink",
        "reflink",
        mode="before",
    )
    @classmethod
    def _reject_quoted_bool(cls, value: object, info: ValidationInfo) -> object:
        """Refuse a string, which beets never reads as this bool.

        Pydantic's lax bool reads ``'no'``/``'off'``/``'false'``/``'0'`` as
        False. beets does not: it tests most of these flags with a bare ``if``
        on the raw view, and a non-empty string is truthy — so ``move: 'no'``
        saved clean, fired no advisory, and handed the user a MOVE they believed
        they had turned off. ``write`` it reads with ``.get(bool)``, which
        raises on a string (``beets/importer/stages.py:385``). Unquoted ``no``
        parses to a real bool and never reaches this; unquoted ``y`` and
        ``maybe`` are strings, as beets reads them. ``reflink`` keeps
        ``"auto"``, a real beets value.
        """
        if info.field_name == "reflink":
            if isinstance(value, str) and value != "auto":
                raise ValueError("must be a bool or auto: write yes, no or auto, without quotes")
        elif isinstance(value, str):
            raise ValueError("must be a bool: write yes or no, without quotes")
        return value

    copy_: bool = Field(default=True, alias="copy")
    move: bool = False
    write: bool = True
    autotag: bool = True
    singletons: bool = False
    incremental: bool = False
    # Mirrors beets' ``DuplicateAction.choices()``, the list beets reads this key
    # with; tests/test_config_advisories.py fails when the two drift apart.
    duplicate_action: Literal["skip", "keep", "remove", "merge", "ask", "upgrade"] = "ask"
    # The four file keys the editor used to drop on the floor. Unmodeled, a
    # ``delete: yes`` typed here saved clean, fired no advisory, and removed the
    # user's downloads on the next import. Defaults are beets' own.
    delete: bool = False
    link: bool = False
    hardlink: bool = False
    reflink: bool | Literal["auto"] = False


class MatchSection(BaseModel):
    """Validates the ``match:`` block."""

    model_config = ConfigDict(extra="ignore")

    strong_rec_thresh: float = Field(default=0.04, ge=0.0, le=1.0)
    medium_rec_thresh: float = Field(default=0.25, ge=0.0, le=1.0)


class KnownKeysSchema(BaseModel):
    """MusicDrop's own policies beets does not have: ``directory:`` and
    ``library:`` required and usable, the match thresholds at most 1, and no
    string in an ``import:`` flag beets reads as on.

    beets decides everything else, plugin names included (owner ruling
    2026-09-25): its typed reads run first (``app/beets/config_check.py``), and a
    key they refuse gets no row from here. Default ``extra='ignore'``: Save
    writes the submitted text, never this model."""

    model_config = ConfigDict(extra="ignore")

    directory: WritablePath
    library: LibraryFile
    import_: ImportSection = Field(default_factory=ImportSection, alias="import")
    match: MatchSection = Field(default_factory=MatchSection)


class ValidationErrorItem(BaseModel):
    """One row of the ``POST /api/config/validate`` and ``POST /api/config/save``
    error responses. ``line`` is 1-based for CodeMirror's ``state.doc.line(n)``
    (CodeMirror reference manual)."""

    loc: str
    """Dotted path, e.g. ``"import.copy"``. Empty for a YAML parse error, a file
    that cannot be read or written, or a message that names its key itself."""

    msg: str
    type: str
    line: int | None = None
    column: int | None = None


class ConfigAdvisory(BaseModel):
    """One note about a config that is valid and does not do what it says."""

    # This docstring is PUBLISHED as the schema description, so the rest is a
    # comment. Deliberately not a ``ValidationErrorItem``: the editor paints the
    # error list red in CodeMirror's lint gutter, and every config an advisory
    # fires on is one both this app and beets accept. One source today: an
    # ``import:`` key MusicDrop overrides. An ``include:`` entry beets would skip
    # was one until 2026-09-25; it is an error row now.
    #
    # No ``line``/``column``: resolving those needs the composed YAML the beets
    # adapter holds (``app/beets/config_check.py``), and this module is
    # import-clean of beets. ``key`` is the dotted path in the
    # same shape as ``ValidationErrorItem.loc``, which is enough to name the
    # setting.

    key: str
    """The setting this is about, dotted: ``"import.autotag"``."""

    message: str
    """One or two sentences: what really happens, and where the value still counts."""


def _autotag_advisory(section: ImportSection) -> str | None:
    if section.autotag:
        return None
    return (
        "MusicDrop forces import.autotag on for every import it runs: with autotag off"
        " beets drops the only stage that reports an album's outcome, so imports would"
        " finish with nothing recorded. This value has no effect in the app —"
        " `beet import` from the command line still honours it."
    )


def _duplicate_action_advisory(section: ImportSection) -> str | None:
    if section.duplicate_action == "ask":
        return None
    return (
        'MusicDrop always runs "ask" for its own imports: duplicates come back to the'
        " review queue instead of being resolved unattended."
        f' Your "{section.duplicate_action}" is discarded in the app — `beet import` from'
        " the command line still honours it."
    )


def _singletons_advisory(section: ImportSection) -> str | None:
    if not section.singletons:
        return None
    return (
        "MusicDrop forces import.singletons off on every import path it runs, review"
        " imports included: only album-shaped tasks can be reviewed, banked or tracked."
        " This value has no effect in the app — `beet import` from the command line"
        " still honours it."
    )


def _incremental_advisory(section: ImportSection) -> str | None:
    # The hardlink clause is stated, not detected: ``import_advisories``
    # validates one key at a time, so ``section.hardlink`` here is always the
    # default whatever the file says.
    if not section.incremental:
        return None
    return (
        "MusicDrop honours import.incremental: a folder in beets' import history is"
        " skipped and counted as already known. A sweep, or a hardlink import from Add"
        " from folder, forces it on and sets incremental_skip_later itself; a bank apply,"
        " slskd's imports and Import them again force it off. `beet import` behaves the"
        " same way."
    )


def _delete_advisory(section: ImportSection) -> str | None:
    # The predicate stays ``section.delete`` alone, not "delete AND copy": under
    # ``{hardlink: yes, delete: yes}`` beets clears ``delete`` itself, so the
    # value destroys nothing there — but it is still inert in the app, which is
    # what the user needs told. Only the CAUSAL clause carries "with copy on",
    # which is false for every non-copy config.
    if not section.delete:
        return None
    return (
        "MusicDrop forces import.delete off on every import it runs: with copy on,"
        " beets would remove your downloads after filing them. To move a download into"
        " the library instead, set import.move. This value has no effect in the app —"
        " `beet import` from the command line still honours it."
    )


def _hardlink_advisory(section: ImportSection) -> str | None:
    # Every import honours the file operation since slskd's follow the config
    # (decisions #77), so ``hardlink`` itself is not overridden. What is: a
    # hardlink run from Add from folder forces the history keys
    # (``run_import_worker``), and an ``incremental: no`` beside it fires no
    # rule of its own, so it is said here.
    if not section.hardlink:
        return None
    return (
        "A hardlink import from Add from folder turns beets' import history on, so a"
        " kept folder added again is skipped. `beet import` leaves history to your"
        " config."
    )


#: The advisory rules, in the order they are reported. Each entry is a key under
#: ``import:`` plus a predicate over the parsed section that returns the message
#: or ``None``.
#:
#: These are DELIBERATE semantic rules, not tightened types. beets' typed reads
#: refuse an invalid value of the keys it reads typed; nothing fires for
#: ``autotag: false`` because ``false`` is a perfectly valid bool. What the user
#: has no way to learn is that MusicDrop overrides it (``run_import_worker``
#: snapshots, forces and restores these keys around every session —
#: ``incremental`` excepted: it is honoured on the default review path and
#: forced by four exclusive arms, sweep, bank-apply, hardlink and the per-run
#: ``incremental: False`` that "Import them again", Review and slskd's imports send
#: (``import_session.run_import_worker``), which is what its advisory says).
#:
#: ``link``/``hardlink``/``reflink`` are HONOURED by every import, slskd's
#: included (decisions #77), so ``link`` and ``reflink`` have no rule; ``hardlink``
#: has one only for the history keys an Add from folder hardlink run forces.
#: Trash restore names its own operation, but for a restore, not an import. A
#: config with every file operation off has no rule at all — beets imports in
#: place, which is its own behaviour with no override to name. What each import resolved to is
#: LOGGED by ``run_import_worker``.
_IMPORT_ADVISORY_RULES: Final[tuple[tuple[str, Callable[[ImportSection], str | None]], ...]] = (
    ("autotag", _autotag_advisory),
    ("duplicate_action", _duplicate_action_advisory),
    ("singletons", _singletons_advisory),
    ("incremental", _incremental_advisory),
    ("delete", _delete_advisory),
    ("hardlink", _hardlink_advisory),
)


def import_advisories(data: object) -> list[ConfigAdvisory]:
    """Collect advisories for ``import:`` keys MusicDrop overrides.

    ``data`` is the parsed YAML root — typed ``object`` rather than a mapping
    on purpose: a parse of ``""`` returns ``None`` for a cleared editor buffer,
    and a document whose root is a list or a scalar parses fine too. Every
    non-mapping shape simply has no ``import:`` section to advise on, and the
    errors channel is what tells the user about it.

    Two rules about when this stays silent:

    * **Only keys the config actually SETS.** An omitted key is not an opinion,
      so a config that never mentions these four gets zero advisories. Presence
      is read from the raw mapping — ``ImportSection``'s defaults would make
      every config look like it had set all seven.
    * **Only VALID values.** Each key is validated on its own through
      ``ImportSection``. A value that fails is left alone: the errors channel
      is already reporting it, and one bad key must not mute the rules for its
      siblings.
    """
    if not isinstance(data, Mapping):
        return []
    section = data.get("import")
    if not isinstance(section, Mapping):
        return []
    out: list[ConfigAdvisory] = []
    for key, rule in _IMPORT_ADVISORY_RULES:
        if key not in section:
            continue
        try:
            parsed = ImportSection.model_validate({key: section[key]})
        except ValidationError:
            continue
        message = rule(parsed)
        if message is not None:
            out.append(ConfigAdvisory(key=f"import.{key}", message=message))
    return out


class SaveRequest(BaseModel):
    """Body of ``POST /api/config/save``. ``base_sha256`` is the CAS token —
    echoed back from whatever snapshot the client loaded; mismatch -> 409
    with diff. mtime_ns is intentionally NOT a CAS field: nanosecond ints
    blow past JavaScript's ``Number.MAX_SAFE_INTEGER`` (2^53 - 1) and would
    silently corrupt the round-trip. The SHA-256 already catches any
    bytes-changed edit, including ones that preserved mtime via
    ``os.utime``."""

    yaml_text: str
    base_sha256: str


class ValidateRequest(BaseModel):
    """Body of ``POST /api/config/validate``. The endpoint is CAS-free — it
    only lints, never writes."""

    yaml_text: str


class ValidateResponse(BaseModel):
    """Response of ``POST /api/config/validate``.

    A named model rather than the looser ``dict[str, list[ValidationErrorItem]]``
    so OpenAPI emits a ``$ref`` to a concrete ``ValidateResponse`` schema. The
    frontend codegen (T10's openapi-typescript pass) then produces a clean
    ``{errors: ValidationErrorItem[]}`` TS type instead of a generic
    ``Record<string, ValidationErrorItem[]>``.

    TWO channels, and the split is the point. ``errors`` is what CodeMirror
    paints red; ``advisories`` is what it must not. A config that only trips an
    advisory is VALID and saves cleanly.
    """

    errors: list[ValidationErrorItem]

    advisories: list[ConfigAdvisory]
    """Valid settings MusicDrop-driven imports override. Required rather than
    defaulted so the generated TypeScript is ``advisories:`` and not
    ``advisories?:`` — the route always sends the key, including on the YAML
    parse-error path, so an optional type would make readers defend against an
    absence that cannot happen."""


class NamingRuleInput(BaseModel):
    """One ``paths:`` entry as the panel sees it: a beets query key (``default`` /
    ``comp`` / ``singleton`` / any query like ``albumtype:soundtrack``) and its
    path-format template string."""

    query: str
    template: str


class ReplaceRuleInput(BaseModel):
    """One ``replace:`` entry — a regex ``pattern`` and its ``replacement`` string
    (empty replacement = delete the matched text)."""

    pattern: str
    replacement: str


class RenderedRule(BaseModel):
    """A single rule's live preview: the path a sample track would get under this
    template. ``sample_source`` labels where the sample came from (an album label,
    or a built-in example). ``error`` is a defensive per-row field — beets'
    ``functemplate`` is lenient and rarely raises, so it is usually ``None``."""

    query: str
    sample_path: str
    sample_source: str
    error: str | None = None


class ReplaceError(BaseModel):
    """A ``replace:`` row whose regex failed to compile. ``index`` points at the
    row in the request's ``replace`` list."""

    index: int
    pattern: str
    message: str


class NamingConfig(BaseModel):
    """Response of ``GET /api/config/naming`` — the current ``paths:``/``replace:``
    split into structured rows, the CAS ``sha256`` token (same one the config
    snapshot uses), plus the initial previews so the panel paints fully populated.

    ``beets_replace`` is beets' own ``replace:`` rules, in the order the installed
    beets lists them, sent whether or not config.yaml has a ``replace:`` block
    (a block replaces beets' rules outright). Empty when beets' defaults cannot be
    read."""

    default: str | None
    comp: str | None
    singleton: str | None
    custom: list[NamingRuleInput]
    replace: list[ReplaceRuleInput]
    beets_replace: list[ReplaceRuleInput]
    sha256: str
    previews: list[RenderedRule]
    replace_errors: list[ReplaceError]


class NamingPreviewRequest(BaseModel):
    """Body of ``POST /api/config/naming/preview`` — read-only render, no CAS."""

    rules: list[NamingRuleInput]
    replace: list[ReplaceRuleInput]


class NamingPreviewResponse(BaseModel):
    rendered: list[RenderedRule]
    replace_errors: list[ReplaceError]


class SaveNamingRequest(BaseModel):
    """Body of ``POST /api/config/naming/save``. ``base_sha256`` is the CAS token
    echoed from the snapshot the panel loaded; mismatch -> 409."""

    rules: list[NamingRuleInput]
    replace: list[ReplaceRuleInput]
    base_sha256: str
