"""Config-editor ADVISORY channel — ``ValidateResponse.advisories``.

Advisories are a SECOND channel, deliberately not ``errors``: CodeMirror paints
the error list red, and every config these rules fire on is *valid* YAML that
beets itself would accept. The rules exist because MusicDrop-driven imports
force or discard four ``import.*`` keys, and until now the editor accepted them
in silence (BACKLOG: "Config editor accepts import.autotag ...", pair-reviewed
2026-08-28).

What each test pins:

* the rule fires only when the user's config actually SETS the key — an omitted
  key is not an opinion, so it gets no advisory;
* the rule fires on a VALID value. ``validate_known_keys`` already raises for
  invalid ones, so an invalid value must stay on the errors channel alone and
  produce no advisory (no double-reporting);
* every message makes the in-app vs CLI distinction. None of these keys is
  globally inert: it is beets' own config file, so ``beet import`` run from the
  command line outside MusicDrop still honours all four.
"""

from __future__ import annotations

from app.beets.config_editor import parse_yaml
from app.models.config_editor import ConfigAdvisory, ImportSection, import_advisories


def _advise(yaml_text: str) -> list[ConfigAdvisory]:
    return import_advisories(parse_yaml(yaml_text))


def _keys(advisories: list[ConfigAdvisory]) -> set[str]:
    return {a.key for a in advisories}


def _message_for(advisories: list[ConfigAdvisory], key: str) -> str:
    matches = [a.message for a in advisories if a.key == key]
    assert len(matches) == 1, f"expected exactly one {key} advisory, got: {advisories}"
    return matches[0]


# --- autotag: forced True on every MusicDrop import path ---------------------


def test_autotag_false_yields_an_advisory_naming_the_force() -> None:
    advisories = _advise("import:\n  autotag: no\n")
    msg = _message_for(advisories, "import.autotag")
    assert "autotag" in msg
    assert "MusicDrop" in msg


def test_autotag_true_yields_no_advisory() -> None:
    assert _advise("import:\n  autotag: yes\n") == []


# --- duplicate_action: forced to "ask" on every MusicDrop import path --------


def test_duplicate_action_skip_yields_an_advisory_naming_both_values() -> None:
    advisories = _advise("import:\n  duplicate_action: skip\n")
    msg = _message_for(advisories, "import.duplicate_action")
    assert "skip" in msg, "the advisory must name the value the user actually chose"
    assert "ask" in msg, "the advisory must name what MusicDrop forces instead"


def test_duplicate_action_ask_yields_no_advisory() -> None:
    """``ask`` is exactly what MusicDrop forces, so nothing is discarded."""
    assert _advise("import:\n  duplicate_action: ask\n") == []


def test_every_discarded_duplicate_action_value_fires() -> None:
    """The editor offers five values; four of them are discarded in-app."""
    for value in ("skip", "keep", "remove", "merge"):
        advisories = _advise(f"import:\n  duplicate_action: {value}\n")
        assert _keys(advisories) == {"import.duplicate_action"}, value
        assert value in _message_for(advisories, "import.duplicate_action")


# --- singletons: forced False on every MusicDrop import path ----------------


def test_singletons_true_yields_an_advisory() -> None:
    advisories = _advise("import:\n  singletons: yes\n")
    msg = _message_for(advisories, "import.singletons")
    assert "singletons" in msg
    assert "MusicDrop" in msg


def test_singletons_false_yields_no_advisory() -> None:
    assert _advise("import:\n  singletons: no\n") == []


# --- incremental: honoured, but TRAPPED by beets' taghistory ----------------


def test_incremental_true_advisory_names_the_taghistory_trap() -> None:
    """``incremental`` is NOT filed as safe.

    MusicDrop honours it on a manual import, but beets records every folder a
    sweep finished OR skipped in its taghistory — so re-importing one of those
    folders from MusicDrop silently does nothing. The advisory must name that,
    not describe the key as merely inert.
    """
    advisories = _advise("import:\n  incremental: yes\n")
    msg = _message_for(advisories, "import.incremental")
    assert "taghistory" in msg
    assert "sweep" in msg.lower()


def test_incremental_false_yields_no_advisory() -> None:
    assert _advise("import:\n  incremental: no\n") == []


# --- cross-cutting ----------------------------------------------------------


def test_every_advisory_names_the_cli_escape_hatch() -> None:
    """None of these keys is globally inert — it is beets' own config file.

    This is also the reason ``link``/``hardlink``/``reflink`` and an all-off
    (in-place) config get no rule: the shape of every rule here is "MusicDrop
    overrides this, the CLI still honours it", and those are either honoured by
    the app or are beets' own behaviour with nothing to escape to.
    """
    advisories = _advise(
        "import:\n  autotag: no\n  duplicate_action: skip\n  singletons: yes\n"
        "  incremental: yes\n  delete: yes\n  link: yes\n  hardlink: yes\n"
        "  reflink: auto\n"
    )
    assert len(advisories) == 8
    for advisory in advisories:
        assert "beet import" in advisory.message, advisory.key


def test_every_rule_fires_together_in_a_stable_order() -> None:
    advisories = _advise(
        "import:\n  delete: yes\n  incremental: yes\n  singletons: yes\n"
        "  duplicate_action: merge\n  autotag: no\n  reflink: auto\n"
        "  hardlink: yes\n  link: yes\n"
    )
    assert [a.key for a in advisories] == [
        "import.autotag",
        "import.duplicate_action",
        "import.singletons",
        "import.incremental",
        "import.delete",
        "import.link",
        "import.hardlink",
        "import.reflink",
    ]


def test_delete_advisory_names_the_download_it_would_remove() -> None:
    """The destructive one. beets keeps ``delete`` alive whenever ``copy``
    survives, so before this was forced off a default import under
    ``delete: yes`` filed the album and then removed the download — silently,
    because the editor did not model the key and no rule mentioned it."""
    (advisory,) = _advise("import:\n  delete: yes\n")
    assert advisory.key == "import.delete"
    assert "downloads" in advisory.message
    assert "no effect in the app" in advisory.message
    # It must also name what to do instead. Without this the advisory reads as a
    # refusal of the capability, when the capability is `import.move` and the app
    # honours it end to end.
    assert "import.move" in advisory.message


def test_delete_false_yields_no_advisory() -> None:
    assert _advise("import:\n  delete: no\n") == []


def test_config_omitting_the_keys_yields_no_advisories() -> None:
    """A config that never mentions them has no opinion to contradict."""
    assert _advise("directory: /tmp\nimport:\n  copy: yes\n  move: no\n") == []


def test_import_section_defaults_match_what_musicdrop_forces() -> None:
    """A tripwire, and an honest note about the test above it.

    Every ``ImportSection`` default happens to equal the value MusicDrop forces,
    so today a config that OMITS a key would produce no advisory even if
    ``import_advisories`` dropped its presence check and read the defaults
    instead — which means the test above cannot observe that check. Keep the
    check anyway (the rules must not depend on a default), and pin the
    coincidence here: the day a default moves, this goes red first and the
    presence check becomes load-bearing.
    """
    defaults = ImportSection()
    assert defaults.autotag is True
    assert defaults.duplicate_action == "ask"
    assert defaults.singletons is False
    assert defaults.incremental is False
    assert defaults.delete is False
    # Modeled so a bad value is an error instead of a silently dropped key,
    # but NOT forced on the default import path, which is every path the UI
    # takes — so these three are beets' defaults, not MusicDrop's forces.
    assert defaults.link is False
    assert defaults.hardlink is False
    assert defaults.reflink is False


def test_absent_import_section_yields_no_advisories() -> None:
    assert _advise("directory: /tmp\nlibrary: /tmp/x\n") == []


def test_non_mapping_import_section_yields_no_advisories() -> None:
    """The scalar case is what makes the ``isinstance(section, Mapping)`` guard
    load-bearing, so it has to be here.

    A STRING or a LIST ``import:`` sneaks past a missing guard for the wrong
    reason — ``"autotag" not in "not-a-mapping"`` is a substring test that
    happens to be true, so every rule is skipped and the result is empty
    anyway. An int has no ``__contains__`` at all: without the guard this
    raises ``TypeError`` and the lint request 500s on a document the user is
    mid-way through typing.
    """
    assert _advise("import: not-a-mapping\n") == []
    assert _advise("import:\n  - autotag\n  - singletons\n") == []
    assert _advise("import: 5\n") == []
    assert _advise("import:\n") == []


def test_non_mapping_root_yields_no_advisories() -> None:
    """``parse_yaml("")`` returns ``None`` — a cleared editor buffer."""
    assert import_advisories(parse_yaml("")) == []
    assert import_advisories(parse_yaml("- a\n- b\n")) == []


def test_invalid_value_yields_no_advisory_because_errors_owns_it() -> None:
    """An advisory must never double-report something already painted red."""
    assert _advise("import:\n  autotag: maybe\n") == []
    assert _advise("import:\n  duplicate_action: banana\n") == []
    assert _advise("import:\n  singletons: sometimes\n") == []


def test_one_invalid_value_does_not_mute_the_other_rules() -> None:
    advisories = _advise("import:\n  autotag: maybe\n  singletons: yes\n")
    assert _keys(advisories) == {"import.singletons"}


def test_a_quoted_boolean_is_refused_because_beets_reads_it_as_true() -> None:
    """The one divergence that moves files against the user's intent.

    Pydantic's lax bool reads ``'no'`` as False; beets tests these flags with a
    bare ``if`` on the raw view, where any non-empty string is truthy. So
    ``move: 'no'`` used to save clean, fire no advisory, and give the user a
    MOVE they believed they had turned off. Measured divergence before the fix:
    ``'no'``, ``'off'``, ``'false'``, ``'0'`` — editor False, beets True.

    Unquoted ``no`` parses to a real bool in ruamel and never reaches the
    validator, so ordinary configs are untouched; this refuses the quoted
    spelling only, and says how to fix it.
    """
    import pytest
    from pydantic import ValidationError

    from app.models.config_editor import ImportSection

    for spelling in ("no", "off", "false", "0", "yes", "on", "1"):
        with pytest.raises(ValidationError, match="without the quotes"):
            ImportSection(move=spelling)  # type: ignore[arg-type]  # the point is the refusal

    # real booleans, and reflink's one real string, still pass
    assert ImportSection(move=True).move is True
    assert ImportSection(move=False).move is False
    assert ImportSection(reflink="auto").reflink == "auto"
    # ...and a quoted reflink is refused too: beets' as_choice would kill the
    # import at set_config, before any file operation.
    with pytest.raises(ValidationError, match="without the quotes"):
        ImportSection(reflink="yes")  # type: ignore[arg-type]  # the point is the refusal


def test_a_filing_flag_advisory_says_where_it_applies_not_that_it_is_ignored() -> None:
    """The quieter half of the `delete` surprise, and it lands on exactly the
    user the keep-downloads work exists for.

    `hardlink`/`link`/`reflink` ARE honoured on a manual import, a sweep and a
    bank apply — verified on disk. They are overridden by the inbox routes and
    Trash restore, which send `operation="move"`. So someone who sets
    `hardlink: yes` because they seed their downloads gets a move out of the
    inbox when they click Import on an inbox row, and nothing told them. The
    message names both halves rather than claiming the flag is ignored.
    """
    for key in ("link", "hardlink"):
        (advisory,) = _advise(f"import:\n  {key}: yes\n")
        assert advisory.key == f"import.{key}"
        assert f"import.{key}" in advisory.message  # names the setting they typed
        assert "manual import" in advisory.message  # ...where it DOES apply
        assert "Inbox imports" in advisory.message  # ...and where it does not

    # reflink's own real value counts as set
    (advisory,) = _advise("import:\n  reflink: auto\n")
    assert advisory.key == "import.reflink"
    assert "import history" not in advisory.message

    # Only a hardlink forces beets' history on, and `incremental: no` beside it
    # fires no rule of its own — so the hardlink advisory is where it is said.
    (hardlink,) = _advise("import:\n  hardlink: yes\n  incremental: no\n")
    assert "import.incremental" in hardlink.message
    (link,) = _advise("import:\n  link: yes\n")
    assert "import history" not in link.message

    # and an explicit off is not an opinion to contradict
    assert _advise("import:\n  hardlink: no\n") == []


def test_incremental_advisory_names_the_hardlink_forcing_and_the_way_past_it() -> None:
    """The sentence has to stay true as the forcing grows.

    A hardlink run now turns ``incremental`` on itself, so a user reading
    "MusicDrop honours this" needs to know a keep-downloads import does not
    leave it alone — and that there is a per-run way past the history it
    builds. The rule cannot DETECT the hardlink (``import_advisories``
    validates one key at a time, so every sibling reads its default here), so
    the clause is stated; this is what pins it.
    """
    msg = _message_for(_advise("import:\n  incremental: yes\n"), "import.incremental")
    assert "hardlink" in msg
    assert "Import them again" in msg
