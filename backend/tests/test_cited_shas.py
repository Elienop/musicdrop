"""Guard: every commit sha cited in a comment must be reachable from main.

A sha written down while working on a branch is DESTROYED by the squash-merge
that lands it, so the citation resolves for its author and for nobody else. It
has happened twice: ``5a55a10`` in ``App.test.tsx`` (#29) — whose object is gone
from the repository entirely — and ``ede5001`` in four middleware tests (#181),
which survived only as an unreachable, GC-eligible object in the author's clone.
Both were removed by the 2026-08-27 stale-reference sweep; this guard is what
stops the class regrowing, per the lock-on-clear rule.

Why ``origin/main`` and not ``HEAD``: on the branch that writes it, a
branch-local sha IS an ancestor of HEAD, so a HEAD check passes at exactly the
moment the mistake is made and only fails some later, innocent PR. Checked
against main, it fails on the PR that introduces it — which is the only place
the author can still fix it cheaply.

Only COMMENTS, DOCSTRINGS and MARKDOWN prose are scanned. Hex-looking STRING
LITERALS are not citations: ``deadbeef`` fixtures and MusicBrainz ids account
for 13 of 21 matches when the whole file is scanned, and none of them is a sha.
Scanning comments only leaves zero false positives today.

Note this needs full history: CI checks out with ``fetch-depth: 0`` for exactly
this test. It fails loudly rather than skipping when history is missing — a
guard that quietly opts out is how the family regrew in the first place.
"""

from __future__ import annotations

import ast
import io
import re
import subprocess
import tokenize
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 7-10 hex chars containing at least one a-f letter. The letter requirement
#: drops plain decimal ids (a Deezer track id, a test fixture's digits) that
#: are hex-legal by accident.
_SHA = re.compile(r"\b(?=[0-9a-f]{7,10}\b)(?=[0-9a-f]*[a-f])[0-9a-f]{7,10}\b")

#: Hex-looking words that appear in comments but are NOT commit citations. Empty
#: today; add a word here (with the reason) rather than widening _SHA, so the
#: exemption stays visible.
_NOT_SHAS: frozenset[str] = frozenset()

#: Where prose lives. Scanned for comments/docstrings; markdown is scanned whole.
_CODE_ROOTS = ("backend/app", "backend/tests", "frontend/src")
_PROSE_FILES = ("BACKLOG.md", "README.md")


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _base_ref() -> str:
    """The ref a citation must be reachable from, or fail saying how to fix it."""
    for ref in ("origin/main", "main"):
        if _git("rev-parse", "--verify", f"{ref}^{{commit}}").returncode == 0:
            return ref
    pytest.fail(
        "Neither origin/main nor main resolves, so cited shas cannot be checked. "
        "In CI this means the checkout lost its history: set `fetch-depth: 0` on "
        "actions/checkout for the backend job. Locally, run `git fetch origin main`."
    )


def _python_prose(path: Path) -> list[tuple[int, str]]:
    """Comment and docstring text from a Python file, with line numbers."""
    source = path.read_text(encoding="utf-8")
    found: list[tuple[int, str]] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                found.append((token.start[0], token.string))
        tree = ast.parse(source)
    except (SyntaxError, tokenize.TokenError):  # pragma: no cover - unparsable file
        return found
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                found.append((getattr(node, "lineno", 1), doc))
    return found


def _ts_prose(path: Path) -> list[tuple[int, str]]:
    """`//` and `/* */` comment text from a TS/TSX file, with line numbers."""
    source = path.read_text(encoding="utf-8")
    found: list[tuple[int, str]] = []
    for lineno, line in enumerate(source.splitlines(), 1):
        marker = line.find("//")
        if marker != -1:
            found.append((lineno, line[marker:]))
    for match in re.finditer(r"/\*.*?\*/", source, re.S):
        found.append((source[: match.start()].count("\n") + 1, match.group(0)))
    return found


def _cited_shas() -> dict[str, list[str]]:
    """Every sha-shaped token in prose, mapped to the `file:line` sites citing it."""
    # This file is the one place that MUST name dead shas — it documents them as
    # the examples the guard exists for — so it cannot police itself without
    # failing on its own docstring. Excluded deliberately, and narrowly: the
    # exclusion is this single file, not the directory.
    self_path = Path(__file__).resolve()

    targets: list[Path] = []
    for root in _CODE_ROOTS:
        targets += [
            p
            for p in (REPO_ROOT / root).rglob("*")
            if p.is_file() and p.suffix in {".py", ".ts", ".tsx"} and p.resolve() != self_path
        ]
    targets += [REPO_ROOT / name for name in _PROSE_FILES if (REPO_ROOT / name).is_file()]

    sites: dict[str, list[str]] = {}
    for path in targets:
        if path.suffix == ".py":
            chunks = _python_prose(path)
        elif path.suffix in {".ts", ".tsx"}:
            chunks = _ts_prose(path)
        else:
            chunks = [(1, path.read_text(encoding="utf-8"))]
        for lineno, text in chunks:
            for match in _SHA.finditer(text):
                token = match.group(0)
                if token in _NOT_SHAS:
                    continue
                sites.setdefault(token, []).append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    return sites


def test_every_cited_sha_is_reachable_from_main() -> None:
    """A cited sha must resolve AND be an ancestor of main.

    Both halves matter and they catch different failures. `ede5001` resolved
    fine and was unreachable; `5a55a10` did not resolve at all. Checking only
    the shas that resolve would have passed the worse of the two.
    """
    base = _base_ref()
    sites = _cited_shas()
    assert sites, (
        "No sha citations found at all. The scan matched nothing, which means the "
        "extractor is broken rather than the tree being clean — this guard is only "
        "meaningful while it can still see the citations it is meant to police."
    )

    broken: list[str] = []
    for sha, where in sorted(sites.items()):
        if _git("cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
            broken.append(f"{sha}: no such commit in this repository — cited at {', '.join(where)}")
        elif _git("merge-base", "--is-ancestor", sha, base).returncode != 0:
            broken.append(f"{sha}: not reachable from {base} — cited at {', '.join(where)}")

    assert not broken, (
        "Commit shas cited in comments that a fresh clone cannot resolve:\n  "
        + "\n  ".join(broken)
        + "\n\nA sha created on a feature branch does not survive its own squash-merge. "
        "Cite the PR number instead (e.g. `#181`), optionally with the sha the PR "
        "landed as, which IS on main and does resolve for everyone."
    )
