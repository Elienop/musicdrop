"""Tests for ``walk_get`` / ``walk_set`` / ``merge_preserve_secrets`` in the
Layer-3 config editor (Plan Task 3).

The merge fixtures intentionally go through ``parse_yaml`` so the maps are
real ruamel ``CommentedMap``s — that's the only shape the production save
flow ever passes to ``merge_preserve_secrets``, and it lets the tests prove
that the helpers don't accidentally trip over ruamel's dict subclassing or
its comment/lc metadata.
"""

from __future__ import annotations

from app.beets.config_editor import (
    REDACTED_TOMBSTONE,
    merge_preserve_secrets,
    parse_yaml,
    walk_get,
    walk_set,
)


def test_walk_get_nested() -> None:
    data = parse_yaml("a:\n  b:\n    c: hello\n")
    assert walk_get(data, ("a", "b", "c")) == "hello"
    assert walk_get(data, ("a", "missing")) is None


def test_walk_set_nested_overwrites() -> None:
    data = parse_yaml("a:\n  b: hello\n")
    walk_set(data, ("a", "b"), "world")
    assert walk_get(data, ("a", "b")) == "world"


def test_merge_preserves_unchanged_redacted() -> None:
    on_disk = parse_yaml("spotify:\n  client_secret: supersecret\n")
    new = parse_yaml(f"spotify:\n  client_secret: {REDACTED_TOMBSTONE}\n")
    merge_preserve_secrets(new, on_disk, redacted_paths=[("spotify", "client_secret")])
    assert walk_get(new, ("spotify", "client_secret")) == "supersecret"


def test_merge_writes_new_value_when_user_rotates() -> None:
    on_disk = parse_yaml("spotify:\n  client_secret: oldsecret\n")
    new = parse_yaml("spotify:\n  client_secret: newvalue\n")
    merge_preserve_secrets(new, on_disk, redacted_paths=[("spotify", "client_secret")])
    assert walk_get(new, ("spotify", "client_secret")) == "newvalue"


def test_merge_leaves_intentional_delete_alone() -> None:
    on_disk = parse_yaml("spotify:\n  client_secret: oldvalue\n  client_id: pubid\n")
    new = parse_yaml("spotify:\n  client_id: pubid\n")  # user deleted client_secret
    merge_preserve_secrets(new, on_disk, redacted_paths=[("spotify", "client_secret")])
    assert walk_get(new, ("spotify", "client_secret")) is None
