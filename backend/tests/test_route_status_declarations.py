"""Guard: every HTTP status a route can raise is declared in the LIVE OpenAPI spec.

Why this exists on top of SonarQube ``python:S8415`` (and the audit script that
drove the two commits before it): both recognise only INTEGER LITERALS in
``raise HTTPException(...)``. Every status spelled ``status.HTTP_409_CONFLICT``
was invisible to them, so ``POST /api/import`` reported "0 issues" while its 409
- which ``frontend/src/api/useImport.ts`` branches on - was missing from the
contract entirely. A generated client with no type for a body it reads is the
exact defect those commits exist to remove.

What this test resolves, and therefore what it can catch:

- statuses written as an INT (``HTTPException(404, ...)``) *and* as an ATTRIBUTE
  (``status.HTTP_404_NOT_FOUND``, ``http_status.HTTP_409_CONFLICT``);
- raises in the endpoint function itself, in same-module helper functions the
  endpoint calls (``_gate``, ``_child_or_404``, ``ensure_import_can_start``, ...),
  transitively;
- raises in the shared adapter modules named in ``_FOLLOWED_MODULES``, whose
  raises are the ones most easily forgotten because they are not in the route
  file at all (``install_cover_op``, ``apply_album_edit_op``,
  ``resolve_duplicates_op``, ``apply_artist_rename_op``,
  ``raise_if_library_busy``).

It is built against ``app.openapi()`` - the LIVE spec - not the tracked
``frontend/openapi.json``, so it cannot pass on a stale artefact.

KNOWN BLIND SPOT, stated rather than hidden: the call graph is followed only
into the route's own module and ``_FOLLOWED_MODULES``. These beets-adapter
modules raise ``HTTPException`` too and are NOT followed yet -
``app/beets/delete.py``, ``app/beets/config_editor.py``, ``app/beets/lyrics.py``.
Adding one to ``_FOLLOWED_MODULES`` is how this guard grows. Measured with all
of them added, exactly NINE (operation, status) pairs are still undeclared, and
nothing becomes unresolvable:

    DELETE /api/albums/{album_id}          404, 409
    DELETE /api/artists                    409
    POST   /api/albums/{album_id}/lyrics/fetch  404, 409
    POST   /api/config/apply               409, 500 (the nested shape)
    POST   /api/config/naming/save         409
    POST   /api/config/save                409

They are left for the follow-up that declares them rather than pre-emptively
allowlisted, because an allowlist entry reads as a decision and this is a
backlog. Statuses raised by a ``Depends(...)`` dependency, or by a helper that
RETURNS an ``HTTPException`` for the caller to raise
(``app/beets/delete.py::_failed``, the delete route's structured 500), are out
of scope in any configuration - the scan only reads ``raise HTTPException(...)``.

This guard checks that a status is PRESENT, not that its body schema is right.
A status declared with the wrong model (``ErrorDetail`` for a nested
``{message, recovery}`` 500, say) passes here; ``app/models/errors.py`` is the
rule for that half.
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
from functools import cache
from typing import Final, NamedTuple

from fastapi import routing
from fastapi.routing import APIRoute

from app.main import app

#: Shared helper modules whose raises count as the calling route's raises.
_FOLLOWED_MODULES: Final = frozenset(
    {
        "app.library_busy",
        "app.beets.cover",
        "app.beets.edit",
        "app.beets.duplicates",
        "app.beets.rename",
    }
)

#: ``status.HTTP_409_CONFLICT`` -> 409. The name is the contract, not the value
#: of the attribute at import time, so the scan never has to import ``status``.
_STATUS_ATTR: Final = re.compile(r"^HTTP_(\d{3})_")

#: Exceptions we treat as "this route can answer with that status".
_RAISED_EXCEPTION_NAMES: Final = frozenset({"HTTPException"})

#: Handler types that swallow an ``HTTPException`` raised in the guarded body.
_SWALLOWING_HANDLERS: Final = frozenset({"HTTPException", "Exception", "BaseException"})

#: ``(method, path, status) -> reason``. An entry here is a status the scan sees
#: a route raise but which is deliberately NOT required in the contract. Keep it
#: empty unless a case is genuinely undecidable, and always carry the reason.
_ALLOWLIST: Final[dict[tuple[str, str, int], str]] = {}

#: Non-vacuity floors: a scan that walks nothing would pass every assertion
#: below for the wrong reason. Bumped only downward-safely (these are minima).
_MIN_ROUTES_SCANNED: Final = 100
_MIN_ROUTES_WITH_RAISES: Final = 50


class _ModuleIndex:
    """The parsed AST of one module, indexed for name resolution."""

    def __init__(self, module_name: str, tree: ast.Module) -> None:
        self.module_name = module_name
        #: every ``def``/``async def`` in the file, keyed by name. Nested defs
        #: are included so a call to a closure resolves; a name defined twice
        #: keeps the first, which is the module-level one in every file here.
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        #: ``local name -> (source module, original name)`` for every
        #: ``from X import y``, including the ones inside function bodies (the
        #: library-busy helpers are imported lazily on purpose).
        self.imported: dict[str, tuple[str, str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                self.functions.setdefault(node.name, node)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                for alias in node.names:
                    self.imported.setdefault(alias.asname or alias.name, (node.module, alias.name))


@cache
def _module_index(module_name: str) -> _ModuleIndex | None:
    """Parse ``module_name`` from disk, or ``None`` if it has no source file."""
    module = sys.modules.get(module_name)
    source_file = getattr(module, "__file__", None) if module is not None else None
    if source_file is None:
        return None
    with open(source_file, encoding="utf-8") as handle:
        return _ModuleIndex(module_name, ast.parse(handle.read()))


def _called_names(node: ast.AST) -> set[str]:
    """Every simple name called in ``node`` (``f(...)`` and ``mod.f(...)``)."""
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            # ``cover.install_cover_op(...)`` - the attribute is the function
            # name; the receiver is checked against the import table below.
            names.add(func.attr)
    return names


def _resolve_call(index: _ModuleIndex, name: str) -> tuple[str, str] | None:
    """Resolve a called ``name`` to ``(module, function)``, or ``None`` to stop.

    Followed: a function defined in the same module, or one imported from a
    module in ``_FOLLOWED_MODULES``. Everything else deliberately stops the
    walk - see the module docstring's blind-spot note.
    """
    origin = index.imported.get(name)
    if origin is not None:
        source_module, original = origin
        if source_module in _FOLLOWED_MODULES:
            return source_module, original
        return None
    if name in index.functions:
        return index.module_name, name
    return None


def _status_of(raise_node: ast.Raise) -> int | str | None:
    """The status a ``raise HTTPException(...)`` answers with.

    Returns the int, or a human-readable marker string when the status cannot be
    decided statically (which the test reports as a failure rather than
    silently dropping), or ``None`` when the node is not a raise we track.
    """
    exc = raise_node.exc
    if not isinstance(exc, ast.Call):
        return None
    func = exc.func
    called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if called not in _RAISED_EXCEPTION_NAMES:
        return None
    arg: ast.expr | None = exc.args[0] if exc.args else None
    for keyword in exc.keywords:
        if keyword.arg == "status_code":
            arg = keyword.value
    if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
        return arg.value
    if isinstance(arg, ast.Attribute):
        match = _STATUS_ATTR.match(arg.attr)
        if match:
            return int(match.group(1))
    return f"unresolved status expression at line {raise_node.lineno}"


def _handler_swallows(handler: ast.ExceptHandler) -> bool:
    """True if ``handler`` catches an ``HTTPException`` raised in the try body.

    A bare ``except:`` or one naming ``Exception``/``BaseException``/
    ``HTTPException`` does - unless it re-raises, in which case the status still
    leaves the process.
    """
    caught = handler.type
    names: list[str] = []
    if caught is None:
        names = ["BaseException"]
    else:
        parts = caught.elts if isinstance(caught, ast.Tuple) else [caught]
        for part in parts:
            if isinstance(part, ast.Name):
                names.append(part.id)
            elif isinstance(part, ast.Attribute):
                names.append(part.attr)
    if not any(name in _SWALLOWING_HANDLERS for name in names):
        return False
    return not any(
        isinstance(node, ast.Raise)
        for node in ast.walk(ast.Module(body=handler.body, type_ignores=[]))
    )


def _suppressed_raises(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[int]:
    """Line numbers of raises that a surrounding ``try`` always catches.

    Only the ``body``/``orelse`` of a ``try`` are guarded: an exception raised
    inside an ``except`` clause is NOT caught by that statement's sibling
    handlers, which is exactly how ``install_cover_op`` maps four adapter
    errors onto four different statuses.
    """
    suppressed: set[int] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Try):
            continue
        if not any(_handler_swallows(handler) for handler in node.handlers):
            continue
        guarded = ast.Module(body=[*node.body, *node.orelse], type_ignores=[])
        for inner in ast.walk(guarded):
            if isinstance(inner, ast.Raise):
                suppressed.add(inner.lineno)
    return suppressed


def _raised_statuses(module_name: str, function_name: str) -> tuple[set[int], list[str]]:
    """Every status reachable from ``module.function``, plus undecidable raises."""
    statuses: set[int] = set()
    unresolved: list[str] = []
    seen: set[tuple[str, str]] = set()
    pending: list[tuple[str, str]] = [(module_name, function_name)]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        index = _module_index(current[0])
        if index is None:
            continue
        func = index.functions.get(current[1])
        if func is None:
            continue
        skip = _suppressed_raises(func)
        for node in ast.walk(func):
            if not isinstance(node, ast.Raise) or node.lineno in skip:
                continue
            status = _status_of(node)
            if isinstance(status, int):
                statuses.add(status)
            elif isinstance(status, str):
                unresolved.append(f"{current[0]}.{current[1]}: {status}")
        for name in _called_names(func):
            target = _resolve_call(index, name)
            if target is not None and target not in seen:
                pending.append(target)
    return statuses, unresolved


class _Operation(NamedTuple):
    """One (method, path) pair in the schema, joined to its handler function."""

    method: str
    path: str
    module: str
    function: str


def _schema_operations() -> list[_Operation]:
    """Every API operation that reaches the generated schema.

    Enumerated through ``iter_route_contexts`` - the same flattening
    ``get_openapi`` itself walks - because ``app.routes`` holds one opaque
    ``_IncludedRouter`` per ``include_router`` call, not the routes.

    ``app/static_files.py`` declares ``responses={404: ...}`` on an
    ``include_in_schema=False`` route: that declaration is INERT (it never
    reaches the spec), so the route is excluded here rather than counted as
    covered.
    """
    operations: list[_Operation] = []
    for context in routing.iter_route_contexts(app.routes):
        if not isinstance(context.original_route, APIRoute):
            continue
        if not context.include_in_schema:
            continue
        endpoint = context.endpoint
        path = context.path_format
        if endpoint is None or path is None:  # pragma: no cover - never today
            continue
        handler = inspect.unwrap(endpoint)
        operations.extend(
            _Operation(method, path, handler.__module__, handler.__name__)
            for method in sorted(context.methods or ())
        )
    return operations


def _declared_statuses(spec: dict[str, object], path: str, method: str) -> set[int]:
    paths = spec.get("paths")
    assert isinstance(paths, dict), "the live spec must carry a paths object"
    operation = paths.get(path, {}).get(method.lower(), {})
    responses = operation.get("responses", {}) if isinstance(operation, dict) else {}
    return {int(code) for code in responses if str(code).isdigit()}


def _gaps() -> tuple[list[str], list[str], int, int]:
    """``(gap lines, unresolved lines, operations scanned, ones that raise)``."""
    spec = app.openapi()
    gaps: list[str] = []
    unresolved: list[str] = []
    operations = _schema_operations()
    with_raises = 0
    for method, path, module_name, function_name in operations:
        raised, route_unresolved = _raised_statuses(module_name, function_name)
        if raised:
            with_raises += 1
        unresolved.extend(f"{method} {path}: {line}" for line in route_unresolved)
        declared = _declared_statuses(spec, path, method)
        for status in sorted(raised - declared):
            if (method, path, status) in _ALLOWLIST:
                continue
            gaps.append(f"{method} {path}: {status} raised but not declared")
    return gaps, unresolved, len(operations), with_raises


def test_every_status_a_route_can_raise_is_declared_in_the_live_spec() -> None:
    gaps, unresolved, scanned, with_raises = _gaps()
    assert scanned >= _MIN_ROUTES_SCANNED, (
        f"only {scanned} operations were scanned; the walk found nothing to check"
    )
    assert with_raises >= _MIN_ROUTES_WITH_RAISES, (
        f"only {with_raises} operations were seen to raise anything; the AST scan is broken"
    )
    assert not unresolved, (
        "a raise whose status cannot be resolved statically - give it an int or a"
        " status.HTTP_* attribute, or add an _ALLOWLIST entry:\n  " + "\n  ".join(unresolved)
    )
    assert not gaps, (
        "these routes raise a status the generated client has no type for"
        " (see app/models/errors.py for how to declare it):\n  " + "\n  ".join(sorted(gaps))
    )


def test_the_scan_resolves_the_attribute_spelling_sonar_cannot_see() -> None:
    """Anti-vacuity: the whole point is ``status.HTTP_409_CONFLICT``, not ``409``.

    ``POST /api/import`` raises its 409 through ``status.HTTP_409_CONFLICT``
    (both in ``ensure_import_can_start`` and at the ``reg.start`` call site).
    If the resolver ever regresses to integer literals only, the guard above
    goes quietly green; this fails instead.
    """
    raised, unresolved = _raised_statuses("app.api.import_", "start_import")
    assert not unresolved
    assert 409 in raised, "the attribute spelling of a status must resolve"
    assert 422 in raised, "the integer spelling of a status must still resolve"


def _suppressed_statuses(source: str) -> set[int]:
    """The statuses ``_suppressed_raises`` drops from one function's source."""
    func = ast.parse(source).body[0]
    assert isinstance(func, ast.FunctionDef)
    skip = _suppressed_raises(func)
    return {
        status
        for node in ast.walk(func)
        if isinstance(node, ast.Raise)
        and node.lineno in skip
        and isinstance(status := _status_of(node), int)
    }


def test_a_raise_the_surrounding_try_always_catches_is_not_reported() -> None:
    """False-positive control, exercised on synthetic source on purpose.

    No route in the app has this shape today, so the rule has nothing real to
    bite on; without this test the branch would be unproven the day one appears.
    A status that never leaves the process must not be demanded of the contract.
    """
    assert _suppressed_statuses(
        "def f():\n"
        "    try:\n"
        "        raise HTTPException(status_code=418, detail='never escapes')\n"
        "    except Exception:\n"
        "        return None\n"
    ) == {418}


def test_a_raise_inside_an_except_clause_is_still_reported() -> None:
    """The other half of the rule, and the one the app really depends on.

    ``install_cover_op`` maps four adapter errors onto four statuses from four
    ``except`` clauses of ONE ``try`` whose last handler is ``except Exception``.
    Treating that handler as swallowing its siblings would erase 404/415/422/500
    from the contract in a single stroke.
    """
    assert (
        _suppressed_statuses(
            "def f():\n"
            "    try:\n"
            "        work()\n"
            "    except KeyError as exc:\n"
            "        raise HTTPException(status_code=404, detail=str(exc)) from exc\n"
            "    except Exception as exc:\n"
            "        raise HTTPException(status_code=500, detail=str(exc)) from exc\n"
        )
        == set()
    )


def test_a_catching_handler_that_re_raises_does_not_suppress() -> None:
    """``except Exception: raise`` re-emits the status, so it still escapes."""
    assert (
        _suppressed_statuses(
            "def f():\n"
            "    try:\n"
            "        raise HTTPException(status_code=409, detail='escapes')\n"
            "    except Exception:\n"
            "        raise\n"
        )
        == set()
    )


def test_the_scan_follows_shared_helper_modules() -> None:
    """Anti-vacuity: ``DELETE /api/trash/all`` raises nothing in its own body.

    Its 409 comes from ``_gate`` -> ``app.library_busy.raise_if_library_busy``,
    two modules away. A scan that stopped at the endpoint function would see an
    empty set and pass.
    """
    raised, unresolved = _raised_statuses("app.api.trash", "empty_trash_all")
    assert not unresolved
    assert 409 in raised, "a raise two call hops away, in a shared module, must be seen"
