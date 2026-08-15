# Developer tasks that span both apps.
#
# `coverage` is what the `sonar-scan` wrapper runs before every upload, so
# SonarQube reads fresh numbers instead of reporting 0% from its Zero Coverage
# Sensor. (That wrapper is a local tool of the maintainer's, not part of this
# repo; the target is useful on its own.) Everything it writes is gitignored
# build output: backend/coverage/, frontend/coverage/, backend/.coverage.
#
# Each suite runs from its own directory because it has to. app/config.py sets
# `env_file=".env"`, which pydantic-settings looks up against the PROCESS cwd, and
# `beets_dir` falls back to the cwd-relative default "data/beets" -- start pytest
# from the repo root and the backend finds no .env at all and silently opens the
# repo-root data/beets, i.e. the REAL dev library. The reports, though, are read
# by a scanner whose base directory is the REPO ROOT, so each one is rebased onto
# it; see
# backend/scripts/rebase_coverage_source.py and frontend/vitest.config.ts.
#
# make stops at the first failing recipe line, so a red suite aborts the run (and
# with it the upload) rather than publishing stale numbers. The stale reports are
# deleted up front for the same reason: an aborted run must not leave behind a
# fresh-looking but UNREBASED coverage.xml for a later hand-run scan to read as 0%.

.DEFAULT_GOAL := help
.PHONY: help coverage

help:
	@echo "make coverage  - run both test suites with coverage and write the four SonarQube"
	@echo "                 reports into backend/coverage/ and frontend/coverage/ (gitignored)"

coverage:
	rm -f backend/coverage/coverage.xml backend/coverage/junit.xml \
		frontend/coverage/lcov.info frontend/coverage/sonar-report.xml
	cd backend && uv run pytest --cov \
		--cov-report=xml:coverage/coverage.xml \
		--cov-report=term:skip-covered \
		--junitxml=coverage/junit.xml --junit-prefix=backend
	cd backend && uv run python scripts/rebase_coverage_source.py coverage/coverage.xml
	cd frontend && npm run test:coverage
