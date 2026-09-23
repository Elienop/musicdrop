"""Guard: the 409 both config-save routes DECLARE is the body they really SEND.

``tests/test_route_status_declarations.py`` proves every status a route can
raise appears in the spec, but it reads response CODES only - an integer key in
``responses``. It is blind to the model behind that key, so declaring the
compare-and-swap 409 of ``POST /api/config/save`` as ``ErrorDetail`` would keep
that guard (and the rest of the suite) green while shipping exactly the failure
``app/models/errors.py`` argues is worse than leaving a status undeclared: a
WRONGLY-TYPED body. ``ErrorDetail`` types ``detail`` as a ``str``, so the two
fields the editor's conflict UI reads - ``current_yaml_text`` and
``current_sha256`` - would vanish from the generated client's types while the
server kept sending them.

``tests/test_openapi_spec_guard.py`` is not evidence about that: it fires on any
un-regenerated spec edit, correct or not, and goes green again the moment the
dump is re-run. It compares the artefact with the live app, never the app with
the wire.

So this file joins the two halves that a wrong model has to break:

1. the schema the LIVE spec declares for 409 on each save route, with every
   ``$ref`` inlined, must equal the schema ``ConfigSaveConflictDetail`` itself
   generates - a pin on WHICH model the route names, so swapping models,
   collapsing the anyOf, or dropping the content block fails it;
2. a REAL 409, provoked from each route with a stale CAS token, must validate
   against that same model, with the key sets pinned exactly at both levels so
   an extra or missing wire field is a failure rather than a silently ignored
   one.

Either half alone is weak: (1) without (2) pins the contract to a model nobody
has checked against the wire, and (2) without (1) passes no matter what the
route declares. Together, swapping the declared model fails (1) and swapping the
raised body fails (2).

Mutation-proved: declaring ``_SAVE_CAS_CONFLICT_RESPONSE["model"]`` as
``ErrorDetail`` in ``app/api/config_.py`` fails test (1) for both routes.

The third test is the control that makes "wrongly typed" a measured claim rather
than an assertion: the app's two other error models are shown to REJECT this
body, so neither could have been reused and the shape genuinely needed a model
of its own.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from app.main import app
from app.models.errors import (
    ConfigSaveConflict,
    ConfigSaveConflictDetail,
    ErrorDetail,
    StructuredErrorDetail,
)
from tests.openapi_body_contract import inline_refs, model_schema

#: A ``base_sha256`` no real ``config.yaml`` can hash to, so the CAS step (and
#: not the parse or schema-validate step before it) is what refuses the save.
_STALE_CAS_TOKEN: Final = "0" * 64

#: The two routes that run the same read-hash-compare under ``_SAVE_LOCK``
#: (``config_editor.save`` / ``config_editor.save_naming``) and answer with the
#: same conflict body. Both are declared from one shared ``responses`` entry in
#: ``app/api/config_.py``; asserting on both is what stops a future split of
#: that constant from leaving one of them wrong.
_SAVE_ROUTES: Final = ("/api/config/save", "/api/config/naming/save")


def _save_request(path: str, yaml_text: str) -> dict[str, object]:
    """A body for ``path`` that is valid everywhere except the CAS token.

    ``save`` parses and schema-validates before it hashes, so it is handed the
    file's own current text; ``save_naming`` validates rules/replace first, and
    two empty lists pass that. Either way the 409 under test is the CAS one.
    """
    if path == "/api/config/save":
        return {"yaml_text": yaml_text, "base_sha256": _STALE_CAS_TOKEN}
    return {"rules": [], "replace": [], "base_sha256": _STALE_CAS_TOKEN}


def _declared_409_schema(path: str) -> tuple[object, object]:
    """The raw and the inlined 409 response schema the LIVE spec gives ``path``."""
    spec = app.openapi()
    paths = spec["paths"]
    assert path in paths, f"{path} is missing from the live spec entirely"
    responses = paths[path]["post"]["responses"]
    assert "409" in responses, (
        f"{path} declares no 409; its compare-and-swap conflict is the only status"
        " the editor's Reload / Overwrite-anyway UI can act on"
    )
    content = responses["409"].get("content")
    assert content, (
        f"{path}'s 409 has a description but no content, so openapi-typescript renders"
        " `content?: never` for a body the client must read (see app/models/errors.py)"
    )
    raw = content["application/json"]["schema"]
    components = spec["components"]["schemas"]
    return raw, inline_refs(raw, {str(key): value for key, value in components.items()})


@pytest.mark.parametrize("path", _SAVE_ROUTES)
def test_the_declared_409_schema_is_the_conflict_model(path: str) -> None:
    """Half 1: the contract's 409 body type is ``ConfigSaveConflictDetail``.

    Compared against the model's own ``model_json_schema()`` rather than by
    component name: this catches a swap to a different model, a collapsed anyOf,
    or a dropped content block. It does NOT catch a same-named model losing a
    field (both sides derive from the same schema and move together) - that is
    the real-body half's job.
    """
    raw, declared = _declared_409_schema(path)
    assert declared == model_schema(ConfigSaveConflictDetail), (
        f"{path} declares its 409 as {raw}, which is not the"
        " ConfigSaveConflictDetail shape the route really sends; a wrongly-typed"
        " status is worse than an undeclared one (app/models/errors.py)"
    )


@pytest.mark.parametrize("path", _SAVE_ROUTES)
def test_the_real_409_body_validates_against_the_declared_model(
    client: TestClient, beets_library_config_path: Path, path: str
) -> None:
    """Half 2: the wire body really is that model, field for field.

    Key sets are pinned with ``==`` at both levels, not with ``model_validate``
    alone: pydantic ignores extra keys, so a field the server sends and the
    contract does not name would otherwise pass unnoticed.
    """
    on_disk = beets_library_config_path.read_bytes()
    response = client.post(path, json=_save_request(path, on_disk.decode()))
    assert response.status_code == 409, f"expected the CAS conflict, got {response.text}"

    body = response.json()
    assert set(body) == set(ConfigSaveConflictDetail.model_fields)
    assert set(body["detail"]) == set(ConfigSaveConflict.model_fields)

    conflict = ConfigSaveConflictDetail.model_validate(body).detail
    assert conflict.detail == "File changed on disk"
    assert conflict.current_yaml_text == on_disk.decode()
    assert conflict.current_sha256 == hashlib.sha256(on_disk).hexdigest()


@pytest.mark.parametrize("model", [ErrorDetail, StructuredErrorDetail])
def test_the_other_error_models_reject_the_conflict_body(
    client: TestClient, beets_library_config_path: Path, model: type[BaseModel]
) -> None:
    """Control: neither existing error model could have described this body.

    ``ErrorDetail`` wants ``detail`` to be a sentence and
    ``StructuredErrorDetail`` wants ``{message, recovery}`` under it. Declaring
    either would type a body the client reads as something it is not - which is
    the justification ``ConfigSaveConflictDetail`` exists on, measured here
    rather than asserted in a docstring.
    """
    on_disk = beets_library_config_path.read_text()
    response = client.post("/api/config/save", json=_save_request("/api/config/save", on_disk))
    assert response.status_code == 409, f"expected the CAS conflict, got {response.text}"

    # `response.json()` is hoisted out so the block holds exactly one call that
    # can throw - otherwise a decoding failure would read as a validation one.
    body = response.json()
    with pytest.raises(ValidationError):
        model.model_validate(body)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "/api/config/save",
            (
                422,
                {
                    "detail": [
                        {
                            "loc": "",
                            "msg": "config.yaml is not UTF-8.",
                            "type": "config_on_disk",
                            "line": None,
                            "column": None,
                        }
                    ]
                },
            ),
        ),
        (
            "/api/config/naming/save",
            (
                422,
                {
                    "detail": [
                        {"loc": "", "msg": "config.yaml is not UTF-8.", "type": "config_on_disk"}
                    ]
                },
            ),
        ),
    ],
)
def test_a_stale_save_to_a_file_that_is_not_utf8_is_not_a_500(
    client: TestClient,
    beets_library_config_path: Path,
    path: str,
    expected: tuple[int, dict[str, object]],
) -> None:
    """Strict UTF-8 decoding of the file on disk made this a bare 500."""
    valid = beets_library_config_path.read_text(encoding="utf-8")
    beets_library_config_path.write_bytes(b"a: 1\nb: \xff\n")

    response = client.post(path, json=_save_request(path, valid))

    assert (response.status_code, response.json()) == expected
    assert beets_library_config_path.read_bytes() == b"a: 1\nb: \xff\n"
