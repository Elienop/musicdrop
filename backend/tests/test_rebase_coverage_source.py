"""Tests for the Cobertura <source> rebase that `make coverage` runs after pytest.

This rewrite is the only reason SonarQube can resolve `backend/app/**` at all: it
turns the `<source>app</source>` coverage.py writes from `backend/` into the
`backend/app` the scanner needs. If it ever silently no-ops, the whole Python
side of the dashboard reads 0% — so its refusals are as load-bearing as its
happy path, and every one of them must leave the report untouched.
"""

from pathlib import Path

import pytest

from scripts.rebase_coverage_source import REBASED_TO, ReportShapeError, main, rebase

# Trimmed to the parts the rewrite touches. coverage.py indents with tabs and
# emits one <source> per `[tool.coverage.run] source` root, with each class
# filename relative to it.
REPORT = (
    '<?xml version="1.0" ?>\n'
    '<coverage version="7.15.4" lines-valid="9788">\n'
    "\t<sources>\n"
    "{sources}"
    "\t</sources>\n"
    "\t<packages>\n"
    '\t\t<package name="plex">\n'
    "\t\t\t<classes>\n"
    '\t\t\t\t<class name="sync.py" filename="plex/sync.py"/>\n'
    "\t\t\t</classes>\n"
    "\t\t</package>\n"
    "\t</packages>\n"
    "</coverage>\n"
)


def write_report(tmp_path: Path, *sources: str) -> Path:
    report = tmp_path / "coverage.xml"
    body = "".join(f"\t\t<source>{s}</source>\n" for s in sources)
    report.write_text(REPORT.format(sources=body), encoding="utf-8")
    return report


def test_rebases_the_source_onto_the_repo_root(tmp_path: Path) -> None:
    report = write_report(tmp_path, "app")

    rebase(report)

    text = report.read_text(encoding="utf-8")
    assert "<source>backend/app</source>" in text
    assert "<source>app</source>" not in text
    # The class filenames stay relative to <source>; rewriting them too would
    # double the prefix.
    assert 'filename="plex/sync.py"' in text


def test_is_idempotent(tmp_path: Path) -> None:
    report = write_report(tmp_path, "app")

    rebase(report)
    once = report.read_text(encoding="utf-8")
    rebase(report)

    assert report.read_text(encoding="utf-8") == once


def test_rebases_an_absolute_source(tmp_path: Path) -> None:
    """Without `relative_files` coverage.py writes the absolute host path.

    The class filenames are relative to it either way, so the same rewrite is
    correct — and the absolute path is exactly what does NOT exist inside the
    scanner container.
    """
    report = write_report(tmp_path, "/mnt/data/projects/MusicDrop/backend/app")

    rebase(report)

    assert "<source>backend/app</source>" in report.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("sources", "expected_count"),
    [((), 0), (("app", "tests"), 2)],
)
def test_refuses_a_report_without_exactly_one_source(
    tmp_path: Path, sources: tuple[str, ...], expected_count: int
) -> None:
    report = write_report(tmp_path, *sources)
    before = report.read_text(encoding="utf-8")

    with pytest.raises(ReportShapeError) as excinfo:
        rebase(report)

    assert f"found {expected_count}" in str(excinfo.value)
    # Name the cause a maintainer would actually hit, not "coverage.py changed".
    assert "[tool.coverage.run] source" in str(excinfo.value)
    assert report.read_text(encoding="utf-8") == before


def test_refuses_a_source_not_rooted_at_the_app_package(tmp_path: Path) -> None:
    report = write_report(tmp_path, ".")
    before = report.read_text(encoding="utf-8")

    with pytest.raises(ReportShapeError) as excinfo:
        rebase(report)

    assert REBASED_TO in str(excinfo.value)
    assert report.read_text(encoding="utf-8") == before


def test_main_rebases_and_returns_zero(tmp_path: Path) -> None:
    report = write_report(tmp_path, "app")

    assert main([str(report)]) == 0
    assert "<source>backend/app</source>" in report.read_text(encoding="utf-8")


def test_main_rejects_a_missing_file_with_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.xml"

    with pytest.raises(SystemExit) as excinfo:
        main([str(missing)])

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "no such report file" in err
    assert "Traceback" not in err


def test_main_shows_usage_for_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0
    assert "rebase_coverage_source.py" in capsys.readouterr().out


def test_main_reports_a_bad_shape_on_stderr_and_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = write_report(tmp_path, ".")

    assert main([str(report)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "Traceback" not in err
