"""Dump the app's live OpenAPI spec into ``frontend/openapi.json``.

This is step 1 of the API-contract refresh (see README): after changing a
Pydantic model, run this, then ``cd ../frontend && npm run gen:api`` (you
must already be in ``backend/`` to run this script). The canonical
serialization — ``json.dumps(spec, indent=2, allow_nan=False)`` plus a single
trailing newline, keys in insertion order — exists so an unchanged spec
rewrites diff-free (no churn in review); ``tests/test_dump_openapi.py``
pins those exact bytes.
"""

import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    # Running as a script puts `scripts/` (not the backend root) on sys.path.
    sys.path.insert(0, str(BACKEND_ROOT))

from app.main import app  # noqa: E402 -- must follow the sys.path bootstrap above

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT = REPO_ROOT / "frontend" / "openapi.json"


def main() -> None:
    spec = app.openapi()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # allow_nan=False: gen:api's strict JSON parser rejects Infinity/NaN, and the
    # drift guard re-parses with Python's lenient default so it would never notice
    # — fail here, at dump time, instead.
    OUTPUT.write_text(json.dumps(spec, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
