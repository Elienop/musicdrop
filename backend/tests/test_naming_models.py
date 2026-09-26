from app.models.config_editor import (
    NamingConfig,
    NamingPreviewRequest,
    NamingPreviewResponse,
    NamingRuleInput,
    RenderedRule,
    ReplaceError,
    ReplaceRuleInput,
    SaveNamingRequest,
)


def test_naming_rule_input_roundtrips() -> None:
    r = NamingRuleInput(query="default", template="$albumartist/$album/$track $title")
    assert r.query == "default"
    assert r.template.endswith("$track $title")


def test_naming_config_holds_split_keys_and_previews() -> None:
    cfg = NamingConfig(
        default="$albumartist/$album/$track $title",
        comp=None,
        singleton=None,
        custom=[
            NamingRuleInput(query="albumtype:soundtrack", template="Soundtracks/$album/$title")
        ],
        replace=[ReplaceRuleInput(pattern="[?]", replacement="_")],
        beets_replace=[ReplaceRuleInput(pattern="^-", replacement="_")],
        sha256="abc",
        previews=[RenderedRule(query="default", sample_path="A/B/01 C.flac", sample_source="x")],
        replace_errors=[],
    )
    assert cfg.custom[0].query == "albumtype:soundtrack"
    assert cfg.previews[0].error is None  # defaults to None


def test_preview_request_response_and_save_request() -> None:
    req = NamingPreviewRequest(
        rules=[NamingRuleInput(query="default", template="$title")],
        replace=[ReplaceRuleInput(pattern="(", replacement="_")],
    )
    resp = NamingPreviewResponse(
        rendered=[RenderedRule(query="default", sample_path="t.flac", sample_source="s")],
        replace_errors=[ReplaceError(index=0, pattern="(", message="bad")],
    )
    save = SaveNamingRequest(rules=req.rules, replace=req.replace, base_sha256="sha")
    assert resp.replace_errors[0].index == 0
    assert save.base_sha256 == "sha"
