from app.models.cover import CoverInstallResult


def test_cover_install_result_defaults() -> None:
    r = CoverInstallResult(ok=True, embedded=False)
    assert r.ok is True
    assert r.embedded is False
    assert r.embed_detail is None
    assert r.message is None
