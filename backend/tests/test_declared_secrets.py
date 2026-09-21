"""``BEETS_DECLARED_SECRETS`` against the installed beets, and what it masks.

The tripwire reads the beets SOURCE with ``ast`` and never imports a plugin:
spotify and tidal make network calls in ``__init__``. Each
``<view>[...].redact = True`` is resolved to a dotted path from its root:

- ``self.config`` is the plugin's section: the string passed to
  ``super().__init__`` in the class, else the module name (``BeetsPlugin``
  does ``name or self.__module__.split(".")[-1]``);
- ``config`` imported from beets is the global root;
- a module-level alias (``mpd_config = config["mpd"]``) adds its keys;
- a function parameter is resolved by ``_PARAMETER_ROOTS`` only, including one
  named ``config``, which would otherwise read as the global root.

A root this cannot resolve fails the test rather than being skipped, and so does
a declaration the walk does not recognise: each file's count of
``.redact = True`` / ``setattr(..., "redact", ...)`` in the TEXT must equal the
declarations the walk resolved.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from collections.abc import Iterator
from pathlib import Path

import beets
import pytest
import yaml

from app.beets.config_snapshot import build_config_snapshot
from app.beets.declared_secrets import BEETS_DECLARED_SECRETS
from app.beets.library import close_library
from app.beets.setup import setup_beets

#: fetchart's sources declare their keys in ``add_default_config(config)``,
#: called as ``source.add_default_config(self.config)`` by the plugin.
_PARAMETER_ROOTS = {("fetchart.py", "config"): ("fetchart",)}

#: Every spelling of a declaration in the text, whatever its AST shape: a plain
#: or multi-target assign, an annotated assign, and ``setattr``.
_DECLARATION_TEXT = re.compile(
    r"""\.redact\s*(?::[^=\n]*)?=\s*True\b|setattr\([^\n]*["']redact["']"""
)


def _package_dir(name: str) -> Path:
    spec = importlib.util.find_spec(name)
    assert spec is not None, name
    assert spec.submodule_search_locations, name
    return Path(next(iter(spec.submodule_search_locations)))


def _chain(node: ast.expr) -> tuple[ast.expr, tuple[str, ...]] | None:
    """Split ``root["a"]["b"]`` into ``(root, ("a", "b"))``; None if a key is
    not a string literal."""
    keys: list[str] = []
    while isinstance(node, ast.Subscript):
        if not (isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
            return None
        keys.insert(0, node.slice.value)
        node = node.value
    return node, tuple(keys)


def _plugin_section(cls: ast.ClassDef, module_name: str) -> str:
    for node in ast.walk(cls):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "__init__"
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Name)
            and node.func.value.func.id == "super"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            return node.args[0].value
    return module_name


def _aliases(tree: ast.Module) -> dict[str, tuple[str, ...]]:
    """Module-level ``name = config["a"]...`` assignments."""
    out: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Subscript)):
            continue
        chain = _chain(node.value)
        target = node.targets[0]
        if chain and isinstance(target, ast.Name) and isinstance(chain[0], ast.Name):
            if chain[0].id == "config":
                out[target.id] = chain[1]
    return out


def _bound_by_a_function(node: ast.AST, name: str, parents: dict[ast.AST, ast.AST]) -> bool:
    """``name`` is a parameter of a function enclosing ``node``."""
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            args = node.args
            every = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
            if any(a is not None and a.arg == name for a in every):
                return True
    return False


def _declarations(path: Path, module_name: str) -> list[tuple[str, tuple[str, ...]]]:
    """Every ``.redact = True`` in ``path``, resolved; fails on one it cannot resolve."""
    text = path.read_text(encoding="utf-8")
    found = list(_resolved(ast.parse(text), path, module_name))
    spelled = len(_DECLARATION_TEXT.findall(text))
    if spelled != len(found):
        pytest.fail(
            f"{path.parent.name}/{path.name}: {spelled} redact declaration(s) in the"
            f" text, {len(found)} resolved by the walk"
        )
    return found


def _resolved(
    tree: ast.Module, path: Path, module_name: str
) -> Iterator[tuple[str, tuple[str, ...]]]:
    aliases = _aliases(tree)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Attribute)
            and node.targets[0].attr == "redact"
            and isinstance(node.value, ast.Constant)
            and node.value.value is True
        ):
            continue
        where = f"{path.parent.name}/{path.name}:{node.lineno}"
        chain = _chain(node.targets[0].value)
        if chain is None:
            pytest.fail(f"{where}: a .redact declaration with a non-literal key")
        root, keys = chain
        if isinstance(root, ast.Attribute) and root.attr == "config":
            assert isinstance(root.value, ast.Name), where
            assert root.value.id == "self", where
            cls: ast.AST = node
            while not isinstance(cls, ast.ClassDef):
                cls = parents[cls]
            yield where, (_plugin_section(cls, module_name), *keys)
        elif isinstance(root, ast.Name) and (path.name, root.id) in _PARAMETER_ROOTS:
            yield where, (*_PARAMETER_ROOTS[path.name, root.id], *keys)
        elif isinstance(root, ast.Name) and _bound_by_a_function(node, root.id, parents):
            pytest.fail(f"{where}: a .redact declaration on the parameter {root.id!r}")
        elif isinstance(root, ast.Name) and root.id in aliases:
            yield where, (*aliases[root.id], *keys)
        elif isinstance(root, ast.Name) and root.id == "config":
            yield where, keys
        else:
            pytest.fail(f"{where}: cannot resolve the root of a .redact declaration")


def _declared_by_installed_beets() -> dict[tuple[str, ...], list[str]]:
    found: dict[tuple[str, ...], list[str]] = {}
    for package in ("beets", "beetsplug"):
        base = _package_dir(package)
        for path in sorted(base.rglob("*.py")):
            parts = path.relative_to(base).with_suffix("").parts
            module = parts[-2] if parts[-1] == "__init__" and len(parts) > 1 else parts[-1]
            for where, dotted in _declarations(path, module):
                found.setdefault(dotted, []).append(where)
    return found


def test_the_table_matches_what_the_installed_beets_declares() -> None:
    """A beets upgrade that adds, drops or moves a ``.redact`` fails here."""
    declared = _declared_by_installed_beets()
    # Control: the walk reaches a known declaration (smartplaylist.py:63).
    assert ("smartplaylist", "prefix") in declared
    missing = {".".join(p): w for p, w in declared.items() if p not in BEETS_DECLARED_SECRETS}
    stale = {".".join(p) for p in BEETS_DECLARED_SECRETS if p not in declared}
    assert missing == {}, "declared secret(s) not in BEETS_DECLARED_SECRETS"
    assert stale == set(), "table entries the installed beets no longer declares"


@pytest.mark.parametrize(
    "source",
    [
        'config["a"]["b"].redact = config["a"]["c"].redact = True\n',
        'config["a"]["b"].redact: bool = True\n',
        'setattr(config["a"]["b"], "redact", True)\n',
        'def add(config):\n    config["b"].redact = True\n',
    ],
    ids=["multi-target", "annotated", "setattr", "a-parameter-named-config"],
)
def test_a_declaration_the_walk_cannot_resolve_fails_it(tmp_path: Path, source: str) -> None:
    """Each shape was skipped, or given the global root, without a word."""
    path = tmp_path / "plugin.py"
    path.write_text(f"from beets import config\n{source}", encoding="utf-8")

    with pytest.raises(pytest.fail.Exception):
        _declarations(path, "plugin")


def test_the_walk_resolves_a_plain_declaration(tmp_path: Path) -> None:
    """The control for the test above: the shape beets uses resolves, and passes."""
    path = tmp_path / "plugin.py"
    path.write_text('from beets import config\nconfig["a"]["b"].redact = True\n', encoding="utf-8")

    assert _declarations(path, "plugin") == [(f"{tmp_path.name}/plugin.py:2", ("a", "b"))]


_INCLUDED = """\
smartplaylist:
  prefix: http://alice:SP-PASS-1@media.lan/
subsonic:
  user: SUB-USER-1
emby:
  username: EMBY-USER-1
  userid: EMBY-ID-1
lastfm:
  user: LASTFM-USER-1
fetchart:
  google_engine: ENGINE-1
lyrics:
  google_engine_ID: LYR-ENGINE-1
spotify:
  client_id: SPOT-ID-1
tidal:
  client_id: TIDAL-ID-1
kodi:
  - host: kodi.local
    user: KODI-USER-1
bareasc:
  prefix: "#"
fuzzy:
  prefix: "~"
"""


def test_declared_secrets_of_unloaded_plugins_are_masked(tmp_path: Path) -> None:
    """None of these plugins is loaded, and none of these keys matches the
    regex, so before the table every value here was served in clear."""
    (tmp_path / "secrets.yaml").write_text(_INCLUDED, encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "library: library.db\ndirectory: music\nplugins: []\ninclude:\n  - secrets.yaml\n",
        encoding="utf-8",
    )
    handle = setup_beets(str(tmp_path))
    try:
        text = build_config_snapshot(handle).effective_yaml
    finally:
        close_library(handle.lib)
    effective = yaml.safe_load(text)

    assert effective["smartplaylist"] == {"prefix": "REDACTED"}
    assert effective["subsonic"] == {"user": "REDACTED"}
    assert effective["emby"] == {"username": "REDACTED", "userid": "REDACTED"}
    assert effective["lastfm"] == {"user": "REDACTED"}
    assert effective["fetchart"] == {"google_engine": "REDACTED"}
    assert effective["lyrics"] == {"google_engine_ID": "REDACTED"}
    assert effective["spotify"] == {"client_id": "REDACTED"}
    assert effective["tidal"] == {"client_id": "REDACTED"}
    assert effective["kodi"] == [{"host": "kodi.local", "user": "REDACTED"}]
    # Controls: the same leaf names under other sections are not secrets.
    assert effective["bareasc"] == {"prefix": "#"}
    assert effective["fuzzy"] == {"prefix": "~"}
    assert effective["directory"].endswith("music")
    for leaked in ("SP-PASS-1", "USER-1", "ID-1", "ENGINE-1"):
        assert leaked not in text


def test_a_declared_null_renders_as_beets_renders_it(tmp_path: Path) -> None:
    """Unloaded, an unset declared secret reads as pass 1 reads it once loaded."""
    (tmp_path / "config.yaml").write_text(
        "library: library.db\ndirectory: music\nplugins: []\nsubsonic:\n  user:\n",
        encoding="utf-8",
    )
    handle = setup_beets(str(tmp_path))
    try:
        effective = yaml.safe_load(build_config_snapshot(handle).effective_yaml)
    finally:
        close_library(handle.lib)
    assert effective["subsonic"] == {"user": "REDACTED"}


def test_kodi_user_is_masked_with_the_plugin_loaded(tmp_path: Path) -> None:
    """Loaded, ``kodi`` is still list-shaped, so pass 1 ignores its flags."""
    (tmp_path / "config.yaml").write_text(
        "library: library.db\ndirectory: music\nplugins: [kodiupdate]\n"
        "kodi:\n  - host: kodi.local\n    user: KODI-USER-2\n    pwd: KODI-PWD-2\n",
        encoding="utf-8",
    )
    handle = setup_beets(str(tmp_path))
    try:
        # Control: the plugin really loaded and set its flag.
        assert ("kodi", "user") in beets.config.redactions
        effective = yaml.safe_load(build_config_snapshot(handle).effective_yaml)
    finally:
        close_library(handle.lib)
    assert effective["kodi"] == [{"host": "kodi.local", "user": "REDACTED", "pwd": "REDACTED"}]
