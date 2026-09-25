"""Validate's and Save's check of a ``config.yaml`` text, asked the way beets asks it.

Four steps, one list of rows (owner rulings 2026-09-23 and 2026-09-25):

1. **Load** with beets' own loader, then YamlSource's ``or {}`` and its mapping
   check (``confuse/sources.py:96-107``). What refuses here refuses a start.
2. **Includes**: MusicDrop's gate (:func:`~app.beets.store_layout.load_candidate`),
   which reads every include without blocking. An include beets would skip is an
   error, as it is at Apply and boot.
3. **beets' typed reads**, replayed on the candidate layered the way beets layers
   it (:func:`beets_read_failures`). beets has no whole-config check, so the list
   is ours to keep current across beets upgrades.
4. **MusicDrop's own policies** beets does not have: ``directory:``/``library:``
   required and usable (:class:`~app.models.config_editor.KnownKeysSchema`), the
   match thresholds at most 1, no string in an ``import:`` flag beets tests
   with a bare ``if``, and the store layout.

Every message is the loader's, confuse's or beets' own text. A row names its key
once: the editor prints ``loc: msg``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from functools import cached_property, partial
from pathlib import Path
from typing import Any, Final, NamedTuple, TypeVar

import beets
import confuse
import yaml
from beets.exceptions import UserError
from beets.importer.actions import DuplicateAction
from beets.util import unique_list
from pydantic import ValidationError

from app.beets.library import LibraryHandle
from app.beets.store_layout import LoadedCandidate, layout_check_for_candidate, load_candidate
from app.config import Settings
from app.models.config_editor import (
    ConfigAdvisory,
    KnownKeysSchema,
    ValidationErrorItem,
    import_advisories,
    loc_to_dot_sep,
)

__all__ = [
    "NOT_A_MAPPING",
    "STORE_LAYOUT",
    "ConfigCheck",
    "NotAMapping",
    "beets_read_failures",
    "check_config_text",
    "load_config_text",
    "parse_problem",
]

#: The one sentence for a top level beets does not read as settings.
NOT_A_MAPPING: Final = "config.yaml must be a mapping of settings."

#: ``type`` of a row from one of beets' typed reads (:func:`beets_read_failures`).
_BEETS_READ: Final = "beets_read"

#: ``type`` of a row about ``include:`` or the store layout; its text names its remedy.
STORE_LAYOUT: Final = "store_layout"

#: ``type`` of a row for an include beets would skip (an error since 2026-09-25,
#: as it is at Apply and boot).
_INCLUDE_SKIPPED: Final = "include_skipped"

#: The tag PyYAML gives a ``<<`` key (``yaml/resolver.py``).
_MERGE_TAG: Final = "tag:yaml.org,2002:merge"


class NotAMapping(Exception):
    """The text loads, and its top level is not a mapping (a list or a scalar)."""


def load_config_text(text: str) -> dict[str, Any]:
    """``text`` read the way beets reads ``config.yaml``.

    beets' loader (``beets.config.loader``, confuse's ``Loader``:
    ``beets/__init__.py:24,51`` → ``confuse/core.py:510,525``), then YamlSource's
    ``or {}`` and mapping check (``confuse/sources.py:101-107``): an empty, ``~``,
    ``[]`` or ``false`` top level is no settings, as beets reads it.

    Raises:
        confuse.ConfigReadError: a YAML error; its mark is on ``exc.reason``.
        NotAMapping: the top level is a list or a scalar beets refuses.
        Exception: what else PyYAML's constructors raise, unwrapped, as it
            escapes beets' own read: ``KeyError`` for ``!!bool ture``,
            ``ValueError`` for ``!!float abc``.
    """
    value = (
        confuse.yaml_util.load_yaml_string(
            text, confuse.CONFIG_FILENAME, loader=beets.config.loader
        )
        or {}
    )
    if not isinstance(value, dict):
        raise NotAMapping(NOT_A_MAPPING)
    return value


_T = TypeVar("_T")
_E = TypeVar("_E", bound=BaseException)


def parse_problem(exc: _E) -> _E | Exception:
    """The parser's own error inside a ``ConfigReadError``; ``exc`` otherwise."""
    if isinstance(exc, confuse.ConfigReadError) and exc.reason is not None:
        return exc.reason
    return exc


class ReadFailure(NamedTuple):
    """One of beets' reads that raised: the key path it read, and what it raised as text."""

    path: tuple[str, ...]
    message: str


def _compile_replacements(cfg: confuse.Configuration) -> None:
    """``Library.get_replacements`` (``beets/library/library.py:61-71``) asked of ``cfg``.

    beets' own reads the global config, so it cannot be called on a candidate.
    """
    for pattern in cfg["replace"].get(dict):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise UserError(f"Malformed regular expression in replace: {pattern}") from exc


def _is_list_section(view: confuse.ConfigView) -> bool:
    """A section written as a list, which :func:`beets_read_failures` does not ask."""
    return view.exists() and isinstance(view.get(), list)


def beets_read_failures(cfg: confuse.Configuration) -> list[ReadFailure]:
    """Replay on ``cfg`` the typed reads that can stop a start, and the ``import.*`` ones.

    Line by line from beets 2.14.0; re-check this list on every beets upgrade:

    * start-up, in the order beets makes them:
      ``beets/plugins.py:472-473,485-489`` (``get_plugin_names``, replayed
      because it changes ``sys.path``, ``beetsplug.__path__`` and the global
      config); ``:196-198`` (``_verify_config``, sent on ``pluginload``), asked
      of every enabled plugin; ``:265`` (``verbose``, read by every plugin
      listener); ``app/beets/setup.py:278-279``; ``beets/library/library.py:84``
      and ``:64-71``; ``beets/dbcore/db.py:1064`` (on a migration).
    * import: MusicDrop's pre-check (``app/beets/import_session.py:2444-2445``);
      ``beets/importer/stages.py:385``; ``beets/importer/tasks.py:509-511,1475``;
      ``beets/importer/session.py:109-112,140,179-181``; and
      ``beets/autotag/match.py:285,288`` for the two thresholds MusicDrop limits.

    ``_verify_config``'s read is beets' own only for a metadata source, which
    cannot be known without importing the plugin. It is asked here of every
    enabled plugin: a ``null`` or scalar section fails it, as it fails the first
    read of any plugin that reads its settings. That is stricter than beets for a
    plugin that reads none (``mbsync: no`` boots). A list section is not asked:
    some plugins read their section as a list (``beetsplug/advancedrewrite.py:168``,
    ``beetsplug/loadext.py:29``). Nothing else a plugin reads is here: beets
    drops a plugin whose own settings fail and starts without it.
    """
    failures: list[ReadFailure] = []

    def read(path: tuple[str, ...], call: Callable[[], _T]) -> _T | None:
        try:
            return call()
        # Broad: whatever the replayed read raises, beets' own raises too.
        except Exception as exc:
            failures.append(ReadFailure(path, _named(exc)))
            return None

    read(
        ("pluginpath",),
        lambda: [
            str(Path(p).expanduser().absolute()) for p in cfg["pluginpath"].as_str_seq(split=False)
        ],
    )
    names: list[str] = unique_list(read(("plugins",), lambda: cfg["plugins"].as_str_seq()) or [])
    cfg.add({"disabled_plugins": []})
    disabled = set(read(("disabled_plugins",), lambda: cfg["disabled_plugins"].as_str_seq()) or [])
    musicbrainz: dict[str, Any] = read(("musicbrainz",), lambda: cfg["musicbrainz"].flatten()) or {}
    if musicbrainz.get("enabled"):
        if "musicbrainz" not in names:
            names.append("musicbrainz")
    elif musicbrainz.get("enabled") is False:
        disabled.add("musicbrainz")
    enabled = [name for name in names if name not in disabled]
    for name in enabled:
        if not _is_list_section(cfg[name]):
            read((name,), partial(cfg[name].__contains__, "source_weight"))
    if enabled:
        read(("verbose",), lambda: cfg["verbose"].get(int))
    read(("library",), lambda: cfg["library"].as_filename())
    read(("directory",), lambda: cfg["directory"].as_filename())
    read(("timeout",), lambda: cfg["timeout"].as_number())
    read(("replace",), lambda: _compile_replacements(cfg))
    read(
        ("create_backup_before_migrations",),
        lambda: cfg["create_backup_before_migrations"].get(bool),
    )

    for key in ("copy", "move", "write", "delete", "remux_mp3_in_wav"):
        read(("import", key), partial(cfg["import"][key].get, bool))
    read(("import", "reflink"), lambda: cfg["import"]["reflink"].as_choice(["auto", True, False]))
    read(("import", "resume"), lambda: cfg["import"]["resume"].as_choice([True, False, "ask"]))
    read(
        ("import", "duplicate_action"),
        lambda: cfg["import"]["duplicate_action"].as_choice(DuplicateAction.choices()),
    )
    for key in ("strong_rec_thresh", "medium_rec_thresh"):
        read(("match", key), cfg["match"][key].as_number)
    return failures


class _Positions:
    """Where a key path's value is written in the text, composed only when asked.

    beets' loader keeps no positions, so the text is composed again with the same
    loader: ``yaml.compose`` gives every node its mark. The nodes are ``Any``:
    PyYAML ships no types (the ``yaml`` override in ``pyproject.toml``).
    """

    def __init__(self, text: str) -> None:
        self._text = text

    @cached_property
    def _root(self) -> Any:
        """The text composed once, on the first ask; ``None`` when it does not compose."""
        try:
            return yaml.compose(self._text, Loader=beets.config.loader)
        # The text loaded already; a failure here only costs the position.
        except Exception:
            return None

    def at(self, path: Sequence[str | int]) -> tuple[int | None, int | None]:
        """1-based line and 0-based column; ``(None, None)`` when not written here.

        A key only an include or beets' defaults supply has no line. A value
        where a section is read (``import: 5`` for ``import.write``) is the one
        beets refuses, so the row goes on it.
        """
        node = self._root
        if not isinstance(node, yaml.MappingNode) or not path:
            return (None, None)
        for key in path:
            if isinstance(node, yaml.MappingNode):
                child = _mapping_value(node, key)
            elif isinstance(node, yaml.SequenceNode) and isinstance(key, int):
                child = node.value[key] if 0 <= key < len(node.value) else None
            else:
                break
            if child is None:
                return (None, None)
            node = child
        return (node.start_mark.line + 1, node.start_mark.column)


def _mapping_value(node: Any, key: str | int) -> Any:
    """``key``'s value node in a mapping node, as PyYAML would construct it.

    A repeated key: the last one is read. Then the ``<<`` merges, the first
    listed winning, as ``SafeConstructor.flatten_mapping`` orders them.
    """
    own = [
        value
        for name, value in node.value
        if name.tag != _MERGE_TAG and isinstance(name, yaml.ScalarNode) and name.value == key
    ]
    if own:
        return own[-1]
    for merged in _merged_mappings(node):
        found = _mapping_value(merged, key)
        if found is not None:
            return found
    return None


def _merged_mappings(node: Any) -> list[Any]:
    """The mapping nodes a mapping node's ``<<`` keys merge, in the order they are listed."""
    merged: list[Any] = []
    for name, value in node.value:
        if name.tag == _MERGE_TAG:
            merged += value.value if isinstance(value, yaml.SequenceNode) else [value]
    return [mapping for mapping in merged if isinstance(mapping, yaml.MappingNode)]


def _named(exc: BaseException) -> str:
    """The error's own text, with its class when the text alone does not say what failed."""
    if isinstance(exc, (confuse.ConfigError, UserError, yaml.YAMLError)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _parse_row(exc: BaseException) -> ValidationErrorItem:
    problem = parse_problem(exc)
    mark = getattr(problem, "problem_mark", None)
    return ValidationErrorItem(
        loc="",
        msg=_named(problem),
        type="yaml_parse",
        line=None if mark is None else mark.line + 1,
        column=None if mark is None else mark.column,
    )


def _written_in(cfg: confuse.Configuration, path: tuple[str, ...]) -> str | None:
    """The file whose value beets read for ``path``, or for the section that broke it.

    An include is merged ABOVE config.yaml, so a key config.yaml also writes
    can fail on the include's value; its line in config.yaml is not the one to
    fix. ``import: 5`` fails ``import.write`` before any value is found, so a
    failing lookup asks the section above it.
    """
    for depth in range(len(path), 0, -1):
        view: confuse.ConfigView = cfg
        for key in path[:depth]:
            view = view[key]
        try:
            for _value, source in view.resolve():
                return source.filename
            return None
        except confuse.ConfigError:
            continue
    return None


def _read_rows(
    failures: list[ReadFailure], positions: _Positions, loaded: LoadedCandidate, document_file: str
) -> list[ValidationErrorItem]:
    """One row per problem, the first read beets would stop on.

    ``import: 5`` fails every ``import.*`` read with one sentence, and
    ``musicbrainz: no`` fails ``flatten`` and then ``_verify_config``: a later
    failure with the same sentence, or on the same key, adds no row.

    confuse's templates write ``<key>: <problem>``; that key becomes ``loc`` so
    the editor's ``loc: msg`` prints confuse's sentence exactly once.
    """
    rows: list[ValidationErrorItem] = []
    seen: set[str] = set()
    reported: list[tuple[str, ...]] = []
    for failure in failures:
        text = failure.message
        if text in seen or any(_related(failure.path, path) for path in reported):
            continue
        seen.add(text)
        reported.append(failure.path)
        dotted = ".".join(failure.path)
        loc, msg = (
            (dotted, text[len(dotted) + 2 :]) if text.startswith(f"{dotted}: ") else ("", text)
        )
        source = _written_in(loaded.config, failure.path)
        line, column = positions.at(failure.path) if source == document_file else (None, None)
        # An include's value has no line here; name the file it came from.
        if source is not None and source in loaded.included:
            msg = f"{msg} (in {loaded.included[source]})"
        rows.append(
            ValidationErrorItem(loc=loc, msg=msg, type=_BEETS_READ, line=line, column=column)
        )
    return rows


def _related(a: Sequence[str | int], b: Sequence[str | int]) -> bool:
    """One key path is the other, or holds it."""
    n = min(len(a), len(b))
    return tuple(a[:n]) == tuple(b[:n])


def _policy_rows(
    document: dict[str, Any], failed: list[tuple[str, ...]], positions: _Positions
) -> list[ValidationErrorItem]:
    """:class:`KnownKeysSchema`'s rows, less any key a beets read already refused."""
    try:
        KnownKeysSchema.model_validate(document)
    except ValidationError as exc:
        rows: list[ValidationErrorItem] = []
        for err in exc.errors():
            path = tuple(err["loc"])
            if any(_related(path, read) for read in failed):
                continue
            line, column = positions.at(path)
            rows.append(
                ValidationErrorItem(
                    loc=loc_to_dot_sep(path),
                    msg=str(err["msg"]),
                    type=str(err["type"]),
                    line=line,
                    column=column,
                )
            )
        return rows
    return []


def _include_rows(loaded: LoadedCandidate, positions: _Positions) -> list[ValidationErrorItem]:
    """The include gate's refusal, and one row per include beets would skip."""
    line, column = positions.at(("include",))
    rows = [
        ValidationErrorItem(
            loc="include",
            msg=f"beets would skip the include {skipped.name!r}: {skipped.reason}",
            type=_INCLUDE_SKIPPED,
            line=line,
            column=column,
        )
        for skipped in loaded.skipped
    ]
    if loaded.error is not None:
        rows.append(
            ValidationErrorItem(
                loc="include", msg=str(loaded.error), type=STORE_LAYOUT, line=line, column=column
            )
        )
    return rows


def _layout_rows(
    loaded: LoadedCandidate,
    *,
    settings: Settings,
    handle: LibraryHandle,
    reported: set[str],
    positions: _Positions,
) -> list[ValidationErrorItem]:
    """The store-layout refusal for the ``directory:`` and ``library:`` beets would load.

    A one-value refusal on a key another row already named is dropped: a
    ``directory:`` holding a NUL drew the schema's row and this one, both about
    the NUL. A LAYOUT refusal is never dropped.
    """
    error = layout_check_for_candidate(loaded, settings, handle).error
    if error is None:
        return []
    # A refusal between two env-derived paths names no key; ``directory:`` is
    # the line the editor can act from.
    key = error.config_key or "directory"
    if error.unusable_value and key in reported:
        return []
    line, column = positions.at((key,))
    return [
        ValidationErrorItem(loc=key, msg=str(error), type=STORE_LAYOUT, line=line, column=column)
    ]


class ConfigCheck(NamedTuple):
    """The rows that refuse a Save, and the advisories that never do."""

    errors: list[ValidationErrorItem]
    advisories: list[ConfigAdvisory]


def check_config_text(
    text: str, *, settings: Settings, handle: LibraryHandle | None
) -> ConfigCheck:
    """Every row :func:`module <app.beets.config_check>` describes for ``text``.

    ``handle`` is ``None`` only in a process whose lifespan has not run; the
    store-layout rows need it and are left out there, and relative paths
    resolve against the configured beets dir.
    """
    try:
        document = load_config_text(text)
    except NotAMapping:
        return ConfigCheck([ValidationErrorItem(loc="", msg=NOT_A_MAPPING, type="model_type")], [])
    # Broad: loading changes nothing, and beets' own read lets these through.
    except Exception as exc:
        return ConfigCheck([_parse_row(exc)], [])

    positions = _Positions(text)
    beets_dir = handle.beets_dir if handle is not None else Path(settings.beets_dir)
    loaded = load_candidate(document, beets_dir)
    failures = beets_read_failures(loaded.config)
    failed = [failure.path for failure in failures]
    document_file = str(beets_dir / confuse.CONFIG_FILENAME)
    errors = _read_rows(failures, positions, loaded, document_file)
    errors += _policy_rows(document, failed, positions)
    errors += _include_rows(loaded, positions)
    # Only over a complete candidate whose own paths beets reads, as before:
    # without ``directory:`` the schema's row stands for it. A path beets cannot
    # read has its row already; from an include it would get a second one here
    # (``_include_sets_a_non_path``), which the ``reported`` check does not drop.
    if (
        handle is not None
        and loaded.error is None
        and "directory" in document
        and not any(path in failed for path in (("directory",), ("library",)))
    ):
        reported = {row.loc for row in errors} | {".".join(path) for path in failed}
        errors += _layout_rows(
            loaded, settings=settings, handle=handle, reported=reported, positions=positions
        )
    # An advisory is about a value beets accepts; one it refuses has its row.
    advisories = [
        advisory
        for advisory in import_advisories(document)
        if not any(_related(advisory.key.split("."), path) for path in failed)
    ]
    return ConfigCheck(errors, advisories)
