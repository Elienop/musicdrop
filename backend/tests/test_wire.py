"""Unit tests for ``app.wire`` — the one scrubber that keeps the wire encodable.

POSIX filenames are bytes, so a name that is not valid UTF-8 survives
``os.fsdecode`` only as lone surrogates (the ``surrogateescape`` handler maps
each undecodable byte to U+DC80..U+DCFF). Starlette renders JSON with
``ensure_ascii=False`` and then encodes UTF-8 *strictly*, so one such string
anywhere in a body raises ``UnicodeEncodeError`` and the endpoint 500s.

The rules pinned here: valid text of any script passes through byte-for-byte,
only genuinely undecodable bytes become U+FFFD, and the scrubber never raises
whatever it is handed.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.wire import (
    PLACEHOLDER,
    AmbiguousDisplayName,
    SurrogateSafeJSONResponse,
    display_path,
    resolve_display_path,
    scrub_content,
    wire_safe,
)

# latin-1 "Café Album": 0xe9 is not valid UTF-8, so os.fsdecode yields a lone
# surrogate and the string can never be encoded back out strictly.
BAD_BYTES = b"Caf\xe9 Album"
BAD_TEXT = "Caf\udce9 Album"
BAD_DISPLAY = f"Caf{PLACEHOLDER} Album"


# ----- wire_safe -----


def test_wire_safe_replaces_an_undecodable_byte_with_the_placeholder() -> None:
    assert wire_safe(BAD_TEXT) == BAD_DISPLAY


def test_wire_safe_emits_one_placeholder_per_invalid_sequence() -> None:
    # Two bytes that cannot form one sequence stay two placeholders (0xe9 is a
    # 3-byte lead, 0xea cannot continue it).
    assert wire_safe("\udce9\udcea") == PLACEHOLDER * 2


def test_wire_safe_collapses_a_maximal_invalid_sequence_into_one_placeholder() -> None:
    # The display form is LOSSY and does NOT distinguish damaged names: 0x80 is
    # a valid continuation for the 0xe9 lead, so both of these decode to exactly
    # "Caf<placeholder> Album". Pinned because the round-trip resolution depends
    # on it — this collision is precisely why an ambiguous name must 409 rather
    # than pick a folder.
    one_byte = wire_safe(os.fsdecode(b"Caf\xe9 Album"))
    two_bytes = wire_safe(os.fsdecode(b"Caf\xe9\x80 Album"))
    assert one_byte == two_bytes == BAD_DISPLAY
    assert one_byte.count(PLACEHOLDER) == 1


@pytest.mark.parametrize(
    "text",
    [
        "",
        "plain ascii",
        "Café Album",  # precomposed é — valid UTF-8, must NOT be touched
        "Sigur R\u00f3s \u2013 ( )",  # accented Latin + an en-dash
        "日本語",  # CJK
        "\U0001f3b5 track",  # emoji (astral plane)
        "أغنية",  # Arabic
    ],
)
def test_wire_safe_leaves_valid_unicode_untouched(text: str) -> None:
    assert wire_safe(text) == text


@pytest.mark.parametrize(
    "text",
    [
        BAD_TEXT,
        "\udce9",
        "\ud800",  # a lone high surrogate — NOT something fsdecode produces
        "\udfff",
        "ok \ud800 mixed \udce9 tail",
        "\udce9" * 100,
        PLACEHOLDER,
    ],
)
def test_wire_safe_never_raises_and_always_yields_encodable_text(text: str) -> None:
    out = wire_safe(text)
    assert out.encode("utf-8")  # the whole point: this must not raise
    assert PLACEHOLDER in out


def test_wire_safe_is_idempotent() -> None:
    once = wire_safe(BAD_TEXT)
    assert wire_safe(once) == once


# ----- display_path -----


def test_display_path_accepts_bytes_str_and_pathlike() -> None:
    assert display_path(BAD_BYTES) == BAD_DISPLAY
    assert display_path(BAD_TEXT) == BAD_DISPLAY
    assert display_path(Path("/music/Sigur Rós")) == "/music/Sigur Rós"


def test_display_path_keeps_a_valid_non_ascii_name_exact() -> None:
    name = "/music/日本/Café"
    assert display_path(name.encode("utf-8")) == name


# ----- scrub_content / the JSON sink -----


def test_scrub_content_walks_nested_structures() -> None:
    scrubbed = scrub_content({BAD_TEXT: [{"error": BAD_TEXT}, 3, None, True]})
    assert scrubbed == {BAD_DISPLAY: [{"error": BAD_DISPLAY}, 3, None, True]}


def test_safe_json_response_renders_a_surrogate_instead_of_raising() -> None:
    body = SurrogateSafeJSONResponse({"from_path": BAD_TEXT}).render({"from_path": BAD_TEXT})
    assert json.loads(body.decode("utf-8")) == {"from_path": BAD_DISPLAY}


def test_safe_json_response_warns_when_it_degrades(caplog: pytest.LogCaptureFixture) -> None:
    # Silent replacement would hide a genuine bug that put non-encodable text in
    # a body — the one real cost of catching this at the sink.
    # Built with a clean body on purpose: ``Response.__init__`` renders too, so
    # constructing with the bad payload would log twice and the count would not
    # mean what it looks like it means.
    response = SurrogateSafeJSONResponse({"p": "clean"})
    with caplog.at_level(logging.WARNING, logger="app.wire"):
        response.render({"p": BAD_TEXT})
    assert [record.levelname for record in caplog.records] == ["WARNING"]


def test_safe_json_response_stays_quiet_on_an_ordinary_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response = SurrogateSafeJSONResponse({"p": "clean"})
    with caplog.at_level(logging.WARNING, logger="app.wire"):
        response.render({"p": "fine"})
    assert caplog.records == []


def test_safe_json_response_is_byte_identical_for_ordinary_payloads() -> None:
    # The scrub is a failure path only — a normal body must serialise exactly as
    # Starlette would, non-ASCII included (ensure_ascii=False is preserved).
    from starlette.responses import JSONResponse

    payload = {"album": "Sigur R\u00f3s \u2013 ( )", "n": 3, "ok": True, "x": None}
    assert SurrogateSafeJSONResponse(payload).render(payload) == JSONResponse(payload).render(
        payload
    )


def test_every_api_route_resolves_to_the_safe_response_class() -> None:
    """Dropping the app-level default silently reopens the bug on every endpoint.

    Two traps make the obvious version of this test vacuous. ``app.routes`` holds
    ``_IncludedRouter`` objects, not flattened ``APIRoute``s, so an
    ``isinstance(route, APIRoute)`` comprehension yields an empty list and passes
    for the wrong reason. And ``APIRoute.response_class`` stays a
    ``DefaultPlaceholder`` forever: FastAPI merges the app-level default into an
    ``_EffectiveRouteContext`` built LAZILY on the first dispatch and cached by
    routes-version (``effective_candidates`` -> ``_build_effective_context``), so
    the routers must be warmed with a real request before being inspected.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    assert app.router.default_response_class is SurrogateSafeJSONResponse
    TestClient(app).get("/api/health")  # warm the lazily-built effective contexts

    def resolved(routes: object) -> Iterator[object]:
        for route in routes:  # type: ignore[attr-defined]  # heterogeneous starlette route list
            included = getattr(route, "original_router", None)
            if included is not None:
                for candidate in route.effective_candidates():
                    if getattr(candidate, "original_router", None) is None:
                        yield candidate.response_class
                yield from resolved(included.routes)

    classes = set(resolved(app.routes))
    assert classes == {SurrogateSafeJSONResponse}, classes


def test_a_validation_422_does_not_echo_what_the_client_sent() -> None:
    """Every row is ``loc``/``msg``/``type`` — the schema's words, not the client's.

    App-wide rather than per-route, because two of the routes that take a body
    take a PASSWORD in it and a list of secret field names is a list somebody
    has to remember to add to. Those three keys are also exactly what FastAPI's
    ``HTTPValidationError`` component declares, so the wire body and the
    contract agree where they used to differ.

    ``/api/trash/restore`` stands in for "any route with a Pydantic body": the
    marker below is an ordinary string in an ordinary field, and the point is
    that no field is special.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    marker = "a-value-the-client-sent-marker"
    resp = TestClient(app).post("/api/trash/restore", json={"folder": {"nested": marker}})

    assert resp.status_code == 422, resp.text
    assert marker not in resp.text
    rows = resp.json()["detail"]
    assert rows, "a validation 422 names at least one bad field"
    for row in rows:
        assert set(row) == {"loc", "msg", "type"}, row


# ----- resolve_display_path (the inverse, for names the client sends back) -----


def test_resolve_display_path_prefers_the_literal_name(tmp_path: Path) -> None:
    (tmp_path / "Plain Album").mkdir()
    assert resolve_display_path(tmp_path, "Plain Album") == tmp_path / "Plain Album"


def test_resolve_display_path_maps_a_scrubbed_name_onto_the_real_entry(tmp_path: Path) -> None:
    raw = tmp_path.joinpath(BAD_BYTES.decode("utf-8", "surrogateescape"))
    raw.mkdir()
    resolved = resolve_display_path(tmp_path, BAD_DISPLAY)
    assert resolved == raw
    assert resolved.exists()


def test_resolve_display_path_walks_multiple_segments(tmp_path: Path) -> None:
    parent = tmp_path.joinpath(BAD_BYTES.decode("utf-8", "surrogateescape"))
    (parent / "CD1").mkdir(parents=True)
    assert resolve_display_path(tmp_path, f"{BAD_DISPLAY}/CD1") == parent / "CD1"


def test_resolve_display_path_refuses_two_entries_with_one_display_form(tmp_path: Path) -> None:
    # b"\xe9" and b"\xea" both scrub to U+FFFD. Picking one would delete or
    # import the wrong folder, so this must fail loudly.
    for bad in (b"Caf\xe9 Album", b"Caf\xea Album"):
        tmp_path.joinpath(bad.decode("utf-8", "surrogateescape")).mkdir()
    with pytest.raises(AmbiguousDisplayName):
        resolve_display_path(tmp_path, BAD_DISPLAY)


def test_resolve_display_path_refuses_a_real_placeholder_name_shadowing_a_damaged_twin(
    tmp_path: Path,
) -> None:
    # U+FFFD is a legal filename character, so `base / rel` can EXIST and still
    # be the wrong target: the damaged twin displays identically. Trusting the
    # literal short-circuits the ambiguity scan entirely.
    tmp_path.joinpath(BAD_BYTES.decode("utf-8", "surrogateescape")).mkdir()
    (tmp_path / BAD_DISPLAY).mkdir()
    with pytest.raises(AmbiguousDisplayName):
        resolve_display_path(tmp_path, BAD_DISPLAY)


def test_resolve_display_path_resolves_a_lone_real_placeholder_name(tmp_path: Path) -> None:
    # wire_safe is the identity on valid UTF-8, so the literal entry matches
    # itself in the scan and a name with no twin still resolves.
    target = tmp_path / BAD_DISPLAY
    target.mkdir()
    assert resolve_display_path(tmp_path, BAD_DISPLAY) == target


def test_resolve_display_path_survives_a_nul_behind_a_placeholder(tmp_path: Path) -> None:
    # os.scandir raises ValueError, not OSError, on an embedded NUL.
    resolved = resolve_display_path(tmp_path, f"{BAD_DISPLAY}\x00/{BAD_DISPLAY}")
    assert not resolved.exists()  # and, above all, it did not raise


def test_resolve_display_path_returns_the_literal_when_nothing_matches(tmp_path: Path) -> None:
    # No match = the caller's own existence check produces its normal 404.
    assert resolve_display_path(tmp_path, BAD_DISPLAY) == tmp_path / BAD_DISPLAY


def test_ambiguous_display_name_is_not_a_value_error() -> None:
    # Callers map ValueError to "not in Trash"; ambiguity must not be swallowed
    # by that branch — it needs its own, louder answer.
    assert not issubclass(AmbiguousDisplayName, ValueError)
