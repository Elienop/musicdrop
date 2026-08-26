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
- raises in the modules named in ``_FOLLOWED_MODULES``, whose raises are the
  ones most easily forgotten because they are not in the route file at all
  (``install_cover_op``, ``apply_album_edit_op``, ``resolve_duplicates_op``,
  ``apply_artist_rename_op``, ``start_album_lyrics_op``, ``missing_report_op``,
  ``delete_album_op``, ``config_editor.save``, ``raise_if_library_busy``, and
  ``ensure_import_can_start`` - which is a same-module helper for
  ``POST /api/import`` but a cross-module one for the acquisition routes).

It is built against ``app.openapi()`` - the LIVE spec - not the tracked
``frontend/openapi.json``, so it cannot pass on a stale artefact.

WHAT ``walked == in_spec`` DOES AND DOES NOT PIN. Both sides of that equality
come from the SAME generator: ``_schema_operations`` calls
``routing.iter_route_contexts(app.routes)``, and so does FastAPI's
``get_openapi``. So it is not an independent census of the app's routes - it is
a check on THIS TEST'S OWN filtering of that generator's output. That is still
the regression it was written for and it does catch it: FastAPI 0.141 turned
``app.routes`` into ``_IncludedRouter`` objects, the old ``isinstance(...,
APIRoute)`` filter matched nothing, and the walk silently went to zero while the
spec stayed at 112. Measured on this tree: a mutant that drops the
``/api/albums`` routes from ``_schema_operations`` alone fails with
``walked 100 != in_spec 112``.

What it CANNOT catch is a collapse in ``iter_route_contexts`` itself, or
anything upstream of it, because both sides then lose the same operations
together. Measured: patching that one generator to skip every ``/api/albums``
route gives ``walked == in_spec == 100`` with an empty gap list - an entire
router gone and this test green. A router deleted from ``app/main.py`` is the
same shape, and no IN-PROCESS ENUMERATION can see it: the routes genuinely do
not exist. That is what THE SOURCE CENSUS below is for - it reads the
filesystem, where the module is still sitting with its decorators on it.

MEASURED BLAST RADIUS OF THAT COLLAPSE, per ROUTER (2026-08-29, by patching
``fastapi.routing.iter_route_contexts`` to drop every route whose endpoint is
defined in one ``app/api`` module - the routes one ``include_router`` call
registers - then clearing ``app.openapi_schema`` and re-running ``_gaps()``).
Every one of the nineteen drops left ``walked == in_spec`` True and the gap list
empty, which is why a count of raising operations was tried here first:

    playlists 70->56, artists 60, albums 61, import_ 61, bank 64, reorganize 66,
    config_ 67, plex 67, trash 67, acquisition 68, disk_sync 68, duplicates 68,
    events 69, lyrics 69, slskd 69,
    browse 70, health 70, search 70, stats 70 (no operation in these four raises
    anything, so no count can notice them going missing).

The earlier note here claimed dropping ``/api/albums`` took 70 -> 59. That was a
PATH-prefix drop, which is a different (larger) set than a router: several
``/api/albums/...`` paths are registered by the lyrics, completeness and cover
routes' own routers. Per router the worst case is 56 and the best non-zero one
is 69, so a floor that fires for the smallest router had to sit at exactly
today's count - and even there, four of the nineteen were uncoverable.

Deriving one side from the tracked ``frontend/openapi.json`` was considered and
rejected: that file is this same app's output (``scripts/dump_openapi.py``
writes ``app.openapi()``), and ``tests/test_openapi_spec_guard.py`` already
asserts it equals the live spec. It would flag a source-level collapse only
until the mandatory regeneration step ran, after which the lost paths would be
gone from the tracked file too and this test would go green again - the same
signal ``test_openapi_spec_guard`` already owns, dying to the same command,
while making THIS guard fail on every legitimate mid-work spec edit. It moves
the coupling; it does not remove it.

THE SOURCE CENSUS is the backstop against a source-level collapse, and it
replaced the count-based floor ``_MIN_ROUTES_WITH_RAISES = 70`` that used to
stand here. ``_source_census()`` walks ``app/**/*.py`` ON DISK and classifies
every module two ways - does it decorate a function ``@router.<method>(...)``,
and does it raise an ``HTTPException`` that escapes - then two tests require the
walk to reproduce each set exactly:

- ``test_every_module_that_declares_routes_still_reaches_the_walk`` - every
  route module on disk must contribute an operation to the schema. This is the
  only assertion here that can see ``browse``, ``health``, ``search`` or
  ``stats`` vanish; they raise nothing, so no count of raising operations moves
  when they do.
- ``test_every_module_that_raises_on_a_routes_behalf_is_still_in_scan_range`` -
  every module that raises on disk must be a module some operation's call graph
  actually REACHED a raise in.

A filesystem listing is an independent oracle of the route walk in a way the
spec is not: ``walked == in_spec`` compares two views of ONE generator, so a
router lost inside that generator disappears from both sides at once, whereas it
cannot disappear from a directory.

WHY THE COUNT HAD TO GO. Its comment claimed the metric "moves only when an
operation stops raising anything at all", so moving a raise between an endpoint
and a helper could not disturb it. That is TRUE only while the helper is
same-module or already in ``_FOLLOWED_MODULES``. MEASURED, by dropping each
followed module from the scan's reach in turn - behaviourally identical to
renaming that module, or extracting a raising helper into a NEW one, and
forgetting to relist it - ``with_raises`` fell to 67-70 with an EMPTY gap list
each time, and census 2 named the lost module in nine cases out of ten::

    dropped from _FOLLOWED_MODULES   with_raises  gaps  old floor(70)  census 2
    app.beets.config_editor                   67     0  RED (unnamed)  RED (named)
    app.beets.delete/duplicates/edit/rename   68     0  RED (unnamed)  RED (named)
    app.beets.completeness/cover/lyrics       69     0  RED (unnamed)  RED (named)
    app.library_busy                          69     0  RED (unnamed)  RED (named)
    app.api.import_                           70     0  GREEN          GREEN

So renaming ``app/beets/completeness.py`` to ``missing.py`` - a pure module
move, no contract change - turned the suite red at 69 with nothing in the gap
list, and the floor's own message offered "an operation legitimately stopped
raising, in which case re-measure and lower the constant deliberately". Taking
that advice would have blinded this scan to every status that module raises on a
route's behalf, silently and forever; for ``config_editor`` it was three
operations at once. The census cannot be paid off that way - the renamed file is
still on disk, still raising, and stays in the expected set until it is genuinely
back in reach.

WHAT THE CENSUS DELIBERATELY DOES NOT FAIL ON. Raises that move WITHIN reach:
both sides use the same ``_status_of``/``_suppressed_raises`` rules, so a raise
pushed from an endpoint into a same-module helper, or merged into a shared guard
in ``app.library_busy`` or any other followed module, drops the module it left
out of the expected set and the reached set together. MEASURED, by actually
performing that refactor (``app/api/lyrics.py``'s six 409 guards merged into one
``raise_if_lyrics_backfill_blocked`` in ``app.library_busy``, 35 lyrics tests
still green): ``app.api.lyrics`` left BOTH sides of census 2 at once, 21 == 21,
census 1 unchanged, gap list empty, and ``POST /api/lyrics/backfill`` still seen
to raise 409 through the new helper. The count-based floor also survived that
one, at 70 - it is only the OUT-of-reach moves above that it read as a contract
change.

ITS ONE GRANULARITY LIMIT, stated rather than hidden and measured rather than
feared: the census is PER MODULE, so if a module's raises leave the scan's reach
while the module keeps ONE reachable raise, both sides still list it and the
census passes. The last row of the table above is exactly that case and it is
the reason it is in the table: ``app.api.import_`` is a ROUTE module, so its own
endpoints keep it reached even with the entry dropped, and what silently goes is
the CROSS-MODULE edge into ``ensure_import_can_start`` from the two acquisition
routes. No count saw that either (70, unchanged). It is covered instead by
``test_the_scan_crosses_from_one_route_module_into_another``, which asserts that
one ``_resolve_call`` edge directly - and that is the general remedy for this
class: an edge the census cannot resolve to a whole module needs its own pin.

The other instance of the same limit is the ``Depends(...)`` shape: an inline
``raise HTTPException(...)`` turned into a shared dependency leaves the scan's
reach, because the walk starts at the endpoint function and never reads its
parameter defaults. MEASURED on ``app/api/events.py``, whose only two raises are
its endpoint's two 503s::

    both 503s moved into a Depends guard  -> census 2 RED, naming app.api.events
                                             (old floor: RED at 69, blaming
                                             "an operation stopped raising")
    only ONE 503 moved, one left inline   -> census 2 GREEN (old floor: GREEN, 70)

So the census converts the first case from a misattributed red into a red that
names the module and the cause, and is exactly as blind as the count in the
second. Closing the second needs the walk to enter parameter defaults, which is
a change to ``_schema_operations``/``_raised_statuses``, not to this census.

One more thing the equality does not pin today: the ``include_in_schema`` filter
in ``_schema_operations``. There is no ``include_in_schema=False`` APIRoute in
the test process at all - ``app/static_files.py``'s SPA fallback is registered
only when ``mount_static`` finds a built ``index.html``, which the test
environment does not have - so removing that filter changes nothing (measured:
``walked == in_spec == 112`` either way). It becomes load-bearing the moment a
built SPA is present when this test runs: the walk would then reach a fallback
route the spec deliberately omits, and only that filter keeps the two sides
equal.

COVERAGE, re-measured 2026-08-28 by running this test rather than by hand:
``_FOLLOWED_MODULES`` names EVERY module that raises an ``HTTPException`` on
some route's behalf from outside that route's own module. Nine are shared
adapters outside ``app/api``; the tenth, ``app.api.import_``, is a ROUTE module
- see the cross-module note below. Together with the route modules themselves
they are the whole set that ``grep -rln 'raise HTTPException' app/`` returns,
with one exception noted below. Nothing is unresolvable, nothing is unscannable,
and the gap list is EMPTY. There is no backlog of un-followed modules left, so
there is no allowlist and nothing deferred; the way to keep it that way is to
add any NEW module that raises on another module's route's behalf to
``_FOLLOWED_MODULES`` in the same commit that creates it - which is now
ENFORCED rather than requested: census 2 fails the day such a module exists
unreached, and ``test_no_followed_module_entry_names_a_module_that_no_longer_exists``
fails the day an entry here stops naming a file.

A CALL FROM ONE ``app/api`` MODULE INTO ANOTHER is not a same-module call, so
``_resolve_call`` stops at it unless the target module is listed - the listing
rule above is about ``app/api`` too, not only about shared adapters.
``app/api/acquisition.py`` imports ``ensure_import_can_start`` from
``app/api/import_.py``, and that helper's two 409s were invisible to the two
acquisition routes that call it (inert only because both call sites also raise
409 from their own ``except RuntimeError`` arm). Listing the module is the fix
rather than documenting the shape, because the shape is reachable by
configuration - it would have been a backlog entry, not a structural blind spot.
Measured after listing it: 0 gaps, 0 unresolved, 0 unscannable. It changes no
operation's status set, and neither does following ALL 22 ``app/api`` modules,
so ``ensure_import_can_start`` is the only cross-``app/api`` target that raises
today; ``test_the_scan_crosses_from_one_route_module_into_another`` below pins
the edge itself rather than a status set, since a set-level assertion there
would be vacuous while both call sites keep raising 409 locally too.
Re-measure the whole class with::

    import pkgutil, app.api, tests.test_route_status_declarations as g
    g._FOLLOWED_MODULES |= {f"app.api.{m.name}" for m in pkgutil.iter_modules(app.api.__path__)}
    g._module_index.cache_clear()          # @cache on the AST parse
    g._gaps.cache_clear()                  # @cache on the whole pass
    print(g._gaps().gaps)

The one grep hit that is not followed is ``app/static_files.py``: its two 404s
sit on an ``include_in_schema=False`` route, which ``_schema_operations``
excludes because a ``responses`` entry there never reaches the spec at all. It
is therefore also the single entry in ``_OUT_OF_SCHEMA_MODULES``, which excuses
it from BOTH censuses for that one reason.

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
``HTTPException``, so nothing is hiding there right now. Census 2 covers this
shape only at MODULE granularity: a dependency that is the sole raise in its
module fails the census by name, one that joins a module already reached does
not. See ITS ONE GRANULARITY LIMIT above.

This guard checks that a status is PRESENT, not that its body schema is right:
it reads response CODES out of the spec and nothing else. A status declared with
the wrong model (``ErrorDetail`` for a nested ``{message, recovery}`` 500, say)
passes here. ``app/models/errors.py`` is the rule for that half, and two sibling
tests ENFORCE it, both by joining a declared model to the body the server really
sends (which is what makes swapping a model fail a test rather than a review):
``tests/test_config_conflict_body_contract.py`` for the config editor's CAS 409,
and ``tests/test_config_validation_body_contract.py`` for the 422 of BOTH config
save routes. Every other status declared anywhere in the app still has only this
file's presence check behind it.
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
from functools import cache
from pathlib import Path
from typing import Final, NamedTuple

from fastapi import routing
from fastapi.routing import APIRoute

from app import main as app_main
from app.main import app

#: Modules whose raises count as the raises of every route that calls into them.
#: Nine are shared adapters outside ``app/api``. ``app.api.import_`` is a route
#: module and is here because ``app/api/acquisition.py`` imports
#: ``ensure_import_can_start`` from it: a call from one ``app/api`` module into
#: ANOTHER is not a same-module call, so ``_resolve_call`` stops at it unless the
#: module is listed (see the docstring's cross-module note).
_FOLLOWED_MODULES: Final = frozenset(
    {
        "app.api.import_",
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

#: The census's ONLY escape hatch: a module the source census finds on disk but
#: which no operation in the generated schema can reach, with the reason it can
#: never be on a route's path. An entry is a decision that has to survive review
#: - never a way to quiet a module that has merely fallen out of the scan's
#: reach, which is the failure the census exists to report.
#:
#: ``test_no_census_exemption_is_stale`` fails if an entry stops naming a module
#: the census actually finds, so a rename cannot leave a rubber stamp behind.
_OUT_OF_SCHEMA_MODULES: Final[dict[str, str]] = {
    "app.static_files": (
        "the SPA fallback route is registered with include_in_schema=False, so"
        " neither it nor its two 404s can ever reach the generated spec;"
        " _schema_operations excludes it rather than counting it as covered"
    ),
}


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


class _SourceCensus(NamedTuple):
    """What the ``app/`` SOURCE TREE says, enumerated independently of routing.

    This is the census's ground truth, and it is deliberately read from the
    FILESYSTEM rather than from ``sys.modules`` or from any route object: a
    module that has been renamed, or that stopped being imported, still shows up
    here under its new name. That independence is the whole point - both sides
    of ``walked == in_spec`` come from one generator and lose a router together
    (module docstring), whereas a directory listing cannot.
    """

    #: Every module under ``app/``, whatever it contains.
    all_modules: frozenset[str]
    #: Modules that decorate a function with ``@<something>.<http method>(...)``
    #: - i.e. that declare route operations.
    route_modules: frozenset[str]
    #: Modules containing at least one ``raise HTTPException(...)`` that the
    #: scan's own rules say escapes (same ``_status_of`` and ``_suppressed_raises``
    #: the walk uses, so the two sides cannot disagree about what "raises" means).
    raising_modules: frozenset[str]


def _app_source_root() -> Path:
    """The ``app/`` package directory, located from the imported package itself."""
    source = app_main.__file__
    assert source is not None, "app.main must have a source file for the census to read"
    return Path(source).resolve().parent


def _module_name_of(path: Path, root: Path) -> str:
    """``app/beets/cover.py`` -> ``app.beets.cover`` (packages lose ``__init__``)."""
    parts = path.relative_to(root).with_suffix("").parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join((root.name, *parts))


def _declares_route_operations(tree: ast.Module) -> bool:
    """True if some function in ``tree`` is decorated ``@router.get(...)`` & co.

    Matches the ATTRIBUTE, so ``@router.post``, ``@app.get`` and any other
    receiver all count; ``_HTTP_METHODS`` is the same set the spec side reads.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            called = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(called, ast.Attribute) and called.attr in _HTTP_METHODS:
                return True
    return False


def _raises_a_tracked_status(tree: ast.Module) -> bool:
    """True if some function in ``tree`` raises a status that escapes it.

    Uses the walk's own predicates on purpose: a raise the walk would drop as
    suppressed must not count here either, or the census would demand reach for
    a status that never leaves the process.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        skip = _suppressed_raises(node)
        for inner in ast.walk(node):
            if isinstance(inner, ast.Raise) and inner.lineno not in skip:
                if _status_of(inner) is not None:
                    return True
    return False


@cache
def _source_census() -> _SourceCensus:
    """Walk ``app/**/*.py`` on disk and classify every module in it."""
    root = _app_source_root()
    all_modules: set[str] = set()
    route_modules: set[str] = set()
    raising_modules: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        module_name = _module_name_of(path, root)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        all_modules.add(module_name)
        if _declares_route_operations(tree):
            route_modules.add(module_name)
        if _raises_a_tracked_status(tree):
            raising_modules.add(module_name)
    return _SourceCensus(
        frozenset(all_modules), frozenset(route_modules), frozenset(raising_modules)
    )


def _expected_modules(modules: frozenset[str]) -> list[str]:
    """A source-census set as the sorted list the walk is required to reproduce."""
    return sorted(modules - set(_OUT_OF_SCHEMA_MODULES))


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

    ``raising_modules`` is where the statuses CAME FROM - the census side. An
    entry means this operation's call graph really reached a raise in that
    module, which is what the source census is compared against.
    """

    statuses: set[int]
    unresolved: list[str]
    unscannable: list[str]
    raising_modules: set[str]


def _raised_statuses(module_name: str, function_name: str) -> _ScanResult:
    """Every status reachable from ``module.function``, plus what it could not read."""
    statuses: set[int] = set()
    unresolved: list[str] = []
    unscannable: list[str] = []
    raising_modules: set[str] = set()
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
            if status is None:
                continue
            # Reached a raise this scan tracks - record the module, whether or
            # not its status resolves, because reach is what the census asks
            # about and an unresolved raise is still a raise the walk can see.
            raising_modules.add(current[0])
            if isinstance(status, int):
                statuses.add(status)
            else:
                unresolved.append(f"{current[0]}.{current[1]}: {status}")
        for name in _called_names(func):
            target = _resolve_call(index, name)
            if target is not None and target not in seen:
                pending.append(target)
    return _ScanResult(statuses, unresolved, unscannable, raising_modules)


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
    #: Operations seen to raise anything. NOT asserted on any more - it was the
    #: old ``_MIN_ROUTES_WITH_RAISES`` floor's metric and is kept as the
    #: diagnostic the module docstring's measurements are quoted from.
    with_raises: int
    #: THE CENSUS, walk side. Modules that defined at least one operation the
    #: schema exposes, and modules some operation's call graph reached a raise
    #: in. Both are compared against ``_source_census()``, which reads the same
    #: facts off the filesystem instead of off the router.
    route_modules: list[str]
    raising_modules: list[str]


@cache
def _gaps() -> _GapReport:
    """Scan every operation in the app and join it to the live spec.

    Cached: three tests below assert on different parts of one pass, and the
    walk is pure (AST off disk plus ``app.openapi()``, itself cached).
    Monkeypatching anything it reads means ``_gaps.cache_clear()``.
    """
    spec = app.openapi()
    gaps: list[str] = []
    unresolved: list[str] = []
    unscannable: list[str] = []
    operations = _schema_operations()
    with_raises = 0
    raising_modules: set[str] = set()
    for method, path, module_name, function_name in operations:
        result = _raised_statuses(module_name, function_name)
        if result.statuses:
            with_raises += 1
        raising_modules |= result.raising_modules
        unresolved.extend(f"{method} {path}: {line}" for line in result.unresolved)
        unscannable.extend(f"{method} {path}: {line}" for line in result.unscannable)
        declared = _declared_statuses(spec, path, method)
        for status in sorted(result.statuses - declared):
            if (method, path, status) in _ALLOWLIST:
                continue
            gaps.append(f"{method} {path}: {status} raised but not declared")
    walked = sorted((operation.method, operation.path) for operation in operations)
    return _GapReport(
        gaps,
        unresolved,
        unscannable,
        walked,
        _spec_operations(spec),
        with_raises,
        sorted({operation.module for operation in operations}),
        sorted(raising_modules),
    )


def test_every_status_a_route_can_raise_is_declared_in_the_live_spec() -> None:
    report = _gaps()
    # Non-vacuity for the test's OWN enumeration: its filtering of
    # ``iter_route_contexts`` must yield exactly the operations FastAPI writes
    # into the spec from that same generator. A router that drops out of THIS
    # side (as every router did when FastAPI 0.141 turned ``app.routes`` into
    # ``_IncludedRouter`` objects) reports zero gaps for every route in it,
    # which is the failure mode a floor of "at least N" cannot see. A router
    # that drops out of BOTH sides is invisible HERE by construction; the two
    # census tests below are what cover that, from the filesystem.
    assert report.walked == report.in_spec, (
        "the AST walk and the live spec disagree about which operations exist,"
        " so some route is being checked against nothing:\n  walked but not in the spec: "
        + str(sorted(set(report.walked) - set(report.in_spec)))
        + "\n  in the spec but not walked: "
        + str(sorted(set(report.in_spec) - set(report.walked)))
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


def test_every_module_that_declares_routes_still_reaches_the_walk() -> None:
    """CENSUS 1 of 2: no router may vanish from the schema without a failure.

    Every module under ``app/`` that decorates a function with an HTTP-method
    decorator must contribute at least one operation to the walk. Drop an
    ``include_router`` call from ``app/main.py``, rename a route module without
    re-including it, or lose the routes inside ``iter_route_contexts``, and the
    module is still on disk with its decorators while the walk no longer names
    it - so this fails, by name.

    This is the ONLY assertion in the file that can see ``browse``, ``health``,
    ``search`` and ``stats`` disappear: they contain no raising operation, so no
    count of raising operations - at any value - moves when they go (module
    docstring, MEASURED BLAST RADIUS). It is also the half that survives growth:
    a new router is added to the expected set by existing, not by anyone
    remembering to raise a constant.
    """
    walked = _gaps().route_modules
    expected = _expected_modules(_source_census().route_modules)
    assert walked == expected, (
        "COVERAGE MOVED OUT OF SCAN RANGE - suspect that FIRST. These modules declare"
        " route operations in app/ but contribute none to the walk, so nothing in them"
        " is checked against the contract at all:\n  "
        + "\n  ".join(sorted(set(expected) - set(walked)))
        + "\n  (and, the other way, walked but declaring no route on disk - which would"
        " mean this census's own decorator rule is wrong: "
        + str(sorted(set(walked) - set(expected)))
        + ")\nLikeliest cause: an include_router call was dropped from app/main.py, or a"
        " route module was renamed and not re-included. Fix the wiring. Deleting the"
        " module from the census instead re-blinds this guard to every route in it."
    )


def test_every_module_that_raises_on_a_routes_behalf_is_still_in_scan_range() -> None:
    """CENSUS 2 of 2: no raise may leave the scan's reach without a failure.

    Every module under ``app/`` that raises an ``HTTPException`` the scan would
    track must be a module some operation's call graph actually reaches. This is
    what replaced ``_MIN_ROUTES_WITH_RAISES``, a floor pinned to the measured
    count of raising operations. That constant's own comment claimed the count
    "moves only when an operation stops raising anything at all". MEASURED, that
    is false: reach depends on ``_FOLLOWED_MODULES``, so dropping each followed
    module in turn (equivalently: renaming it, or extracting a raising helper
    into a NEW module, and not relisting it) took the count to 67-70 with an
    EMPTY gap list. Renaming ``app/beets/completeness.py`` alone - a pure module
    move, no contract change - failed the floor at 69, and the failure message
    invited the reader to "re-measure and lower the constant", which would have
    blinded the scan to that module's 404 permanently. For ``config_editor`` it
    would have blinded three operations at once.

    The census cannot be paid off that way: the expected set is read off the
    FILESYSTEM, so a renamed module reappears under its new name and stays
    expected until it is genuinely back in reach.

    And it stays quiet when raises merely MOVE within reach: a raise pushed from
    an endpoint into a same-module helper, into ``app.library_busy``, or into any
    other followed module leaves both sides equal - the module it left drops out
    of the expected set and the reached set together, because both sides are
    computed with the same ``_status_of``/``_suppressed_raises`` rules. Measured
    on a real refactor of ``app/api/lyrics.py``; see the module docstring.

    It is PER MODULE, so it cannot see coverage leave a module that keeps one
    reachable raise - the module docstring's ITS ONE GRANULARITY LIMIT names the
    two shapes that hit this (a cross-``app/api`` edge, and a ``Depends(...)``
    guard sharing a module with other raises) and what pins each instead.
    """
    reached = _gaps().raising_modules
    expected = _expected_modules(_source_census().raising_modules)
    assert reached == expected, (
        "COVERAGE MOVED OUT OF SCAN RANGE - suspect that FIRST. These modules raise an"
        " HTTPException in app/ that the scan's own rules say escapes, but no route's"
        " call-graph walk reaches them, so every status they raise is now invisible here"
        " and can never be reported as an undeclared gap:\n  "
        + "\n  ".join(sorted(set(expected) - set(reached)))
        + "\n  (and, the other way, reached by the walk but not seen to raise on disk -"
        " which would mean the two sides disagree about what a raise is: "
        + str(sorted(set(reached) - set(expected)))
        + ")\nLikeliest causes, in order: (1) a module in _FOLLOWED_MODULES was RENAMED or"
        " MOVED and the set still names the old path; (2) a raising helper was extracted"
        " into a NEW module nobody added to _FOLLOWED_MODULES; (3) an inline raise became"
        " a Depends(...) guard, which the walk never enters because it starts at the"
        " endpoint function and does not read its parameter defaults. Restore the reach."
        " An _OUT_OF_SCHEMA_MODULES entry is for a module that can never be on a route's"
        " path at all, and needs the reason written down."
    )


def test_no_followed_module_entry_names_a_module_that_no_longer_exists() -> None:
    """The rename, caught from the other side: a stale ``_FOLLOWED_MODULES`` name.

    ``_resolve_call`` compares an import's source module against this set by
    STRING, so an entry that no longer names a file silently stops following
    anything - no error, no gap, just a quieter scan. Census 2 catches the new
    name being out of reach; this catches the old name still being listed, which
    is the same edit and the more legible message.
    """
    missing = sorted(_FOLLOWED_MODULES - _source_census().all_modules)
    assert not missing, (
        "COVERAGE MOVED OUT OF SCAN RANGE - suspect that FIRST. _FOLLOWED_MODULES names"
        " modules that are not in app/ any more, so _resolve_call stops at every call"
        " into them and their raises are invisible:\n  " + "\n  ".join(missing)
    )


def test_no_census_exemption_is_stale() -> None:
    """An exemption must keep naming a module the census would otherwise flag.

    Otherwise the entry is a rubber stamp: it would go on excusing a name that
    no longer exists while the module it used to describe, renamed, is excused
    by nobody and flagged by nobody.
    """
    census = _source_census()
    classified = census.route_modules | census.raising_modules
    stale = sorted(set(_OUT_OF_SCHEMA_MODULES) - classified)
    assert not stale, (
        "these _OUT_OF_SCHEMA_MODULES entries no longer name a module that declares a"
        " route or raises a status, so they excuse nothing - drop them, or repoint them"
        " at whatever the module was renamed to:\n  " + "\n  ".join(stale)
    )


def test_the_source_census_discriminates() -> None:
    """Anti-vacuity for both censuses, asserted on SHAPE so it cannot rot.

    A census whose expected set is empty - ``rglob`` aimed at the wrong
    directory, or a classifier that stopped matching - would turn both
    equalities into a complaint about the WALK rather than about itself. A
    classifier that matched everything would be just as useless in the other
    direction. So: both sets non-empty, both PROPER subsets of the tree, at
    least one raising module outside ``app/api`` (the shared adapters are the
    ones a route file's own text cannot show), and at least one route module
    that raises nothing (the browse/health/search/stats class, which is the
    whole reason census 1 exists). No module is named, so a legitimate rename
    moves this test's ground truth with it instead of failing it.
    """
    census = _source_census()
    assert "app.main" in census.all_modules, "the census must be reading the app/ tree"
    assert census.route_modules < census.all_modules, "the decorator rule must discriminate"
    assert census.raising_modules < census.all_modules, "the raise rule must discriminate"
    assert any(not name.startswith("app.api.") for name in census.raising_modules), (
        "the raise rule must reach the shared adapters outside app/api"
    )
    assert census.route_modules - census.raising_modules, (
        "a router that raises nothing is exactly what census 1 exists to cover"
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


def test_the_scan_crosses_from_one_route_module_into_another() -> None:
    """``app/api`` -> ``app/api`` is NOT a same-module call, so it needs listing.

    ``app/api/acquisition.py`` imports ``ensure_import_can_start`` from
    ``app/api/import_.py``; ``_resolve_call`` follows same-module defs and
    ``_FOLLOWED_MODULES`` only, so without the entry that helper's two 409s are
    invisible to ``POST /api/acquisition/review-inbox`` and
    ``POST /api/acquisition/inbox/import-item``.

    Asserted on the EDGE, not on those routes' status sets: both call sites also
    raise 409 themselves (the ambiguous-name and ``RuntimeError`` arms), so
    ``409 in statuses`` is true either way and would pin nothing. Drop
    ``app.api.import_`` from ``_FOLLOWED_MODULES`` and ``_resolve_call`` returns
    ``None`` here, which this catches and the main assertion above does not.
    """
    index = _module_index("app.api.acquisition")
    assert index is not None
    assert _resolve_call(index, "ensure_import_can_start") == (
        "app.api.import_",
        "ensure_import_can_start",
    ), "a helper imported from another app/api module must be followed, not stopped at"
    # ... and the target really is worth following: it raises a status of its own.
    helper = _raised_statuses("app.api.import_", "ensure_import_can_start")
    assert not helper.unresolved
    assert not helper.unscannable
    assert 409 in helper.statuses
