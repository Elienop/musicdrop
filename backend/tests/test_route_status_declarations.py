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
  ``start_album_lyrics_op``, ``missing_report_op``, ``delete_album_op``,
  ``config_editor.save``, ``raise_if_library_busy``).

It is built against ``app.openapi()`` - the LIVE spec - not the tracked
``frontend/openapi.json``, so it cannot pass on a stale artefact.

COVERAGE, re-measured 2026-08-27 by running this test rather than by hand:
``_FOLLOWED_MODULES`` now names EVERY module outside ``app/api`` that raises an
``HTTPException`` on a route's behalf - the nine there plus the route modules
themselves are the whole set that ``grep -rln 'raise HTTPException' app/``
returns, with one exception noted below. Nothing is unresolvable, nothing is
unscannable, and the gap list is EMPTY. There is no backlog of un-followed
adapters left, so there is no allowlist and nothing deferred; the way to keep it
that way is to add any NEW shared module that raises on a route's behalf to
``_FOLLOWED_MODULES`` in the same commit that creates it.

The one grep hit that is not followed is ``app/static_files.py``: its two 404s
sit on an ``include_in_schema=False`` route, which ``_schema_operations``
excludes because a ``responses`` entry there never reaches the spec at all.

WHAT THE SCAN STILL STRUCTURALLY CANNOT SEE. These are not a backlog - no
setting of ``_FOLLOWED_MODULES`` reaches them, because the scan reads
``raise HTTPException(...)`` and nothing else. Both are declared in the contract
today by hand; both would go silently undeclared if that hand-declaration were
removed, so they are written down rather than trusted to memory:

- a helper that RETURNS an ``HTTPException`` for its caller to raise.
  ``app/beets/delete.py::_failed`` (the delete routes' structured 500) is the
  only one in the repo - a ``grep -rn`` over ``app/`` for both
  ``return HTTPException`` and ``-> HTTPException`` finds nothing else - and
  the walk sees only ``raise _failed(exc)``,
  whose status is not a literal anywhere in the raise.
- a bodiless response constructed and RETURNED rather than raised.
  ``app/api/http_cache.py:64::not_modified`` builds the ``304`` that every
  conditional image GET answers with; a returned ``Response`` is not a raise, so
  no ``304`` can ever appear in this scan's status set.

A third shape is in scope but inert today: a status raised inside a
``Depends(...)`` dependency would be missed, since the walk starts at the
endpoint function and never enters its parameter defaults. Checked at the same
measurement - none of the seventeen dependencies this app injects raises an
``HTTPException``, so nothing is hiding there right now.

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
        "app.beets.lyrics",
        "app.beets.completeness",
        "app.beets.config_editor",
        "app.beets.delete",
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

#: The keys of an OpenAPI path item that name an operation. Everything else a
#: path item may legally carry (``parameters``, ``summary``, ``servers``,
#: ``$ref``) is not an operation and must not be read as one. FastAPI writes
#: only ``route.methods`` lowercased today, but the filter is what makes the
#: walk-vs-spec comparison below sound rather than lucky.
_HTTP_METHODS: Final = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)

#: Non-vacuity floor on how many operations the AST scan sees raise ANYTHING.
#: There is no external ground truth for this one (unlike the operation set,
#: which is checked against the spec itself), so it stays a minimum: 63 today.
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


class _ScanResult(NamedTuple):
    """What one call-graph walk found.

    ``unresolved`` and ``unscannable`` are both FAILURES, kept apart only so the
    report says which kind: ``unresolved`` is a raise whose status is not a
    static int, ``unscannable`` is a function body the walk could not read at
    all - which would otherwise make the operation contribute zero raises and
    report zero gaps, silently.
    """

    statuses: set[int]
    unresolved: list[str]
    unscannable: list[str]


def _raised_statuses(module_name: str, function_name: str) -> _ScanResult:
    """Every status reachable from ``module.function``, plus what it could not read."""
    statuses: set[int] = set()
    unresolved: list[str] = []
    unscannable: list[str] = []
    entry = (module_name, function_name)
    seen: set[tuple[str, str]] = set()
    pending: list[tuple[str, str]] = [entry]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        index = _module_index(current[0])
        if index is None:
            # The module is absent from ``sys.modules`` or has no source file,
            # so every raise in it is invisible. Several ``app/beets/*``
            # modules are imported lazily inside functions, which is exactly
            # how a followed module could stop being loaded at collection time.
            unscannable.append(f"{current[0]}.{current[1]}: module has no readable source")
            continue
        func = index.functions.get(current[1])
        if func is None:
            if current == entry:
                unscannable.append(
                    f"{current[0]}.{current[1]}: the handler is not a def in that module"
                )
            # Otherwise: a followed import that names a class or a constant
            # rather than a function. There is no body to walk and nothing was
            # lost, so this is not a resolution failure.
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
    return _ScanResult(statuses, unresolved, unscannable)


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


def _spec_operations(spec: dict[str, object]) -> list[tuple[str, str]]:
    """Every ``(METHOD, path)`` the LIVE spec exposes, from the spec itself.

    This is the ground truth the walk is measured against. ``get_openapi``
    writes one key per entry in ``route.methods`` (lowercased) under each
    ``path_format``, so a walk that sees the same routes must produce exactly
    this set - no hand-picked minimum, which both rots and under-protects.
    """
    paths = spec.get("paths")
    assert isinstance(paths, dict), "the live spec must carry a paths object"
    return sorted(
        (method.upper(), path)
        for path, item in paths.items()
        for method in item
        if method in _HTTP_METHODS
    )


class _GapReport(NamedTuple):
    """Everything one full pass over the app found, ready to assert on."""

    gaps: list[str]
    unresolved: list[str]
    unscannable: list[str]
    #: ``(METHOD, path)`` the AST walk covered, and the same from the spec.
    walked: list[tuple[str, str]]
    in_spec: list[tuple[str, str]]
    with_raises: int


def _gaps() -> _GapReport:
    """Scan every operation in the app and join it to the live spec."""
    spec = app.openapi()
    gaps: list[str] = []
    unresolved: list[str] = []
    unscannable: list[str] = []
    operations = _schema_operations()
    with_raises = 0
    for method, path, module_name, function_name in operations:
        result = _raised_statuses(module_name, function_name)
        if result.statuses:
            with_raises += 1
        unresolved.extend(f"{method} {path}: {line}" for line in result.unresolved)
        unscannable.extend(f"{method} {path}: {line}" for line in result.unscannable)
        declared = _declared_statuses(spec, path, method)
        for status in sorted(result.statuses - declared):
            if (method, path, status) in _ALLOWLIST:
                continue
            gaps.append(f"{method} {path}: {status} raised but not declared")
    walked = sorted((operation.method, operation.path) for operation in operations)
    return _GapReport(gaps, unresolved, unscannable, walked, _spec_operations(spec), with_raises)


def test_every_status_a_route_can_raise_is_declared_in_the_live_spec() -> None:
    report = _gaps()
    # Non-vacuity, and the tightest form available: the walk must cover exactly
    # the operations the spec itself contains. A router that drops out of the
    # walk (as every router did when FastAPI 0.141 turned ``app.routes`` into
    # ``_IncludedRouter`` objects) reports zero gaps for every route in it,
    # which is the failure mode a floor of "at least N" cannot see.
    assert report.walked == report.in_spec, (
        "the AST walk and the live spec disagree about which operations exist,"
        " so some route is being checked against nothing:\n  walked but not in the spec: "
        + str(sorted(set(report.walked) - set(report.in_spec)))
        + "\n  in the spec but not walked: "
        + str(sorted(set(report.in_spec) - set(report.walked)))
    )
    assert report.with_raises >= _MIN_ROUTES_WITH_RAISES, (
        f"only {report.with_raises} operations were seen to raise anything; the AST scan is broken"
    )
    assert not report.unscannable, (
        "the scan could not read the body of a handler it was asked to walk, so that"
        " operation contributed no raises and would report no gaps - resolve it or"
        " import the module eagerly:\n  " + "\n  ".join(sorted(report.unscannable))
    )
    assert not report.unresolved, (
        "a raise whose status cannot be resolved statically - give it an int or a"
        " status.HTTP_* attribute, or add an _ALLOWLIST entry:\n  " + "\n  ".join(report.unresolved)
    )
    assert not report.gaps, (
        "these routes raise a status the generated client has no type for"
        " (see app/models/errors.py for how to declare it):\n  " + "\n  ".join(sorted(report.gaps))
    )


def test_the_scan_resolves_the_attribute_spelling_sonar_cannot_see() -> None:
    """Anti-vacuity: the whole point is ``status.HTTP_409_CONFLICT``, not ``409``.

    ``POST /api/import`` raises its 409 through ``status.HTTP_409_CONFLICT``
    (both in ``ensure_import_can_start`` and at the ``reg.start`` call site).
    If the resolver ever regresses to integer literals only, the guard above
    goes quietly green; this fails instead.
    """
    result = _raised_statuses("app.api.import_", "start_import")
    assert not result.unresolved
    assert not result.unscannable
    assert 409 in result.statuses, "the attribute spelling of a status must resolve"
    assert 422 in result.statuses, "the integer spelling of a status must still resolve"


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
    """The other half of the rule: only ``body``/``orelse`` are guarded.

    ``_suppressed_raises`` is a TWO-condition guard - some handler must swallow
    (``_handler_swallows``) AND the raise must sit in the try's body/orelse. A
    fixture whose only swallowing-looking handler re-raises fails the first
    condition, so the walk never reaches the second and the rule under test is
    not exercised at all: the previous version of this test passed unchanged
    when ``guarded`` was widened to include handler bodies. So the fixture below
    satisfies the OTHER condition on purpose - ``except Exception: return None``
    swallows and does not re-raise - leaving the body/orelse rule as the only
    thing deciding the outcome. Widen ``guarded`` to include handler bodies and
    this returns ``{404}``.

    Blast radius, measured rather than asserted: this bug ALONE does not erase
    ``install_cover_op``'s 404/415/422/500. All four live in ``except`` clauses
    of a ``try`` whose last handler re-raises, so ``_handler_swallows`` is False
    for every handler and the statement is skipped before ``guarded`` is built.
    Erasing them needs this bug AND a broken re-raise check together. The rule
    still has to hold on its own: sibling handlers do not catch each other, and
    the day a swallowing handler joins that ``try``, this is what keeps the four
    statuses in the contract.
    """
    assert (
        _suppressed_statuses(
            "def f():\n"
            "    try:\n"
            "        work()\n"
            "    except KeyError as exc:\n"
            "        raise HTTPException(status_code=404, detail=str(exc)) from exc\n"
            "    except Exception:\n"
            "        return None\n"
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
    result = _raised_statuses("app.api.trash", "empty_trash_all")
    assert not result.unresolved
    assert not result.unscannable
    assert 409 in result.statuses, "a raise two call hops away, in a shared module, must be seen"
