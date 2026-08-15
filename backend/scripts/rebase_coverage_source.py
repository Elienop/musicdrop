#!/usr/bin/env python3
"""Rebase a coverage.py Cobertura report from `backend/` onto the repo root.

SonarQube's scanner mounts the repo ROOT as its base directory, and SonarPython
resolves a Cobertura `<class filename=...>` as `new File(<source>, filename)`
with a relative `<source>` taken against that base dir -- never against the
report file's own directory.

coverage.py can only write paths relative to the directory it ran in, and the
backend suite has to run from `backend/`: `app/config.py` declares
`env_file=".env"`, which pydantic-settings looks up against the PROCESS cwd (run
pytest from the repo root and no .env is found at all), and `beets_dir` falls
back to the cwd-relative default `"data/beets"`. So the report comes out saying
`<source>app</source>` and nothing in it resolves from the repo root.

There is no coverage.py setting for this: `relative_files` only strips the
machine prefix, and `[paths]` remapping is not applied to the XML report
(checked against coverage 7.15). Rewriting the single `<source>` element after
the fact is the whole fix. It is done here rather than with `sed` so that an
unexpected report shape fails loudly instead of silently shipping a report whose
paths the scanner cannot resolve.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path, PurePosixPath

# coverage.py writes one <source> per `[tool.coverage.run] source` entry, and
# pyproject pins that to the single `app` package.
SOURCE_ELEMENT = re.compile(r"<source>([^<]*)</source>")
ROOTED_AT = "app"
REBASED_TO = "backend/app"


class ReportShapeError(RuntimeError):
    """The report is not shaped like the one this rewrite is safe to apply to."""


def rebase(report: Path) -> None:
    """Point the report's lone <source> at `backend/app`. Idempotent.

    Raises ReportShapeError, without touching the file, if the report is not the
    single-rooted-at-`app` shape the rewrite assumes.
    """
    text = report.read_text(encoding="utf-8")
    sources = SOURCE_ELEMENT.findall(text)
    if len(sources) != 1:
        raise ReportShapeError(
            f"{report}: expected exactly one <source> element, found {len(sources)}. "
            "The usual cause is a second `[tool.coverage.run] source` entry, or a "
            "measured file outside app/ -- coverage.py then emits a <source> per "
            "root. Fix the coverage config rather than uploading a report whose "
            "paths the scanner cannot resolve."
        )
    if PurePosixPath(sources[0]).name != ROOTED_AT:
        raise ReportShapeError(
            f"{report}: <source> is {sources[0]!r}, expected a path ending in "
            f"{ROOTED_AT!r}. The report is not rooted at the app package, so "
            f"rebasing it on {REBASED_TO!r} would misplace every file."
        )
    report.write_text(SOURCE_ELEMENT.sub(f"<source>{REBASED_TO}</source>", text), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rebase_coverage_source.py",
        description="Rebase a coverage.py Cobertura report onto the repo root.",
    )
    parser.add_argument(
        "report",
        type=Path,
        help="the coverage.xml to rewrite in place",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    report: Path = parser.parse_args(argv).report
    if not report.is_file():
        # Exit 2 (argparse's usage-error code): the caller got the invocation
        # wrong. A report that exists but is malformed exits 1 instead.
        parser.error(f"no such report file: {report}")
    try:
        rebase(report)
    except ReportShapeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
