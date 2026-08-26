"""Guard: every ``$ref`` in the live spec resolves to a component that exists.

A dangling reference is invisible in Python and silent in the dump: the app
serves the spec happily, and ``openapi-typescript`` generates a type for a
component it was never given, so the frontend build fails far from the cause —
or worse, generates ``unknown`` and compiles.

The failure mode this exists for is specific. ``app/models/errors.py`` documents
a 422 that can carry either body as an ``anyOf`` over ``ErrorDetail`` and
FastAPI's ``HTTPValidationError``, and FastAPI emits the latter COMPONENT only
while at least one route still lets it auto-generate a 422. Hand-declaring the
last such route would delete the component and leave those ``anyOf`` refs
pointing at nothing. Written against every ``$ref`` rather than that one pair,
because the same trap catches any hand-written response entry.
"""

from collections.abc import Iterator

from fastapi.testclient import TestClient

from app.main import app


def _spec() -> dict[str, object]:
    # Deliberately NOT the conftest ``client`` fixture: the spec needs no beets
    # library, and that fixture would drag one in (see test_openapi_spec_guard).
    client = TestClient(app)
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    assert isinstance(spec, dict)
    return {str(key): item for key, item in spec.items()}


def _iter_refs(node: object, path: str = "$") -> Iterator[tuple[str, str]]:
    """Yield every ``(json path, $ref value)`` in the document, depth-first."""
    if isinstance(node, dict):
        for key, value in node.items():
            where = f"{path}.{key}"
            if key == "$ref" and isinstance(value, str):
                yield path, value
            else:
                yield from _iter_refs(value, where)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _iter_refs(value, f"{path}[{index}]")


def _resolve(spec: dict[str, object], ref: str) -> bool:
    """True iff ``ref`` is a local JSON pointer naming a node the spec really has."""
    if not ref.startswith("#/"):
        return False  # no external documents in this contract, by policy
    node: object = spec
    for token in ref[2:].split("/"):
        # RFC 6901 escapes; neither appears in a FastAPI component name today,
        # but decoding them costs nothing and a wrong pointer must not pass.
        key = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    return True


def test_every_ref_in_the_live_spec_resolves() -> None:
    spec = _spec()
    refs = list(_iter_refs(spec))
    # Non-vacuity: an empty walk would pass this test for the wrong reason.
    assert len(refs) > 50, f"expected the spec to be full of $refs, found {len(refs)}"

    dangling = sorted({ref for _, ref in refs if not _resolve(spec, ref)})
    assert not dangling, (
        "the live OpenAPI spec references components that do not exist: "
        + ", ".join(dangling)
        + "\nSee app/models/errors.py: a hand-declared 422 that removes the last"
        " auto-generated one deletes the HTTPValidationError component."
    )


def test_the_shared_422_entry_still_names_both_bodies() -> None:
    """The specific pair the guard above exists for, pinned by name.

    ``test_every_ref_in_the_live_spec_resolves`` would also pass if the
    ``anyOf`` were quietly dropped to a single ``$ref``; this fails instead.
    """
    spec = _spec()
    responses = spec["paths"]["/api/import"]["post"]["responses"]  # type: ignore[index]
    schema = responses["422"]["content"]["application/json"]["schema"]
    assert [entry["$ref"] for entry in schema["anyOf"]] == [
        "#/components/schemas/ErrorDetail",
        "#/components/schemas/HTTPValidationError",
    ]
    for ref in ("ErrorDetail", "HTTPValidationError"):
        assert _resolve(spec, f"#/components/schemas/{ref}")
