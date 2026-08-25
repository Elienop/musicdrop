"""Pin the dump script's output contract: bytes, not just content.

``tests/test_openapi_spec_guard.py`` compares the tracked ``frontend/openapi.json``
against the live spec as PARSED DICTS on purpose — formatting drift must not
fail that guard. But the dump's canonical serialization is what keeps an
unchanged spec rewriting diff-free (no churn in review), and byte-equality
here is deliberately stricter than the guard's parsed-dict comparison: it
also catches serialization-invisible drift the dict compare cannot see, like
``false`` vs ``0``.
"""

from pathlib import Path

import pytest

import scripts.dump_openapi as dump_openapi
from scripts.dump_openapi import main

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACKED_FILE = REPO_ROOT / "frontend" / "openapi.json"


def test_dump_output_bytes_match_tracked_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "openapi.json"
    monkeypatch.setattr(dump_openapi, "OUTPUT", target)

    main()

    assert target.read_bytes() == TRACKED_FILE.read_bytes()
