"""Run a probe inside an unprivileged mount namespace.

A bind mount is the one alias no spelling collapses, so several tests need one;
running them in-process is not possible, and each probe was a program inside a
string literal that ruff and mypy never opened. The probes live under
``tests/probes/`` as real modules for that reason (pytest does not collect them
— its default ``python_files`` is ``test_*.py``), and this module holds the one
copy of the harness that starts them.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROBES = Path(__file__).resolve().parent / "probes"


def unshare_works() -> bool:
    """Whether this box grants an unprivileged mount namespace.

    Measured rather than assumed: a box without user namespaces would otherwise
    fail these tests for a reason that is not about this code.
    """
    try:
        done = subprocess.run(
            ["unshare", "-Urm", "true"], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def run_probe(name: str, work: Path, *, timeout: int = 180) -> list[str]:
    """Run ``tests/probes/<name>.py`` under ``unshare -Urm``; return its stdout lines.

    ``--propagation private`` is what ``unshare -m`` already does. The child
    inherits this process's environment, which the rootdir conftest has floored
    (``BEETSDIR`` and ``MUSICDROP_BEETS_DIR`` under the test tree), and adds only
    the import root.
    """
    backend = Path(__file__).resolve().parent.parent
    done = subprocess.run(
        ["unshare", "-Urm", sys.executable, str(PROBES / f"{name}.py"), str(work)],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(backend),
        env={**os.environ, "PYTHONPATH": str(backend)},
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return [line for line in done.stdout.splitlines() if line and not line.startswith(" ")]
