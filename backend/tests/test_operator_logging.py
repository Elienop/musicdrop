"""Guard: an operator-facing INFO record must reach ``docker logs``.

The shipped container runs bare ``uvicorn app.main:app`` (``Dockerfile:61``), so
uvicorn's own ``LOGGING_CONFIG`` configures logging — and it leaves the root
logger at WARNING with no handlers. An app-namespace INFO record is therefore
dropped entirely: not mis-tagged, not unformatted, absent. A WARNING from the
same logger does get out, through logging's ``lastResort`` handler, which is
what makes the gap easy to miss — ``logger.exception`` beside a
``logger.info`` works, so the failure path is visible and the success path is
not.

``app/main.py``'s ``_boot_log`` already documented this for startup refusals.
The rule was still broken in five places, one of which a commit had just
described as "the only record of what beets did to the user's files".

AST, not grep, so ``logger .info(...)`` and a commented-out line are judged
correctly. Checked by attribute name rather than by resolving the logger object:
the convention this pins is the NAME — ``logger`` for warnings and exceptions,
``operator_logger`` for anything an operator must be able to read.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"

#: ``info`` ONLY. uvicorn's config drops ``debug`` too, but that is what DEBUG
#: means — a developer-only record, correctly invisible in production (7 such
#: calls in ``app/plex/playlists_pull.py`` are fine and this must not flag
#: them). ``warning`` and above reach stderr through ``lastResort``, so they may
#: stay on ``logger``. INFO is the only level whose name promises an operator
#: will read it while the container silently drops it.
_DROPPED_LEVELS = frozenset({"info"})


def _calls(tree: ast.AST) -> list[tuple[str, str, int]]:
    """Every ``<name>.<attr>(...)`` call, as (name, attr, lineno)."""
    out: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        ):
            out.append((node.func.value.id, node.func.attr, node.lineno))
    return out


def _sources() -> list[tuple[Path, ast.AST]]:
    return [(p, ast.parse(p.read_text())) for p in sorted(APP.rglob("*.py"))]


def test_no_operator_record_is_logged_at_a_level_the_container_drops() -> None:
    offenders = [
        f"{path.relative_to(APP.parent)}:{lineno} logger.{attr}(...)"
        for path, tree in _sources()
        for name, attr, lineno in _calls(tree)
        if name == "logger" and attr in _DROPPED_LEVELS
    ]
    assert offenders == [], (
        "these records are dropped in the shipped container; use "
        "``operator_logger`` (uvicorn.error) if an operator must read them:\n  "
        + "\n  ".join(offenders)
    )


def test_the_walk_can_actually_see_the_calls_it_is_judging() -> None:
    """The control. Without it the test above passes when ``_calls`` silently
    returns nothing — an empty offender list is indistinguishable from a broken
    walk, which is the failure mode this whole file exists to catch elsewhere.
    """
    found = [
        (path.name, lineno)
        for path, tree in _sources()
        for name, attr, lineno in _calls(tree)
        if name == "operator_logger" and attr == "info"
    ]
    assert len(found) >= 3, f"expected the operator_logger.info sites, saw {found}"

    # ...and that it would flag the banned shape if one existed
    planted = ast.parse("logger.info('x')\noperator_logger.info('y')\n")
    assert ("logger", "info", 1) in _calls(planted)
