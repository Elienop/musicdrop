"""Shared machinery for pinning a DECLARED error body to the model behind it.

``tests/test_route_status_declarations.py`` reads response CODES only, so it is
blind to the model a status is declared with: a wrongly-typed body keeps it (and
the rest of the suite) green while the generated client parses one shape as
another - the failure ``app/models/errors.py`` argues is worse than leaving a
status undeclared.

Closing that needs two halves per status, and both need the same two helpers:

1. the schema the LIVE spec declares, with every ``$ref`` inlined, compared to
   the schema the model generates for itself (also inlined) - a pin on WHICH
   model the route names: swapping models, collapsing the anyOf, reversing the
   arms, or dropping the content block all fail it;
2. a REAL error body from that route, validated against the same model - the
   half that catches field-level drift (both sides of (1) derive from
   ``Model.model_json_schema()`` and would move together if a field were lost).

Used by ``tests/test_config_conflict_body_contract.py`` (the CAS 409) and
``tests/test_config_validation_body_contract.py`` (both save routes' 422).
"""

from __future__ import annotations

from pydantic import BaseModel


def inline_refs(node: object, components: dict[str, object], stack: tuple[str, ...] = ()) -> object:
    """``node`` with every local ``$ref`` replaced by the schema it names.

    The live spec puts nested models in ``components/schemas`` while
    ``model_json_schema`` puts them in ``$defs``; inlining both sides removes
    that difference so the comparison is about SHAPE, not about where each
    generator parks its definitions. A dangling or recursive ref fails here
    rather than quietly comparing two ``{"$ref": ...}`` stubs as equal.
    """
    if isinstance(node, list):
        return [inline_refs(item, components, stack) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        name = ref.rsplit("/", 1)[-1]
        assert name in components, f"the spec has no component for {ref}"
        assert ref not in stack, f"recursive $ref {ref} - cannot be inlined"
        return inline_refs(components[name], components, (*stack, ref))
    return {key: inline_refs(value, components, stack) for key, value in node.items()}


def drop_nulls(node: object) -> object:
    """``node`` without the keys whose value is ``None``.

    FastAPI finishes ``get_openapi`` with ``jsonable_encoder(...,
    exclude_none=True)``, which strips them from the whole document - so a field
    declared ``line: int | None = None`` contributes ``"default": null`` to the
    model's own schema and NOTHING to the spec. Normalising the model side the
    same way keeps the comparison about shape; without it every optional field
    reads as a mismatch.
    """
    if isinstance(node, list):
        return [drop_nulls(item) for item in node]
    if not isinstance(node, dict):
        return node
    return {key: drop_nulls(value) for key, value in node.items() if value is not None}


def model_schema(model: type[BaseModel]) -> object:
    """The fully inlined JSON schema ``model`` generates for itself."""
    schema: dict[str, object] = model.model_json_schema(ref_template="#/$defs/{model}")
    defs = schema.pop("$defs", {})
    assert isinstance(defs, dict)
    return drop_nulls(inline_refs(schema, {str(key): value for key, value in defs.items()}))
