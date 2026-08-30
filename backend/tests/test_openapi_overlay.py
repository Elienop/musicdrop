"""Pin the middleware 400/401/403/413 overlay against the LIVE served spec.

The guards (``app/host_guard.py``, ``app/auth/gate.py``, ``app/origin_guard.py``,
``app/body_limit.py``) reject requests before the router;
``app/openapi_overlay.py`` declares their statuses on every operation. These
tests read ``/openapi.json`` off the running
app — the same contract the frontend generates its types from — and pin that
every route, now and added later, advertises the statuses it can actually get,
with the real ``ErrorDetail`` body shape, without disturbing anything FastAPI
declared itself (the 422 above all).
"""

from collections.abc import Iterator

from fastapi.testclient import TestClient

from app.main import app
from app.openapi_overlay import (
    _BODY_LIMIT_413,
    _ORIGIN_GUARD_403,
    overlay_middleware_responses,
)
from app.origin_guard import UNSAFE_METHODS

_ERROR_DETAIL_REF = "#/components/schemas/ErrorDetail"


def _as_dict(value: object) -> dict[str, object]:
    """Narrow an arbitrary JSON value to a string-keyed dict (``{}`` otherwise)."""
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def _spec() -> dict[str, object]:
    # Deliberately NOT the conftest ``client`` fixture: the spec needs no beets
    # library, and that fixture would drag one in (see test_openapi_spec_guard).
    client = TestClient(app)
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    assert isinstance(spec, dict)
    return {str(key): item for key, item in spec.items()}


def _operations() -> Iterator[tuple[str, str, dict[str, object]]]:
    """Every (method, path, operation) in the live spec, in schema order.

    Selected by SHAPE (a dict carrying ``responses``), never by a method list:
    a path item's non-operation keys (``parameters``, ``summary``) carry no
    ``responses``, and a mirrored method list would share any blind spot the
    overlay's own ``_HTTP_METHODS`` develops — the point of these pins is to
    catch that, not inherit it.
    """
    for path, path_item in _as_dict(_spec().get("paths")).items():
        for method, operation in _as_dict(path_item).items():
            op = _as_dict(operation)
            if "responses" in op:
                yield method, path, op


def _assert_error_detail(operation: dict[str, object], method: str, path: str, status: str) -> None:
    entry = _as_dict(_as_dict(operation.get("responses")).get(status))
    content = _as_dict(entry.get("content"))
    media = _as_dict(content.get("application/json"))
    schema = _as_dict(media.get("schema"))
    assert schema.get("$ref") == _ERROR_DETAIL_REF, f"{method.upper()} {path} {status}: {entry!r}"


def test_every_write_operation_declares_403_with_error_detail() -> None:
    write_methods = {method.lower() for method in UNSAFE_METHODS}
    checked = 0
    for method, path, operation in _operations():
        if method in write_methods:
            checked += 1
            _assert_error_detail(operation, method, path, "403")
        else:
            # Negative pin, scoped to what the overlay owns: a read operation
            # may declare its OWN 403 for a route-level reason, but never the
            # origin guard's — the guard checks UNSAFE_METHODS only.
            entry = _as_dict(_as_dict(operation.get("responses")).get("403"))
            assert entry.get("description") != _ORIGIN_GUARD_403, (
                f"{method.upper()} {path} carries the origin-guard 403 the guard never produces"
            )
    assert checked > 0


def test_every_operation_declares_400_with_error_detail() -> None:
    checked = 0
    for method, path, operation in _operations():
        checked += 1
        _assert_error_detail(operation, method, path, "400")
    assert checked > 0


def test_413_follows_the_request_body_and_nothing_else() -> None:
    with_body = 0
    without_body = 0
    for method, path, operation in _operations():
        if "requestBody" in operation:
            with_body += 1
            _assert_error_detail(operation, method, path, "413")
        else:
            # Negative pin, chosen programmatically (every bodyless op counts)
            # and scoped to the OVERLAY's entry: a raw-body route (e.g.
            # PUT /api/playlists/{id}/artwork reads ``await request.body()``,
            # so FastAPI emits no requestBody) really returns 413 and may one
            # day declare its own — only the body-limit overlay text is banned.
            without_body += 1
            entry = _as_dict(_as_dict(operation.get("responses")).get("413"))
            assert entry.get("description") != _BODY_LIMIT_413, (
                f"{method.upper()} {path} carries the body-limit 413 without a requestBody"
            )
    assert with_body > 0, "expected at least one bodied operation"
    assert without_body > 0, "expected at least one bodyless operation"


def test_preexisting_richer_403_is_preserved() -> None:
    fetch = _as_dict(_as_dict(_spec().get("paths")).get("/api/artists/image/fetch"))
    post = _as_dict(fetch.get("post"))
    entry = _as_dict(_as_dict(post.get("responses")).get("403"))
    assert (
        entry.get("description") == "Artist images are turned off, or the request is cross-origin."
    )
    _assert_error_detail(post, "post", "/api/artists/image/fetch", "403")


#: What a 422 arm may name. ``HTTPValidationError`` is FastAPI's own; the other
#: three are the bodies routes really raise with - a sentence (``ErrorDetail``)
#: or, for the two config-editor saves, a LIST of per-error rows whose own model
#: says which shape (``app/models/errors.py``).
_ALLOWED_422_ARMS = frozenset(
    {
        "HTTPValidationError",
        "ErrorDetail",
        "ConfigValidationErrorDetail",
        "NamingValidationErrorDetail",
    }
)


def _arm_name(schema: dict[str, object]) -> str:
    """What one 422 arm names: its ``$ref`` target, or an inline schema's title.

    Inline is not a defect here. A raw response entry registers no component, so
    a model used ONLY by such an entry has to be inlined or its ``$ref`` would
    dangle (``app/models/errors.py::_inlined_json_schema``); the title pydantic
    writes is the model's name either way.
    """
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return ref.rsplit("/", 1)[-1]
    return str(schema.get("title"))


def _schema_arms(schema: dict[str, object]) -> list[str]:
    """The body shapes a response schema offers: one, or an ``anyOf`` of them.

    A route that raises its OWN 422 returns two body shapes under that status
    and documents both as an ``anyOf`` (see ``app/models/errors.py``); every
    other 422 is FastAPI's single generated ref.
    """
    if "$ref" in schema:
        return [_arm_name(schema)]
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        return [_arm_name(_as_dict(entry)) for entry in any_of]
    return []


def test_422_entries_stay_fastapi_validation_and_are_never_invented() -> None:
    declared = 0
    for method, path, operation in _operations():
        responses = _as_dict(operation.get("responses"))
        if "422" in responses:
            declared += 1
            content = _as_dict(_as_dict(responses.get("422")).get("content"))
            media = _as_dict(content.get("application/json"))
            arms = _schema_arms(_as_dict(media.get("schema")))
            # The validation shape is never traded away: a route may ADD its own
            # body to the 422, never replace FastAPI's with it.
            assert "HTTPValidationError" in arms, (
                f"{method.upper()} {path} 422 no longer references HTTPValidationError: {arms!r}"
            )
            assert set(arms) <= _ALLOWED_422_ARMS, (
                f"{method.upper()} {path} 422 references an unexpected schema: {arms!r}"
            )
        else:
            # FastAPI puts 422 exactly on operations with something to validate
            # (a body or parameters); the overlay must not add it anywhere else.
            assert "requestBody" not in operation, (
                f"{method.upper()} {path} has a body/params but lost its 422"
            )
            assert "parameters" not in operation, (
                f"{method.upper()} {path} has a body/params but lost its 422"
            )
    assert declared > 0


def test_overlay_is_idempotent_and_takes_nothing_away() -> None:
    schema: dict[str, object] = {
        "paths": {
            "/x": {
                "get": {
                    "responses": {
                        "400": {"description": "a custom 400 the overlay must keep"},
                        "403": {"description": "a custom 403 the overlay must keep"},
                        "422": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
                                }
                            }
                        },
                    }
                },
                "post": {
                    "requestBody": {},
                    "responses": {"200": {"description": "ok"}},
                },
            }
        }
    }
    once = overlay_middleware_responses(schema)
    assert once is schema
    twice = overlay_middleware_responses(once)
    assert twice is once

    get = _as_dict(_as_dict(_as_dict(once.get("paths")).get("/x")).get("get"))
    get_responses = _as_dict(get.get("responses"))
    assert get_responses.get("400") == {"description": "a custom 400 the overlay must keep"}
    assert get_responses.get("403") == {"description": "a custom 403 the overlay must keep"}
    assert _as_dict(_as_dict(get_responses.get("422")).get("content")).get("application/json") == {
        "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
    }
    assert "413" not in get_responses  # bodyless: the overlay adds no 413

    post = _as_dict(_as_dict(_as_dict(once.get("paths")).get("/x")).get("post"))
    _assert_error_detail(post, "post", "/x", "400")
    _assert_error_detail(post, "post", "/x", "403")
    _assert_error_detail(post, "post", "/x", "413")

    # No dangling $ref: the overlay guarantees its target exists even though
    # this minimal schema declared nothing.
    schemas = _as_dict(_as_dict(once.get("components")).get("schemas"))
    assert "ErrorDetail" in schemas
    error_detail: object = schemas["ErrorDetail"]
    assert isinstance(error_detail, dict)
    assert error_detail.get("required") == ["detail"]
