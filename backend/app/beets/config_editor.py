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

Currently exports: ``parse_yaml``, ``validate_known_keys``,
``store_layout_report``, ``atomic_write``, ``read_naming``, ``save``,
``save_naming``, and ``apply`` (asyncio-locked threadpool rebuild that swaps
``app.state.beets_library``).

Save writes the submitted document straight back to disk (the editor serves and
edits the RAW ``config.yaml``): there is no secret-preserve merge — masking the
served text is exactly what used to overwrite list-nested credentials with
"REDACTED" and flatten the user's comments/anchors.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import threading
from collections.abc import Collection
from pathlib import Path
from typing import Any, Final, NamedTuple, cast

import beets
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError

# ``build_config_snapshot`` is used by save()/apply() to return the post-write
# snapshot (raw editable doc + redacted effective view + freshness fields).
from app.beets.config_snapshot import build_config_snapshot
from app.beets.library import LibraryHandle

# ``setup_beets`` / ``reset_beets_globals`` are bound at MODULE LEVEL on
# purpose: the Apply 500-branch test monkeypatches ``app.beets.config_editor``
# directly (the name the handler captured at import time), so the lambda fires
# inside ``_rebuild_beets_handle``. Importing them inside the function body
# would defeat that patch and the 500 path would silently call the real
# beets setup.
from app.beets.setup import reset_beets_globals, setup_beets
from app.beets.store_layout import (
    StoreLayoutError,
    checked_store_dirs,
    layout_check_for_config,
)
from app.config import Settings
from app.config import settings as _module_settings
from app.library_busy import library_job_active
from app.models.config_api import BeetsConfigSnapshot
from app.models.config_editor import (
    ConfigAdvisory,
    KnownKeysSchema,
    NamingConfig,
    NamingRuleInput,
    ReplaceRuleInput,
    SaveNamingRequest,
    SaveRequest,
    ValidationErrorItem,
    loc_to_dot_sep,
)
from app.playlists.atomic import write_atomic_text

__all__ = [
    "apply",
    "atomic_write",
    "parse_yaml",
    "read_naming",
    "save",
    "save_naming",
    "store_layout_report",
    "validate_known_keys",
]


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

    Walks ``root`` along ``path[:-1]``, then asks ruamel's tracker for the
    child's 0-based position — ``lc.value(key)`` on a map, ``lc.item(idx)`` on a
    sequence. The line is bumped by 1 for CodeMirror's 1-based ``doc.line(n)``.
    Always calling ``.lc.value(...)`` raises ``IndexError`` on a sequence parent,
    which lost ``('plugins', 2)`` its gutter marker.

    Swallows missing keys, missing ``.lc`` and non-map nodes, which happen
    mid-edit when the schema and the parsed doc disagree on shape:
    ``(None, None)`` carries the ``loc`` with no gutter marker.

    A key that arrived through a ``<<:`` merge is PRESENT in the mapping and
    absent from ``lc.data``, so :func:`_merged_line_col` asks the anchor's own
    mapping, which records the line the value is written on.
    """
    if not path:
        return (None, None)
    try:
        node: Any = root
        for key in path[:-1]:
            node = node[key]
        if hasattr(node, "lc") and node.lc.data is not None:
            last = path[-1]
            try:
                if isinstance(node, CommentedSeq) and isinstance(last, int):
                    line_col = node.lc.item(last)
                else:
                    line_col = node.lc.value(last)
            except (KeyError, IndexError):
                # RAISES rather than returning None for a merged key, so the
                # fallback has to sit inside its own ``except`` — measured:
                # ``lc.value('directory')`` on a ``<<: *base`` document raised
                # ``KeyError`` while the key was present in the mapping.
                line_col = None
            if line_col is not None:
                line0, col0 = line_col
                return (line0 + 1, col0)
            return _merged_line_col(node, last)
    except (KeyError, IndexError, AttributeError, TypeError):
        pass
    return (None, None)


def _merged_line_col(node: Any, key: str | int) -> tuple[int, int] | tuple[None, None]:
    """Where a merged-in key is WRITTEN: its anchor's line, not the ``<<:`` line.

    ``_base: &base\n  directory: /music\n<<: *base`` gave the refusal no gutter
    position at all (measured). ``CommentedMap.merge`` holds each merged mapping,
    and those are ``CommentedMap``s with their own ``lc.data``.
    """
    for merged in getattr(node, "merge", ()) or ():
        data = getattr(getattr(merged, "lc", None), "data", None)
        if data is not None and key in data:
            line0, col0 = merged.lc.value(key)
            return (line0 + 1, col0)
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


#: The ``type`` on the row a refused ``directory:`` produces. Not a Pydantic
#: error type — nothing in ``KnownKeysSchema`` can express "this value is fine on
#: its own but destroys data given where Trash resolves", so the check runs
#: beside the schema rather than inside it.
_STORE_LAYOUT_ERROR_TYPE: Final = "store_layout"


class StoreLayoutReport(NamedTuple):
    """The document's gutter rows, and one advisory per include beets drops."""

    errors: list[ValidationErrorItem]
    advisories: list[ConfigAdvisory]


def _skipped_include_advisory(name: str) -> ConfigAdvisory:
    """An include beets would drop. Advisory, not an error: beets starts."""
    return ConfigAdvisory(
        key="include",
        message=(
            f"beets could not read {name!r}, so it skips that entry and stops"
            " reading include: there. Nothing listed after it is merged."
        ),
    )


def store_layout_report(
    data: CommentedMap | dict[str, Any],
    *,
    settings: Settings,
    handle: LibraryHandle,
    reported_keys: Collection[str] = (),
) -> StoreLayoutReport:
    """Zero or one row: the ``directory:`` and ``library:`` the submitted document
    would LOAD, against where Trash, the origin store and the beets data dir resolve.

    "Would load" and not ``data["directory"]``: an ``include:`` is merged ABOVE
    the document's own keys, so :func:`effective_config_paths` asks beets' own
    config class. THE SINGLE SOURCE for Validate and :func:`save`, so the gutter
    and the Save refusal cannot disagree.

    Silent when the document has no ``directory:`` or its value is not a
    filename — ``KnownKeysSchema`` reports both. A missing ``library:`` is held
    to beets' own ``library.db`` default instead, which is what the next boot
    opens; the schema requires that key too, so at Validate and Save the default
    adds no row and only Apply's on-disk read reaches it. The ``isinstance`` on
    ``data`` is load-bearing; ruamel returns ``None`` for an empty document.

    ``reported_keys`` suppress exactly one row: a single-UNUSABLE-VALUE refusal
    on a key the schema also reported, where both say the same thing. Measured,
    ``directory: "/music/\\0evil"`` drew a ``value_error`` and a ``store_layout``
    row both naming the NUL. A LAYOUT refusal is never suppressed — under
    ``directory: /`` the schema's "not writable" and the layout row are different
    facts, and only the second names the loss.
    """
    if not isinstance(data, dict) or "directory" not in data:
        return StoreLayoutReport([], [])
    check = layout_check_for_config(document=data, settings=settings, handle=handle)
    advisories = [_skipped_include_advisory(name) for name in check.skipped_includes]
    error = check.error
    if error is None:
        return StoreLayoutReport([], advisories)
    if error.unusable_value and (error.config_key or "directory") in reported_keys:
        return StoreLayoutReport([], advisories)
    # The gutter row is painted against the key the refusal is ABOUT, so a
    # ``library:`` that lands inside Trash underlines ``library:`` and not the
    # ``directory:`` line above it. A refusal between two env-derived paths names
    # no config key; it still has to be shown, and ``directory:`` is the line the
    # editor can act from.
    key = error.config_key or "directory"
    root = data if isinstance(data, CommentedMap) else None
    line, col = _line_col_for_path(root, (key,)) if root is not None else (None, None)
    return StoreLayoutReport(
        [
            ValidationErrorItem(
                loc=key,
                msg=str(error),
                type=_STORE_LAYOUT_ERROR_TYPE,
                line=line,
                column=col,
            )
        ],
        advisories,
    )


def _strip_yaml_directive(text: str) -> str:
    """Drop a leading ``%YAML 1.1`` directive line and its ``---`` document-start.

    ruamel emits this two-line prologue whenever ``yaml.version`` is set. We keep
    the version on the dumper (it drives 1.1 scalar-quoting — see ``atomic_write``)
    but the directive itself is unwanted churn in the user's config.yaml, so we
    peel it off the dumped text. Only a directive at the very top is stripped; a
    ``---`` is removed only when it directly follows the directive (never a
    ``---`` that legitimately appears inside the document).
    """
    if not text.startswith("%YAML"):
        return text
    newline = text.find("\n")
    if newline == -1:
        return text
    rest = text[newline + 1 :]
    if rest.startswith("---\n"):
        rest = rest[len("---\n") :]
    elif rest == "---\n".rstrip("\n") or rest.startswith("--- "):
        # A "--- <inline scalar>" form (never produced for a mapping root, but be
        # defensive): keep the content after the marker.
        rest = rest[len("---") :].lstrip(" ")
    return rest


def atomic_write(dst: Path, data: CommentedMap, yaml: YAML) -> None:
    """Dump ``data`` and publish it as ``dst`` through the shared atomic writer.

    The recipe lives in one place — ``write_atomic_text``: a temp beside the
    target under a name nobody can precompute, fsync, ``os.replace``, then the
    parent-dir fsync that forces the rename durable. Nothing here derives the
    temp's name from ``dst``; a link planted at the old ``.<name>.tmp`` sibling
    was followed and then published AS the destination.

    ``mode=None`` is this file's policy, and it is mode bits ONLY: an existing
    regular config keeps its permissions (e.g. ``0o600`` on a credential-bearing
    file) while its mtime advances. The advancing mtime is the Save freshness
    signal (:attr:`BeetsConfigSnapshot.apply_pending` compares
    ``current_mtime`` to ``handle.file_mtime_at_load``); carrying the old mtime
    over would mask "the user just saved" and the Apply button would never light
    up. First write: the umask default. See ``write_atomic_bytes`` for what
    ``None`` does when the target is not a regular file.
    """
    # Dump to a buffer and strip the "%YAML 1.1" directive prologue (see
    # _strip_yaml_directive) rather than write ``yaml.dump(data, f)`` directly:
    # ruamel injects that header on every dump whenever ``yaml.version`` is set,
    # churning the user's hand-edited config.yaml (diff noise, a changed CAS sha,
    # a no-op save that isn't byte-identical). The version MUST stay (1,1) on the
    # dump side — clearing it would switch the emitter to the YAML-1.2 resolver,
    # which writes bool-token strings ("no"/"yes"/"on"/"off"/"y"/"n") and
    # sexagesimals ("d:d:d") UNQUOTED; those silently reload as bool/int and
    # corrupt config (e.g. a naming ``replace`` rule value "no" becomes False,
    # crashing beets' re.compile on Apply). 1.1 keeps them quoted.
    buf = io.StringIO()
    yaml.dump(data, buf)
    write_atomic_text(dst, _strip_yaml_directive(buf.getvalue()), mode=None)


# Serializes the read-SHA -> compare -> atomic-write of ``save``/``save_naming``
# (both run sync via run_in_threadpool). Without it two concurrent saves that
# loaded the same base both pass the CAS and both write, silently losing one; the
# lock makes the second re-read the now-updated bytes so its CAS correctly 409s.
_SAVE_LOCK = threading.Lock()


def save(handle: LibraryHandle, req: SaveRequest, *, settings: Settings) -> BeetsConfigSnapshot:
    """Persist ``req.yaml_text`` to ``handle.config_path``, returning the new snapshot.

    Sequence (spec § "Layer 3 — Backend: Save flow"):

    1. **Parse** with ruamel — bad YAML -> HTTP 422 with ``problem_mark`` line/col.
    2. **Schema validate** via ``KnownKeysSchema`` — known-key errors -> 422 with
       per-error ``ValidationErrorItem`` payloads. Then the same
       :func:`store_layout_report` row ``POST /api/config/validate`` paints
       in the gutter: a ``directory:`` that would put the music library at or
       under Trash (or over the origin store) is refused HERE, before the write,
       because the file this writes is also the file the process boots from — a
       config saved in that shape would refuse to start on the next restart.
    3. **SHA-256 CAS** — compare ``req.base_sha256`` to the SHA-256 of the
       on-disk bytes. Mismatch -> 409 with ``current_yaml_text`` (raw on-disk
       file) and ``current_sha256`` so the frontend's merge view can render
       the diff. SHA-256 alone is the CAS token (no mtime check): nanosecond
       mtime ints overflow JavaScript's ``Number.MAX_SAFE_INTEGER`` and
       silently corrupt across the JSON wire — the SHA already covers every
       bytes-changed edit, including the rare ``os.utime`` "preserve mtime,
       change content" case (see ``test_save_409_on_sha_change``). The CAS token
       hashes the same RAW bytes the GET snapshot served as ``yaml_text``, so the
       editor's base and the write target line up exactly.
    4. **Atomic write** via ``atomic_write`` — fsync + dir-fsync + the file's
       own mode preserved (``mode=None``). The submitted document is written
       verbatim (no secret-preserve merge: the editor serves and edits the raw
       file). Mode bits only: atime/mtime advance so step 5's freshness signal
       fires.
    5. **Return new snapshot** — ``apply_pending`` will be ``True`` because the
       mtime advanced past ``handle.file_mtime_at_load`` (this is the load-bearing
       reason ``atomic_write`` preserves the mode and not the mtime — a frozen
       mtime and the Apply button would never light up); the Apply endpoint
       (Task 8) is what clears it.
    """
    yaml = _yaml()

    # NOTE: The 422 and 409 payloads below are built as literal dicts, deliberately
    # NOT derived from the Pydantic models. The contract tests
    # (test_config_validation_body_contract / test_config_conflict_body_contract)
    # validate REAL response bodies against the models; if these raises were
    # changed to build the payload from the model, both halves of the pin would
    # become invariant and the check would silently go vacuous.

    # 1. Parse with ruamel.
    try:
        new_map = parse_yaml(req.yaml_text)
    # Same three as Validate's arm: ruamel raises RecursionError past the nesting
    # limit and ValueError on an over-long integer.
    except (YAMLError, RecursionError, ValueError) as exc:
        mark = getattr(exc, "problem_mark", None)
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": "",
                    "msg": str(exc),
                    "type": "yaml_parse",
                    "line": (mark.line + 1) if mark else None,
                    "column": mark.column if mark else None,
                }
            ],
        ) from exc

    # 2. Schema validate, then the containment check on ``directory:``. Same
    # list, same 422: to the editor both are lint rows on the same document, and
    # splitting them into two statuses would make the gutter and the Save button
    # disagree about what "there is an error" means.
    schema_errors = validate_known_keys(new_map)
    errors = (
        schema_errors
        + store_layout_report(
            new_map,
            settings=settings,
            handle=handle,
            reported_keys={item.loc for item in schema_errors},
        ).errors
    )
    if errors:
        raise HTTPException(
            status_code=422,
            detail=[item.model_dump() for item in errors],
        )

    # 3. SHA-256 CAS. Read the bytes once and reuse them for both the hash and
    # the (possible) 409 payload. The CAS read → write runs under _SAVE_LOCK so a
    # concurrent save can't pass the same-base check and clobber this one
    # (last-writer-wins).
    with _SAVE_LOCK:
        on_disk_bytes = handle.config_path.read_bytes()
        on_disk_sha = hashlib.sha256(on_disk_bytes).hexdigest()
        if on_disk_sha != req.base_sha256:
            raise HTTPException(
                status_code=409,
                detail={
                    "detail": "File changed on disk",
                    "current_yaml_text": on_disk_bytes.decode("utf-8"),
                    "current_sha256": on_disk_sha,
                },
            )

        # 4. Atomic write. The editor serves and edits the RAW file (secrets
        # included), so the submitted document IS the intended file — write it
        # straight back. There is deliberately no secret-preserve merge: masking
        # the served text is what used to clobber list-nested credentials with
        # "REDACTED" and flatten the user's comments/anchors on the round-trip.
        atomic_write(handle.config_path, new_map, yaml)

    # 5. Return the new snapshot. apply_pending will be True because mtime
    # advanced past handle.file_mtime_at_load — Task 8's Apply endpoint clears it.
    return build_config_snapshot(handle)


def _beets_default_naming() -> tuple[dict[str, str], dict[str, str]]:
    """beets' built-in ``paths``/``replace`` defaults, read FRESH from the
    installed beets' bundled ``config_default.yaml``.

    NOT ``beets.config``: that is the loaded/merged config, which is STALE
    between a naming Save and an Apply (Apply has not reloaded beets yet), so
    reading it would show the pre-Save values and make the panel revert the
    user's just-saved edit. The bundled file is static and version-pinned
    (``beets==2.13.*``), never contaminated by the loaded user config, so
    "on-disk override ?? bundled default" is correct both with no override AND
    immediately after a Save.

    Returns ``(paths_defaults, replace_defaults)`` as plain ``{str: str}`` maps
    (a ``None`` value coerced to ``""`` for forward-safety). Degrades to
    ``({}, {})`` on any read/parse failure so :func:`read_naming` never 500s.
    """
    try:
        text = (Path(beets.__file__).parent / "config_default.yaml").read_text(encoding="utf-8")
        doc = parse_yaml(text)
    except (OSError, ValueError, YAMLError):
        return ({}, {})
    if not isinstance(doc, dict):
        return ({}, {})

    paths_raw = doc.get("paths")
    paths_src = paths_raw if isinstance(paths_raw, dict) else {}
    paths = {str(k): "" if v is None else str(v) for k, v in paths_src.items()}

    replace_raw = doc.get("replace")
    replace_src = replace_raw if isinstance(replace_raw, dict) else {}
    replace = {str(k): "" if v is None else str(v) for k, v in replace_src.items()}

    return (paths, replace)


def read_naming(handle: LibraryHandle) -> NamingConfig:
    """Parse the on-disk ``paths:``/``replace:`` into structured rows + CAS sha,
    falling back per key to beets' built-in defaults so the panel reflects the
    *effective* naming even when the user relies on the defaults (no explicit
    ``paths:``/``replace:`` block).

    ``previews`` and ``replace_errors`` are left empty here — the router fills
    them by calling the renderer with ``handle.lib`` (this function stays
    config-only, no library access)."""
    on_disk_bytes = handle.config_path.read_bytes()
    sha = hashlib.sha256(on_disk_bytes).hexdigest()
    doc = parse_yaml(on_disk_bytes.decode("utf-8"))

    # ``or {}`` is not enough — a truthy scalar/list (from a hand-corrupted
    # config like ``paths: somestring``) would survive it and then ``.items()``
    # would raise. Coerce any non-mapping to empty so read never 500s.
    paths_raw = doc.get("paths")
    paths = paths_raw if isinstance(paths_raw, dict) else {}
    default = comp = singleton = None
    custom: list[NamingRuleInput] = []
    for key, val in paths.items():
        tmpl = "" if val is None else str(val)
        skey = str(key)
        if skey == "default":
            default = tmpl
        elif skey == "comp":
            comp = tmpl
        elif skey == "singleton":
            singleton = tmpl
        else:
            custom.append(NamingRuleInput(query=skey, template=tmpl))

    # Per-key fallback to beets' bundled defaults. ``paths`` IS merged per-key in
    # beets, so a user who set only ``default`` still inherits ``comp``/
    # ``singleton`` — this mirrors that. The explicit on-disk value (even an
    # explicit ``""``) wins; a bundled key may still be absent (``None``) if a
    # future beets drops it.
    paths_default, replace_default = _beets_default_naming()
    if default is None:
        default = paths_default.get("default")
    if comp is None:
        comp = paths_default.get("comp")
    if singleton is None:
        singleton = paths_default.get("singleton")

    # ``replace`` is NOT merged. beets reads it from a single source
    # (``config["replace"].get(dict)`` -> confuse ``first()``), so a present
    # ``replace:`` block *wholly replaces* beets' defaults. Show the explicit
    # rows verbatim when present, else the bundled defaults. (An explicit empty
    # ``replace: {}`` is indistinguishable from absent here -> both show the
    # defaults; an accepted rare-case simplification.)
    replace_raw = doc.get("replace")
    replace_map = replace_raw if isinstance(replace_raw, dict) else {}
    if not replace_map:
        replace_map = replace_default
    replace = [
        ReplaceRuleInput(pattern=str(p), replacement="" if r is None else str(r))
        for p, r in replace_map.items()
    ]

    return NamingConfig(
        default=default,
        comp=comp,
        singleton=singleton,
        custom=custom,
        replace=replace,
        sha256=sha,
        previews=[],
        replace_errors=[],
    )


def _naming_map(rules: list[NamingRuleInput]) -> CommentedMap:
    """A ruamel mapping of query -> template, skipping rows with an empty query
    or template (an empty query would otherwise write a stray ``'': tmpl`` key)."""
    m = CommentedMap()
    for rule in rules:
        if rule.query and rule.template:
            m[rule.query] = rule.template
    return m


def _replace_map(replace: list[ReplaceRuleInput]) -> CommentedMap:
    """A ruamel mapping of pattern -> replacement, skipping empty patterns."""
    m = CommentedMap()
    for row in replace:
        if row.pattern:
            m[row.pattern] = row.replacement
    return m


def save_naming(handle: LibraryHandle, req: SaveNamingRequest) -> BeetsConfigSnapshot:
    """Write ``paths:``/``replace:`` back into the same ``config.yaml``.

    1. **Regex validate** — any ``replace`` pattern that fails ``re.compile`` ->
       422 (beets' ``get_replacements()`` would otherwise raise on config load).
    2. **SHA-256 CAS** — ``req.base_sha256`` vs the on-disk bytes; mismatch -> 409
       with ``current_sha256`` (same shape as ``save``'s 409).
    3. **ruamel round-trip** — load the on-disk doc, replace ONLY the ``paths:``
       and ``replace:`` nodes (empty -> drop the key); every other key, comment,
       and secret is untouched.
    4. **Atomic write** + return the standard snapshot (``apply_pending`` True
       until Apply reloads beets).

    No :func:`store_layout_report` step, unlike :func:`save`: step 3 rewrites
    exactly two nodes and neither is ``directory:``, so the music root this
    document resolves to is the same one before and after — a naming Save cannot
    move ``M`` into a refused relationship with Trash or the origin store.
    """
    yaml = _yaml()

    # NOTE: Same as ``save`` above — the 422/409 payloads are literal dicts,
    # deliberately not model-derived, so the contract tests' real-body half
    # stays load-bearing.

    # 1. Regex validate.
    bad: list[dict[str, object]] = []
    for i, row in enumerate(req.replace):
        if not row.pattern:
            continue
        try:
            re.compile(row.pattern)
        except re.error as exc:
            bad.append({"loc": f"replace[{i}]", "msg": f"invalid regex: {exc}", "type": "regex"})
    if bad:
        raise HTTPException(status_code=422, detail=bad)

    # 2. CAS. Read → compare → merge → write under _SAVE_LOCK so a concurrent
    # save can't pass the same-base check and clobber this one.
    with _SAVE_LOCK:
        on_disk_bytes = handle.config_path.read_bytes()
        on_disk_sha = hashlib.sha256(on_disk_bytes).hexdigest()
        if on_disk_sha != req.base_sha256:
            raise HTTPException(
                status_code=409,
                detail={
                    "detail": "File changed on disk",
                    "current_yaml_text": on_disk_bytes.decode("utf-8"),
                    "current_sha256": on_disk_sha,
                },
            )

        # 3. Round-trip merge — only the two nodes change.
        doc = parse_yaml(on_disk_bytes.decode("utf-8"))
        paths = _naming_map(req.rules)
        if paths:
            doc["paths"] = paths
        else:
            doc.pop("paths", None)
        replace = _replace_map(req.replace)
        if replace:
            doc["replace"] = replace
        else:
            doc.pop("replace", None)

        # 4. Atomic write + snapshot.
        atomic_write(handle.config_path, doc, yaml)
    return build_config_snapshot(handle)


def on_disk_layout_error(handle: LibraryHandle, settings: Settings) -> StoreLayoutError | None:
    """The refusal ``config.yaml`` AS IT SITS ON DISK would cause, or ``None``.

    Apply's input is the file, not a request body, so a hand edit (or an editor
    session from before a restart) can carry a ``directory:`` no Save ever saw.
    Read fresh here rather than from the handle: ``handle.lib.directory`` is the
    music root of the load being replaced.

    Silent on an unreadable or unparseable file. That is not this check's
    question — ``setup_beets`` will fail on the same file moments later and
    :func:`apply` already answers 500 with the restart hint — and returning a
    layout refusal for a YAML syntax error would name the wrong problem.

    ``RecursionError`` and ``ValueError`` are the two Save and Validate also
    catch around ``parse_yaml``: ruamel raises them past the nesting limit and on
    an over-long integer. This call sits OUTSIDE Apply's rebuild handler, so
    either one left the route answering a bare 500. ``UnicodeDecodeError`` is not
    named because it IS a ``ValueError``.
    """
    try:
        doc = parse_yaml(handle.config_path.read_text(encoding="utf-8"))
    except (OSError, YAMLError, RecursionError, ValueError):
        return None
    if not isinstance(doc, CommentedMap):
        return None
    return layout_check_for_config(document=doc, settings=settings, handle=handle).error


def _rebuild_beets_handle(old: LibraryHandle, beets_dir: str) -> LibraryHandle:
    """Tear down beets process-globals and re-run ``setup_beets()``.

    Blocking — runs in FastAPI's threadpool. Pure of the request scope so unit
    tests can drive it directly without an ASGI lifecycle. Order matters:
    ``reset_beets_globals(old)`` closes the previous library's SQLite handle
    AND clears confuse + plugin caches, so the subsequent ``setup_beets`` re-
    reads ``config.yaml`` from scratch instead of replaying the previous load.

    Note: if ``setup_beets()`` raises, the old handle is already torn down —
    the process is in a degraded state and serves errors until restart. The
    :func:`apply` 500 path surfaces this with a ``recovery`` hint pointing
    at restart; we deliberately do NOT try to "undo" the teardown on failure
    because confuse + plugins + SQLite would each need their own rollback,
    which is exactly the kind of half-recovered state the restart hint avoids.
    """
    reset_beets_globals(old)
    return setup_beets(beets_dir)


def _swap_lock(app: FastAPI) -> asyncio.Lock:
    """Return the per-app Apply swap lock, creating it lazily if missing.

    Lazy creation is the gentle posture: production sets the lock in
    ``main.py``'s lifespan, but the ``TestClient`` ``client`` fixture skips
    lifespan (the ``with`` block would tear down the hand-wired
    ``app.state.beets_library``). Reaching this branch in production would
    mean the lifespan never ran, which is already a much bigger problem than
    a missing lock. Idempotent: a second call sees the cached lock.
    """
    lock = getattr(app.state, "beets_swap_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state.beets_swap_lock = lock
    assert isinstance(lock, asyncio.Lock)
    return lock


def _settings(app: FastAPI) -> Settings:
    """Return the live :class:`Settings` instance.

    Production's lifespan parks ``settings`` on ``app.state``; the
    ``TestClient`` ``client`` fixture skips lifespan, so we fall back to the
    module-level singleton. Monkey-patching ``app.config.settings`` (the test
    pattern the existing ``beets_library`` fixture uses) still works either
    way: production sees the patched value through ``app.state.settings``
    (which the lifespan set at startup), tests see it through the fallback.
    """
    s: Settings | None = getattr(app.state, "settings", None)
    if s is None:
        s = _module_settings
    return s


async def apply(request: Request) -> BeetsConfigSnapshot:
    """Reload beets in-process: swap ``app.state.beets_library`` for a fresh handle.

    Sequence (spec § "Layer 3 - Backend: Apply flow"):

    1. **Import gate** — 409 while an import is active; the rebuild tears down
       the SQLite connection its worker holds.
    2. **Per-app lock** — two Applies racing through ``reset_beets_globals`` +
       ``setup_beets`` could close the library twice.
    2b. **Containment gate** — 422 on a refused store layout in the config ON
       DISK (:func:`on_disk_layout_error`, ``store_layout._ROWS``). Before the
       rebuild, because a failure after its teardown strands the process with no
       working config; 422 and not 409, because the page renders every Apply 409
       as the library-job sentence.
    3. **Threadpool rebuild** — blocking I/O; any exception maps to 500 with the
       restart hint.
    4. **Atomic swap** — ``app.state.beets_library`` is replaced only after the
       rebuild succeeds. On a 500 the OLD handle is already torn down
       (:func:`_rebuild_beets_handle`), so the process is degraded until restart.
    4b. **Backstop** — the same layout question, asked of what beets actually
       loaded. 422, with the new handle already swapped in and the refusal
       recorded on the import registry.
    5. **Return snapshot** — ``apply_pending`` is ``False``: the new handle's
       ``file_mtime_at_load`` captured the on-disk mtime during ``setup_beets``.
    """
    app = request.app

    # Outside lock: best-effort gate; TOCTOU acceptable for single-user
    # self-host (an import can still arrive between this check and the swap,
    # but the worst case is a 500 inside the rebuild — the registry's own
    # threading.Lock guarantees the import either finished or hasn't started
    # touching beets yet, and the 500 path's recovery hint covers the rest).
    # Pulling the gate inside the asyncio.Lock would block Apply behind
    # any concurrent Apply request even when no import is active, which is
    # worse UX for the single-user case this product targets.
    if library_job_active():
        raise HTTPException(
            status_code=409,
            detail="Import in progress; Apply available when it finishes / lyrics backfill",
        )

    async with _swap_lock(app):
        old: LibraryHandle = app.state.beets_library
        settings = _settings(app)
        layout_error = await run_in_threadpool(on_disk_layout_error, old, settings)
        if layout_error is not None:
            # The headline is the refused PAIR, not a fixed sentence: this used
            # to read "config.yaml would move the music library" for every
            # refusal, including the ones where the Trash is what moved.
            raise HTTPException(
                status_code=422,
                detail={
                    "message": f"Apply refused: {layout_error.headline}",
                    "recovery": str(layout_error),
                },
            )
        try:
            new = await run_in_threadpool(_rebuild_beets_handle, old, settings.beets_dir)
        except Exception as exc:
            # Catch-all is deliberate: the rebuild reaches into beets'
            # private surface (LazyConfig._materialized, plugin caches),
            # plus filesystem + SQLite — any failure leaves the process in a
            # degraded state where the old handle may be partially closed.
            # Surfacing a structured 500 with the recovery hint is more
            # useful than re-raising into the ASGI 500 path.
            #
            # ``message`` (NOT ``detail``) for the inner key so the rendered
            # response body is ``{"detail": {"message": ..., "recovery": ...}}``
            # — Starlette already wraps our payload in an outer ``detail``,
            # so an inner ``detail`` would produce the confusing
            # ``{"detail": {"detail": ...}}`` shape the FE would have to
            # special-case. The 409 sibling stays a flat ``detail: str``;
            # this is the structured form of the same convention.
            raise HTTPException(
                status_code=500,
                detail={
                    "message": f"Apply failed during rebuild: {exc}",
                    "recovery": (
                        "Restart MusicDrop. The saved config is on disk; cold start will load it."
                    ),
                },
            ) from exc
        app.state.beets_library = new
        # Re-attach the fresh lib to the LIVE import registry. The lifespan
        # attaches the lib exactly once (main.py), and the registry's runner
        # captures it at construction (``_resolve_runner`` builds
        # ``BeetsImportRunner(self._lib, ...)``); without this, every later
        # import — manual, inbox webhook, or bank-apply — would silently keep
        # running against the pre-Apply Library (its ``directory``,
        # ``replacements`` and cached ``path_formats`` are frozen at build time,
        # and a changed ``library:`` would write a DIFFERENT DB than the UI now
        # reads). Lazy imports mirror this file's convention and dodge the
        # api.bank → beets.duplicates → beets.config_editor cycle. Inside the
        # swap lock, after the state swap, mirroring the lifespan wiring.
        from app.api.bank import get_bank_dir
        from app.import_jobs.registry import get_registry
        from app.playlists.store import get_playlists_dir

        # 4b. Backstop — the same question, asked of what beets ACTUALLY loaded.
        # Step 2b reproduces beets' include merge over the candidate document;
        # this one reads ``new.lib.directory`` and ``new.lib.path`` off the live
        # handle, so a divergence between that reproduction and beets (an
        # ``include:`` shape we read differently, a beets upgrade) is caught here
        # instead of shipping a refused layout into the process.
        #
        # It also supplies the pair the registry needs. Resolving those two paths
        # raises on a symlink loop, outside every ``except StoreLayoutError`` the
        # Apply path has; taking them from ``checked_store_dirs`` gives that the
        # same 422 as a refusal.
        try:
            trash_dir, origins_dir = checked_store_dirs(settings, new)
        except StoreLayoutError as exc:
            # ONE library after Apply, whatever the outcome. The rebuild has
            # already closed the old one and the swap above stands, so leaving
            # the registry holding it was measured to let an import "succeed"
            # into the pre-Apply store — SQLite reopens a closed handle on
            # demand — while the UI read the new one. The registry gets the NEW
            # library, no store pair, and the refusal: measured, an import
            # started in this state was accepted and wrote into the beets data
            # dir, so ``start`` now refuses with this sentence.
            # ``.exception``: the record carries the traceback with the
            # sentence, like the three boot refusals.
            logging.getLogger("uvicorn.error").exception(
                "Apply loaded a config whose store layout is refused: %s", exc
            )
            get_registry().attach_library(
                new.lib,
                None,
                bank_dir=get_bank_dir(),
                playlists_dir=get_playlists_dir(),
                trash_origins_dir=None,
                refusal=f"Apply loaded config.yaml, but {exc}",
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "message": f"Apply loaded config.yaml, but {exc.headline}",
                    "recovery": f"{exc} Then restart MusicDrop.",
                },
            ) from exc

        get_registry().attach_library(
            new.lib,
            trash_dir,
            bank_dir=get_bank_dir(),
            playlists_dir=get_playlists_dir(),
            trash_origins_dir=origins_dir,
        )

    return build_config_snapshot(new)
