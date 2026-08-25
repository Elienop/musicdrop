"""Guard: the tracked ``frontend/openapi.json`` must match the live spec.

We compare PARSED DICTS, never serialized text: a byte comparison would fail
on pure formatting drift (whitespace, key order) and would teach people to
distrust the guard — the contract content is what the frontend codegen
consumes, not the bytes on disk.

Why this guard exists at all: an ADDED path is self-correcting — frontend
code simply cannot compile against a type that was never generated, so the
gap is caught at build time. A CHANGED one is not: the old generated
TypeScript types still compile fine while the contract is silently wrong,
and no compiler anywhere will ever point at the drift.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACKED_FILE = REPO_ROOT / "frontend" / "openapi.json"

REGEN_GUIDANCE = (
    "Regenerate the tracked contract (two steps, from the repo root):\n"
    "  cd backend && uv run python scripts/dump_openapi.py\n"
    "  cd ../frontend && npm run gen:api"
)


def _as_dict(value: object) -> dict[str, object]:
    """Narrow an arbitrary JSON value to a string-keyed dict (``{}`` otherwise)."""
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def _diff_section(
    tracked: dict[str, object], live: dict[str, object]
) -> tuple[list[str], list[str], list[str]]:
    """Return (only in tracked, only in live, changed) key lists for one dict section."""
    only_tracked = sorted(set(tracked) - set(live))
    only_live = sorted(set(live) - set(tracked))
    changed = sorted(key for key in set(tracked) & set(live) if tracked[key] != live[key])
    return only_tracked, only_live, changed


def _summarize_section(
    label: str, tracked: dict[str, object], live: dict[str, object], lines: list[str]
) -> None:
    only_tracked, only_live, changed = _diff_section(tracked, live)
    if only_tracked:
        lines.append(f"  {label} only in tracked: {', '.join(only_tracked)}")
    if only_live:
        lines.append(f"  {label} only in live: {', '.join(only_live)}")
    if changed:
        lines.append(f"  {label} changed: {', '.join(changed)}")


def _drift_summary(tracked: dict[str, object], live: dict[str, object]) -> str:
    """Summarize WHAT drifted so a developer never hand-diffs a ~12k-line file."""
    lines: list[str] = []

    tracked_components = _as_dict(tracked.get("components"))
    live_components = _as_dict(live.get("components"))
    _summarize_section("paths", _as_dict(tracked.get("paths")), _as_dict(live.get("paths")), lines)
    _summarize_section(
        "component schemas",
        _as_dict(tracked_components.get("schemas")),
        _as_dict(live_components.get("schemas")),
        lines,
    )

    for key in sorted(set(tracked) | set(live)):
        if key == "paths":
            continue
        if key == "components":
            sub = {k: v for k, v in tracked_components.items() if k != "schemas"}
            sub_live = {k: v for k, v in live_components.items() if k != "schemas"}
            _summarize_section("components (non-schema)", sub, sub_live, lines)
            continue
        if key not in tracked:
            lines.append(f"  top-level key only in live: {key!r}")
        elif key not in live:
            lines.append(f"  top-level key only in tracked: {key!r}")
        elif tracked[key] != live[key]:
            lines.append(f"  top-level key changed: {key!r}")

    if not lines:
        lines.append(
            "  (drift detected but not summarizable at key level — diff the file"
            " against a fresh dump)"
        )
    return "\n".join(lines)


def test_tracked_openapi_matches_live_spec() -> None:
    if not TRACKED_FILE.is_file():
        pytest.fail(f"tracked API contract is missing at {TRACKED_FILE}\n{REGEN_GUIDANCE}")
    tracked = json.loads(TRACKED_FILE.read_text(encoding="utf-8"))
    if not isinstance(tracked, dict):
        pytest.fail(
            f"tracked API contract at {TRACKED_FILE} is not a JSON object\n{REGEN_GUIDANCE}"
        )
    # Deliberately NOT the conftest ``client`` fixture: the spec needs no
    # beets library, and that fixture would drag one in for this test.
    client = TestClient(app)
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    live = resp.json()
    assert live == tracked, (
        "frontend/openapi.json has drifted from the live /openapi.json spec.\n"
        f"{REGEN_GUIDANCE}\n"
        "What drifted:\n" + _drift_summary(_as_dict(tracked), _as_dict(live))
    )
