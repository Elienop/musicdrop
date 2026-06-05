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

Currently exports: ``parse_yaml``, ``validate_known_keys``, ``walk_get``,
``walk_set``, ``merge_preserve_secrets``, ``atomic_write``, ``save``,
``apply`` (asyncio-locked threadpool rebuild that swaps
``app.state.beets_library``), plus the re-exports ``REDACTED_TOMBSTONE``
(from ``confuse``) and ``find_redacted_paths`` (from
``app.beets.config_snapshot``).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from confuse import REDACTED_TOMBSTONE
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError

from app.artist_art_jobs.registry import artist_art_backfill_active

# Re-exported from config_snapshot so save() can import both the masking
# sentinel and the path discoverer from one module. ``build_config_snapshot``
# is used by save() to return the post-write snapshot.
from app.beets.config_snapshot import build_config_snapshot, find_redacted_paths
from app.beets.library import LibraryHandle

# ``setup_beets`` / ``reset_beets_globals`` are bound at MODULE LEVEL on
# purpose: the Apply 500-branch test monkeypatches ``app.beets.config_editor``
# directly (the name the handler captured at import time), so the lambda fires
# inside ``_rebuild_beets_handle``. Importing them inside the function body
# would defeat that patch and the 500 path would silently call the real
# beets setup.
from app.beets.setup import reset_beets_globals, setup_beets
from app.config import Settings
from app.config import settings as _module_settings
from app.import_jobs.registry import get_registry
from app.lyrics_jobs.registry import lyrics_backfill_active
from app.models.config_api import BeetsConfigSnapshot
from app.models.config_editor import (
    KnownKeysSchema,
    SaveRequest,
    ValidationErrorItem,
    loc_to_dot_sep,
)
from app.reorganize_jobs.registry import reorganize_backfill_active

__all__ = [
    "REDACTED_TOMBSTONE",
    "apply",
    "atomic_write",
    "find_redacted_paths",
    "merge_preserve_secrets",
    "parse_yaml",
    "save",
    "validate_known_keys",
    "walk_get",
    "walk_set",
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


def walk_get(data: Any, path: tuple[str | int, ...]) -> Any:
    """Walk ``data`` along ``path`` and return the leaf, or ``None`` on miss.

    Used as a thin, total accessor over the heterogeneous shape that ruamel's
    round-trip mode produces — mappings come back as ``CommentedMap`` (a
    ``dict`` subclass) and sequences as ``CommentedSeq`` (a ``list`` subclass),
    so the plain ``isinstance(node, dict | list)`` branches cover both. Any
    step that misses returns ``None``; the caller pairs the result with an
    on-disk lookup that intentionally treats a missing path as "user deleted
    this key" (see ``merge_preserve_secrets``).

    An empty ``path`` is treated as a degenerate caller mistake and returns
    ``None`` rather than ``data`` itself — the "total accessor" framing means
    "every well-formed lookup that misses returns ``None``", and a zero-step
    walk has no well-formed answer.
    """
    if not path:
        return None
    node: Any = data
    for key in path:
        if isinstance(node, dict) and key in node:
            node = node[key]
        elif isinstance(node, list) and isinstance(key, int) and 0 <= key < len(node):
            node = node[key]
        else:
            return None
    return node


def walk_set(data: Any, path: tuple[str | int, ...], value: Any) -> None:
    """Set ``data[path] = value`` by walking ``path[:-1]`` then assigning.

    Assumes the parent path already exists (callers only invoke this after a
    successful ``walk_get`` against ``new_map`` confirmed the path resolves).
    Indexing a ``CommentedMap`` / ``CommentedSeq`` here goes through the same
    ``__setitem__`` ruamel uses internally, so comments and key order on the
    parent node are preserved.

    The assert at the end guards future callers: if a path resolves to a
    scalar parent (e.g. ``walk_set({"a": 1}, ("a", "b"), "x")``), Python's
    raw ``int.__setitem__`` raises ``TypeError`` with no context. The assert
    fails earlier and names the offending sub-path. Inside the only current
    call site (``merge_preserve_secrets``), the precondition is honored
    structurally because every path comes from ``find_redacted_paths``
    walking the same on-disk shape we're writing back into.
    """
    node: Any = data
    for key in path[:-1]:
        node = node[key]
    if not isinstance(node, (dict, list)):
        raise AssertionError(
            f"walk_set requires a mapping/sequence parent at {path[:-1]}, got {type(node).__name__}"
        )
    # Re-bind through Any so mypy doesn't reject ``list[Any][str | int]`` —
    # the runtime dispatch is sound (a sequence parent only ever pairs with
    # an int leaf key in ``find_redacted_paths``'s output, and a mapping
    # parent accepts any str/int key), but mypy sees the narrowed union and
    # objects to the str branch.
    leaf: Any = node
    leaf[path[-1]] = value


def merge_preserve_secrets(
    new_map: CommentedMap,
    on_disk_map: CommentedMap,
    *,
    redacted_paths: list[tuple[str | int, ...]],
) -> None:
    """Preserve on-disk secret values when the user's submission left REDACTED untouched.

    For each redacted path: if the new value still equals ``REDACTED_TOMBSTONE``,
    the user did not type a new value — copy the on-disk value over. If the
    user typed a new value, leave it. If the user deleted the key entirely
    (no ``walk_get`` hit in ``new_map``), leave it deleted (intentional delete).

    ``redacted_paths`` is the output of :func:`find_redacted_paths` against
    ``on_disk_map`` (the only source of truth for "what was masked at display
    time"). Mutates ``new_map`` in place; ``on_disk_map`` is read-only here.

    Edge case — literal "REDACTED": if a user literally types the exact string
    ``"REDACTED"`` (the sentinel used by confuse + our safety-net regex), the
    merge will treat it as "unchanged" and revert to the on-disk value.
    Switching the comparison to ``is`` would NOT be a reliable fix — CPython
    string interning of short literals is an implementation detail, not a
    language guarantee, so two ``"REDACTED"`` strings can pass ``is`` and
    defeat the check. Workaround for the rare user who wants the literal
    string ``"REDACTED"`` as their actual credential: type it with a trailing
    space or any other distinguishing character.

    Edge case — null on-disk value: ``walk_get(on_disk_map, path)`` returns
    ``None`` for both "key absent" and "key explicitly ``null`` in YAML".
    Both collapse to "no restoration" here, which is the right outcome for
    credentials either way — preserving a literal YAML ``null`` over the
    user's intent-to-leave-blank would be more surprising than skipping it.
    """
    for path in redacted_paths:
        new_val = walk_get(new_map, path)
        if new_val == REDACTED_TOMBSTONE:
            on_val = walk_get(on_disk_map, path)
            if on_val is not None:
                walk_set(new_map, path, on_val)


def atomic_write(dst: Path, data: CommentedMap, yaml: YAML) -> None:
    """Atomic write with crash-safety on ext4.

    Sequence (per Dan Luu's *Files are hard* — danluu.com/file-consistency/ —
    and LWN's ext4-rename discussion — lwn.net/Articles/322823/): write a
    tempfile in the SAME directory as ``dst`` -> fsync the tempfile ->
    ``copymode`` from ``dst`` (mode bits only — see note below on why we do
    NOT use ``copystat``) -> ``os.replace`` -> fsync the PARENT DIRECTORY.
    Skipping the parent-dir fsync leaves a window where the rename can be
    lost on power-cut even on ext4 with delayed allocation tuned for it;
    the dir fsync forces the directory entry change durable.

    Why ``copymode`` and not ``copystat``: ``shutil.copystat`` copies mode
    bits, atime, mtime, AND extended attributes / flags. Preserving mtime
    is wrong here because the Save endpoint's freshness signal
    (:attr:`BeetsConfigSnapshot.apply_pending`) is driven by
    ``current_mtime > handle.file_mtime_at_load``; preserving the old
    mtime would mask "the user just saved" from "nothing changed" and the
    Apply button would never light up after a Save. ``copymode`` preserves
    the user's chosen permissions (e.g. ``0o600`` on credential-bearing
    files) without freezing the mtime — exactly the policy this slice
    wants.

    First-write fallback: when ``dst`` does not exist (defensive — production
    callers go through ``setup_beets()`` which always ensures the file),
    chmod the tempfile to ``0o644`` so the post-replace file isn't left at
    the umask-derived mode.

    The ``atomicwrites`` PyPI package is deprecated by its own author in
    favor of this recipe (github.com/untitaker/python-atomicwrites), so we
    roll it ourselves rather than pull in an unmaintained dep.
    """
    tmp = dst.parent / f".{dst.name}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(data, f)
            f.flush()
            os.fsync(f.fileno())

        if dst.exists():
            shutil.copymode(dst, tmp)
        else:
            os.chmod(tmp, 0o644)

        os.replace(tmp, dst)

        dir_fd = os.open(dst.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        # Best-effort cleanup if something raised mid-flight (a successful
        # os.replace already consumed the tempfile name, so this is a no-op
        # on the happy path).
        if tmp.exists():
            with suppress(OSError):
                tmp.unlink()


def save(handle: LibraryHandle, req: SaveRequest) -> BeetsConfigSnapshot:
    """Persist ``req.yaml_text`` to ``handle.config_path``, returning the new snapshot.

    Sequence (spec § "Layer 3 — Backend: Save flow"):

    1. **Parse** with ruamel — bad YAML -> HTTP 422 with ``problem_mark`` line/col.
    2. **Schema validate** via ``KnownKeysSchema`` — known-key errors -> 422 with
       per-error ``ValidationErrorItem`` payloads.
    3. **SHA-256 CAS** — compare ``req.base_sha256`` to the SHA-256 of the
       on-disk bytes. Mismatch -> 409 with ``current_yaml_text`` (raw on-disk
       file) and ``current_sha256`` so the frontend's merge view can render
       the diff. SHA-256 alone is the CAS token (no mtime check): nanosecond
       mtime ints overflow JavaScript's ``Number.MAX_SAFE_INTEGER`` and
       silently corrupt across the JSON wire — the SHA already covers every
       bytes-changed edit, including the rare ``os.utime`` "preserve mtime,
       change content" case (see ``test_save_409_on_sha_change``).
    4. **Secret-preserve merge** — re-parse the on-disk bytes (NOT ``beets.config``
       — we want the file's own redacted paths), discover redacted paths against
       that, then ``merge_preserve_secrets`` so any path the user left at
       ``REDACTED`` reverts to the on-disk value before the write.
    5. **Atomic write** via ``atomic_write`` — fsync + dir-fsync + ``copymode``.
       ``copymode`` (mode bits only) and NOT ``copystat`` — the helper
       intentionally lets atime/mtime advance so step 6's freshness signal
       fires.
    6. **Return new snapshot** — ``apply_pending`` will be ``True`` because the
       mtime advanced past ``handle.file_mtime_at_load`` (this is the load-bearing
       reason ``atomic_write`` uses ``copymode`` instead of ``copystat`` — the
       latter would freeze mtime and the Apply button would never light up);
       the Apply endpoint (Task 8) is what clears it.
    """
    yaml = _yaml()

    # 1. Parse with ruamel.
    try:
        new_map = parse_yaml(req.yaml_text)
    except YAMLError as exc:
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

    # 2. Schema validate.
    errors = validate_known_keys(new_map)
    if errors:
        raise HTTPException(
            status_code=422,
            detail=[item.model_dump() for item in errors],
        )

    # 3. SHA-256 CAS. Read the bytes once and reuse them for both the hash
    # and the (possible) 409 payload + the secret-preserve re-parse below.
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

    # 4. Secret-preserve merge. ``find_redacted_paths`` walks the on-disk
    # CommentedMap so paths line up with what the user saw via the GET snapshot.
    # The widening to ``list[tuple[str | int, ...]]`` is a no-op at runtime —
    # ``find_redacted_paths`` only ever emits string keys (per its docstring,
    # list/dict descent does not extend the path with an index) — but mypy's
    # list invariance won't let ``list[tuple[str, ...]]`` flow into
    # ``merge_preserve_secrets``'s ``list[tuple[str | int, ...]]`` parameter
    # directly, so we copy through a comprehension.
    on_disk_map = parse_yaml(on_disk_bytes.decode("utf-8"))
    redacted: list[tuple[str | int, ...]] = [tuple(p) for p in find_redacted_paths(on_disk_map)]
    merge_preserve_secrets(new_map, on_disk_map, redacted_paths=redacted)

    # 5. Atomic write.
    atomic_write(handle.config_path, new_map, yaml)

    # 6. Return the new snapshot. apply_pending will be True because mtime
    # advanced past handle.file_mtime_at_load — Task 8's Apply endpoint clears it.
    return build_config_snapshot(handle)


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

    1. **Import gate** — refuse with 409 if an import is currently active.
       Reset_beets_globals tears down the SQLite connection the import worker
       holds; doing that mid-import would corrupt the in-flight ImportSession.
       ``get_registry()`` (not a module-import) reads the LIVE registry
       binding — ``conftest.reset_import_registry`` swaps it between tests.
    2. **Per-app lock** — serialise concurrent Apply requests. Two threads
       racing through ``reset_beets_globals`` + ``setup_beets`` would leave
       ``app.state.beets_library`` non-deterministic and could close the
       library twice (``sqlite3.ProgrammingError``).
    3. **Threadpool rebuild** — beets setup is blocking I/O (filesystem +
       SQLite); ``run_in_threadpool`` hands it to FastAPI's worker pool so
       the event loop stays responsive. Any exception from the rebuild
       maps to 500 with a ``recovery`` hint — the user's saved config is
       on disk, so a restart is always the safe recovery path.
    4. **Atomic swap** — only after the rebuild succeeds, replace
       ``app.state.beets_library``. On a 500 the old handle stays in place
       and the process keeps serving with the previously-loaded config.
    5. **Return snapshot** — ``apply_pending`` will be ``False`` because the
       new handle's ``file_mtime_at_load`` captured the current on-disk
       mtime during ``setup_beets``.
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
    if (
        get_registry().has_active_job()
        or lyrics_backfill_active()
        or artist_art_backfill_active()
        or reorganize_backfill_active()
    ):
        raise HTTPException(
            status_code=409,
            detail="Import in progress — Apply available when it finishes / lyrics backfill",
        )

    async with _swap_lock(app):
        old: LibraryHandle = app.state.beets_library
        settings = _settings(app)
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

    return build_config_snapshot(new)
