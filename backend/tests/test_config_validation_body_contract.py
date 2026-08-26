"""Guard: the 422 both config-save routes DECLARE is what they really SEND.

Until this file existed, ``POST /api/config/save`` documented its 422 as
FastAPI's ``HTTPValidationError`` - not by decision, but because an undeclared
422 is the one FastAPI generates. That shape's items are
``{loc: (string|integer)[], msg, type}``. What ``app/beets/config_editor.py``
actually raises with, both for a ruamel parse failure and for a known-keys
schema failure, is a LIST of ``ValidationErrorItem``: ``loc`` is a plain STRING
and each row carries ``line`` and ``column`` - the same row shape the editor's
gutter already consumes from ``POST /api/config/validate``'s 200. No live client
reads this save 422 body today (the save mutation's error handler covers 409 only
and the 422 body is discarded), but declaring it as ``HTTPValidationError``
mistypes the generated TypeScript - the exact wrongly-typed-status failure
``app/models/errors.py`` argues is worse than an undeclared one.

``POST /api/config/naming/save`` raises its own 422 too, and NOT with the same
item shape: ``save_naming`` builds three-key dicts by hand (``loc`` is a form
row, ``replace[<index>]``) with no ``line``/``column`` anywhere. It gets its own
model rather than being folded into the other one, which
``test_the_naming_422_item_is_not_the_config_save_item_shape`` measures instead
of asserting in prose.

Both routes take a Pydantic request body, so FastAPI's own validation 422 is
ALSO reachable on each - verified here against a real malformed request, not
assumed. That is why each entry is an ``anyOf`` over both bodies rather than a
single model, which would have replaced FastAPI's entry and traded one wrongly
typed status for another.

The shape of the pin follows ``tests/test_config_conflict_body_contract.py``
(same two halves, same reasons; see ``tests/openapi_body_contract.py``):

1. the schema the LIVE spec declares for 422 on each save route, with every
   ``$ref`` inlined, must be the ``anyOf`` of that route's own model and
   FastAPI's ``HTTPValidationError``;
2. a REAL 422 - driven through the endpoint by an actual YAML parse error, an
   actual schema violation, and an actual bad regex - must validate against that
   model, with the key sets pinned exactly at every level so a field the server
   sends and the contract omits is a failure rather than a silently ignored key.

Mutation-proved: deleting the ``422`` entry from either route's ``responses``
in ``app/api/config_.py`` - i.e. going back to FastAPI's plain
``HTTPValidationError`` - fails half 1 for that route.
"""

from __future__ import annotations

from typing import Final, NamedTuple

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from app.main import app
from app.models.config_editor import ValidationErrorItem
from app.models.errors import (
    ConfigValidationErrorDetail,
    ErrorDetail,
    NamingRuleError,
    NamingValidationErrorDetail,
)
from tests.openapi_body_contract import inline_refs, model_schema

#: A ``base_sha256`` no real ``config.yaml`` can hash to. Every case below sends
#: it, so a body that somehow PASSED validation would answer 409 rather than
#: rewriting the fixture's file: the 422s under test are proved to come from the
#: parse / schema / regex steps, which all run before the compare-and-swap.
_STALE_CAS_TOKEN: Final = "0" * 64

_VALIDATION_ERROR_REF: Final = "#/components/schemas/HTTPValidationError"


class _Case(NamedTuple):
    """One route's own 422, and a request that really provokes it."""

    #: The pytest id, and what the request is wrong about.
    name: str
    path: str
    body: dict[str, object]
    #: The model declared for that route's own 422 body, and for one item of it.
    #: Spelled as a union of the two concrete pairs rather than ``type[BaseModel]``
    #: so the assertions below can read ``.detail`` / ``.loc`` off what
    #: ``model_validate`` returns.
    model: type[ConfigValidationErrorDetail] | type[NamingValidationErrorDetail]
    item_model: type[ValidationErrorItem] | type[NamingRuleError]
    #: The ``type`` discriminator the item carries on the wire.
    item_type: str
    #: Whether the item points at a position in the YAML document. Only the
    #: config-save rows do; a bad regex comes from a form row, not a line.
    positioned: bool


_CASES: Final = (
    _Case(
        name="yaml-parse",
        path="/api/config/save",
        body={"yaml_text": "directory: [1,\n", "base_sha256": _STALE_CAS_TOKEN},
        model=ConfigValidationErrorDetail,
        item_model=ValidationErrorItem,
        item_type="yaml_parse",
        positioned=True,
    ),
    _Case(
        name="schema-violation",
        path="/api/config/save",
        body={"yaml_text": "directory: 5\nlibrary: library.db\n", "base_sha256": _STALE_CAS_TOKEN},
        model=ConfigValidationErrorDetail,
        item_model=ValidationErrorItem,
        item_type="path_type",
        positioned=True,
    ),
    _Case(
        name="bad-replace-regex",
        path="/api/config/naming/save",
        body={
            "rules": [],
            "replace": [{"pattern": "[", "replacement": "_"}],
            "base_sha256": _STALE_CAS_TOKEN,
        },
        model=NamingValidationErrorDetail,
        item_model=NamingRuleError,
        item_type="regex",
        positioned=False,
    ),
)

#: ``(path, model)`` for the contract half - the naming save's 422 is a
#: DIFFERENT model from the config save's, so both are pinned separately. A
#: future split or merge of the two entries has to keep both true.
_DECLARATIONS: Final = (
    ("/api/config/save", ConfigValidationErrorDetail),
    ("/api/config/naming/save", NamingValidationErrorDetail),
)


class _FastAPIValidationErrorItem(BaseModel):
    """One row of FastAPI's ``HTTPValidationError``, as the spec declares it.

    ``loc`` is an ARRAY of path segments - that is the whole difference, and it
    is what makes this model reject a config-editor 422 body. Pinned as faithful
    by ``test_a_malformed_request_body_still_answers_with_the_validation_shape``,
    which validates a REAL FastAPI 422 against it rather than trusting the copy.
    """

    loc: list[str | int]
    msg: str
    type: str


class _FastAPIValidationError(BaseModel):
    """FastAPI's generated 422 body: what an UNDECLARED 422 documents."""

    detail: list[_FastAPIValidationErrorItem]


def _declared_422_schema(path: str) -> tuple[object, object]:
    """The raw and the inlined 422 response schema the LIVE spec gives ``path``."""
    spec = app.openapi()
    paths = spec["paths"]
    assert path in paths, f"{path} is missing from the live spec entirely"
    responses = paths[path]["post"]["responses"]
    assert "422" in responses, f"{path} has no 422 at all, which FastAPI would have generated"
    content = responses["422"].get("content")
    assert content, (
        f"{path}'s 422 has a description but no content, so openapi-typescript renders"
        " `content?: never` for a body the client must read (see app/models/errors.py)"
    )
    raw = content["application/json"]["schema"]
    components = spec["components"]["schemas"]
    return raw, inline_refs(raw, {str(key): value for key, value in components.items()})


@pytest.mark.parametrize(("path", "model"), _DECLARATIONS, ids=[p for p, _ in _DECLARATIONS])
def test_the_declared_422_schema_is_both_bodies_the_route_can_send(
    path: str, model: type[BaseModel]
) -> None:
    """Half 1: the contract offers the route's OWN body and FastAPI's, in that order.

    The own-body arm is compared structurally against the model rather than by
    name: a model that keeps its name but loses ``line`` is the same defect as
    declaring ``HTTPValidationError``, and the frontend cannot tell them apart.
    """
    raw, declared = _declared_422_schema(path)
    assert isinstance(raw, dict)
    assert isinstance(declared, dict)
    arms = declared.get("anyOf")
    raw_arms = raw.get("anyOf")
    single_shape = (
        f"{path} declares its 422 as {raw}, a single shape; both the route's own"
        " body and FastAPI's request-validation body are reachable, so dropping"
        " either one types a body the client reads as something it is not"
    )
    assert isinstance(arms, list), single_shape
    assert isinstance(raw_arms, list), single_shape
    assert len(arms) == 2, f"{path} declares {len(arms)} 422 bodies, expected exactly 2"
    assert arms[0] == model_schema(model), (
        f"{path}'s first 422 body is not the {model.__name__} shape it really sends"
    )
    assert raw_arms[1] == {"$ref": _VALIDATION_ERROR_REF}, (
        f"{path} no longer references FastAPI's validation shape: {raw_arms[1]!r}"
    )


@pytest.mark.parametrize("case", _CASES, ids=[case.name for case in _CASES])
def test_the_real_422_body_validates_against_the_declared_model(
    client: TestClient, case: _Case
) -> None:
    """Half 2: the wire body really is that model, field for field.

    Key sets are pinned with ``==`` at every level, not with ``model_validate``
    alone: pydantic ignores extra keys, so a field the server sends and the
    contract does not name would otherwise pass unnoticed.
    """
    response = client.post(case.path, json=case.body)
    assert response.status_code == 422, f"expected the route's own 422, got {response.text}"

    body = response.json()
    assert set(body) == set(case.model.model_fields)
    assert body["detail"], "a 422 with an empty error list would tell the editor nothing"
    for item in body["detail"]:
        assert set(item) == set(case.item_model.model_fields)

    items = case.model.model_validate(body).detail
    first = items[0]
    assert isinstance(first, case.item_model)
    assert first.type == case.item_type
    assert isinstance(first.loc, str), "loc is a plain string here, not FastAPI's array of segments"
    if case.positioned:
        # The two fields SettingsBeetsPage.tsx reads to place a CodeMirror
        # marker. Undeclared until this contract existed, and null here would
        # mean the declaration is honest but the marker never lands.
        assert isinstance(first, ValidationErrorItem)
        assert first.line is not None
        assert first.column is not None


@pytest.mark.parametrize("case", _CASES, ids=[case.name for case in _CASES])
@pytest.mark.parametrize("model", [_FastAPIValidationError, ErrorDetail])
def test_the_previously_declared_shapes_reject_these_bodies(
    client: TestClient, case: _Case, model: type[BaseModel]
) -> None:
    """Control: what these routes used to promise cannot describe what they send.

    ``_FastAPIValidationError`` is the shape an undeclared 422 documents (its
    ``loc`` is an array); ``ErrorDetail`` is the shape ``validation_or_detail_422``
    would have given them (its ``detail`` is a sentence). Both are rejected by a
    real body, which is what turns "neither could be reused" from a docstring
    claim into a measured one.
    """
    response = client.post(case.path, json=case.body)
    assert response.status_code == 422, f"expected the route's own 422, got {response.text}"

    # Decoded outside the block so it holds exactly one call that can throw -
    # otherwise a decoding failure would read as a validation one (Sonar S5778).
    body = response.json()
    with pytest.raises(ValidationError):
        model.model_validate(body)


@pytest.mark.parametrize("path", [path for path, _ in _DECLARATIONS])
def test_a_malformed_request_body_still_answers_with_the_validation_shape(
    client: TestClient, path: str
) -> None:
    """The second arm of each ``anyOf`` is real, not a defensive guess.

    Both routes take a Pydantic body, so a request that never reaches the beets
    adapter is answered by FastAPI itself. This also pins
    ``_FastAPIValidationError`` above as a faithful copy of that shape: the
    control test would prove nothing against a model FastAPI does not use.

    Validated, not key-set-pinned: FastAPI's real rows carry ``input`` (and
    sometimes ``ctx``/``url``) on top of the three keys its own
    ``HTTPValidationError`` component declares. That gap is FastAPI's contract,
    not this app's, and pinning it here would fail on a FastAPI upgrade that
    documents them.
    """
    response = client.post(path, json={"not": "a valid body"})
    assert response.status_code == 422

    body = response.json()
    detail = _FastAPIValidationError.model_validate(body).detail
    assert detail, "a request-validation 422 always names at least one bad field"
    assert detail[0].loc[0] == "body"


def test_the_naming_422_item_is_not_the_config_save_item_shape(client: TestClient) -> None:
    """Why the naming save got a model of its own, measured on both wires.

    ``NamingRuleError`` is three keys where ``ValidationErrorItem`` is five.
    Declaring the naming 422 with the five-field model would promise a ``line``
    and a ``column`` that route can never send (the panel has no document to
    point into), and the key-set pin in half 2 would fail on the real body.
    """
    save, naming = (
        client.post(case.path, json=case.body)
        for case in _CASES
        if case.name in {"schema-violation", "bad-replace-regex"}
    )
    assert save.status_code == 422, save.text
    assert naming.status_code == 422, naming.text

    save_item = save.json()["detail"][0]
    naming_item = naming.json()["detail"][0]
    assert set(naming_item) == set(NamingRuleError.model_fields)
    assert set(save_item) - set(naming_item) == {"line", "column"}
    assert set(naming_item) < set(save_item), "the naming row is a strict subset, not a rival shape"
