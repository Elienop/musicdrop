"""Guard: an operator-facing INFO record must reach ``docker logs``.

The shipped container runs bare ``uvicorn app.main:app`` (``Dockerfile:61``), so
uvicorn's own ``LOGGING_CONFIG`` configures logging — and it leaves the root
logger at WARNING with no handlers. An app-namespace INFO record was therefore
dropped entirely: not mis-tagged, not unformatted, absent. A WARNING from the
same logger did get out, through logging's ``lastResort`` handler, which is
what made the gap easy to miss — ``logger.exception`` beside a
``logger.info`` worked, so the failure path was visible and the success path
was not.

``app/main.py``'s ``_boot_log`` documented this for startup refusals; the rule
was still broken in five places, one of which a commit had just described as
"the only record of what beets did to the user's files".

``main.wire_app_log_namespace`` now closes the gap for the whole namespace, and
``test_the_app_namespace_reaches_a_real_uvicorns_output`` below measures that.
It runs in the LIFESPAN, so two windows stay open and the convention below is
what covers them: a record emitted while a module is still being imported (the
wiring has not run yet), and any run that is not under uvicorn — where there is
no handler to borrow and ``lastResort`` behaves exactly as it did.

The AST test is AST and not grep, so ``logger .info(...)`` and a commented-out
line are judged correctly. Checked by attribute name rather than by resolving
the logger object: the convention it pins is the NAME — ``logger`` for warnings
and exceptions, ``operator_logger`` for anything an operator must be able to
read.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
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


#: The child that measures the namespace. In a SUBPROCESS because
#: ``dictConfig`` is destructive — non-incremental it closes every existing
#: handler, which under pytest are the capture handlers the rest of the run
#: needs. uvicorn's REAL ``LOGGING_CONFIG``, imported not transcribed, so a
#: change upstream is measured rather than mirrored.
#:
#: It runs the real LIFESPAN rather than calling the wiring by hand: the call
#: site is half the fix, and a test that reaches past it passes on an app that
#: never wires anything.
_PROBE = """
import logging, logging.config
from uvicorn.config import LOGGING_CONFIG

logging.config.dictConfig(LOGGING_CONFIG)
log = logging.getLogger("app.probe")
log.info("BEFORE-INFO")

from fastapi.testclient import TestClient

from app.main import app

with TestClient(app):
    print("LIFESPAN-RAN", flush=True)
    log.info("AFTER-INFO")
    log.warning("AFTER-WARNING")
"""


def test_the_app_namespace_reaches_a_real_uvicorns_output(tmp_path: Path) -> None:
    """An ``app.*`` INFO record must arrive, level-tagged, under uvicorn's config.

    The BEFORE half is the control, and it is the whole point: without the
    wiring the INFO record is not merely unformatted, it produces no output at
    all, so an assertion on the AFTER half alone would pass on a formatter that
    was already there.

    Level, not logger name: uvicorn's own formatter is ``%(levelprefix)s
    %(message)s``, so app records read exactly like uvicorn's — which is the
    same trade ``operator_logger`` already makes.
    """
    backend = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        # The child is not under the conftest BEETSDIR floor: aim every path it
        # may touch at a throwaway dir, and pin the auth clause's input so a
        # real local hash cannot change its output.
        "MUSICDROP_BEETS_DIR": str(tmp_path / "beets"),
        "MUSICDROP_STATIC_DIR": "",
        "MUSICDROP_PASSWORD_HASH": "",
    }
    done = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=backend,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = done.stdout + done.stderr
    assert done.returncode == 0, out

    assert "LIFESPAN-RAN" in out, out
    assert "BEFORE-INFO" not in out, out
    assert "INFO:     AFTER-INFO" in out, out
    assert "WARNING:  AFTER-WARNING" in out, out
